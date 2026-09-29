# Handoff 4：H3 原生改写器 —— 评审加固承接（I1 · I4 · M1）

> 面向 `native-h3-prompt-rewriter` 的实施/接手会话。
> 来源：已归档变更 `minimax-h3-prompt-rewrite-stage` 的 tasks §7（评审加固）与
> `docs/code-review-6ba2f19-to-cdea2a1.md`。这两个 change（`minimax-h3-prompt-rewrite-stage`、
> `embed-openharness-h3-prompt-writer`）已于 2026-09-29 移入 `openspec/changes/archive/`，不再执行。
> 本文**只交接仍适用于原生实现的加固项**，不复述外部 `design_agent` 方案的其余内容。
> 所有后端验证必须在既有 `video-claw-backend` 容器内执行（`docker exec -i ... /app/.venv/bin/python - < 脚本`，项目测试规则）。

---

## 1. 澄清事实（为什么只承接这三项）

数据源从外部 Design Agent Platform 会话 API 换成 `_cancellable_query(LLM(), ...)` 本地调用后，
以下三块逻辑**未变**，其中评审期发现的加固项因此仍然成立，须随本变更处理：

- **引擎阶段注入**（动态六/七阶段，开关现为 `h3_rewrite.enable` / `VC_H3_REWRITE__ENABLE`）；
- **产物 / 版本记录**（`prompt_rewrite.items`、`_make_item` / `_append_version` / `_process_revisions`、停点/重生成/修订）；
- **前端阶段导航**（`TopBar` 按 `GET /api/stages` 过滤启用阶段）。

逐项判定（对照原 minimax tasks §7）：

| 原编号 | 评审项 | 承接结论 | 依据（当前工作树核实） |
| --- | --- | --- | --- |
| 7.1 / I1 | `_get_next_stage` 停用阶段死锁 | **承接 → 9.1** | `orchestrator.py` L481-489 逻辑未改；`prompt_rewrite` 仍是可关闭的注入阶段 |
| 7.2 / I2 | `_ensure_session` 异常关闭 client | **作废，不承接** | 外部会话链路整体删除：`prompt_rewrite_agent.py` 已无 `_ensure_session`/`DesignAgentClient`/`submit_turn`，改为本地 LLM 调用，无外部句柄可泄漏 |
| 7.3 / I4 | `_make_item` `versions` 共享引用 | **承接 → 9.2** | `prompt_rewrite_agent.py` L410 仍为 `(prev_item or {}).get("versions", [])`；`_process_revisions` L615 的 `source="superseded"` 追加依赖此隔离 |
| 7.4 / M1 | `fetchEnabledStages` 失败不写缓存 | **承接 → 9.3** | `TopBar.tsx` L39 `!resp.ok`、L45 `catch` 两分支仍 `return DEFAULT_ENABLED_STAGES` 且不写 `cachedEnabledStages` |

> 评审报告里的 C1（沙盒路径穿越）、I3（多能力 AND 过滤）、M2（sync 伪 URL）**不属于本节范围**：
> 它们已由 commit `441d18c` 单独修复并附验证脚本（`verify_sandbox_path_guard.py` /
> `verify_multi_ability_filter.py` / `verify_sync_remote_id.py`），与 prompt_rewrite 数据源无关。

---

## 2. 待办加固项（根因 · 位置 · 修复方向 · 验收）

### 9.1 I1 — 停用 `prompt_rewrite` 后存量会话推进死锁

- **位置**：`video-claw/video-claw/backend/core/orchestrator.py` `_get_next_stage`（约 L481-489）；
  阶段序列来源 `get_stage_order()`（约 L46-66，按 `Config.H3_REWRITE_ENABLED` 动态插入 `PROMPT_REWRITE`）。
- **根因**：`_get_next_stage` 用 `order.index(current)`，`current` 不在启用序列时抛 `ValueError` → 返回 `None`
  → "确认按钮 → 推进下一阶段"点不动，UI 无任何提示，只能删会话重开。
  场景：会话在 `enable=true` 下跑完改写，随后运维把 `VC_H3_REWRITE__ENABLE` 关掉，存量会话 `current_stage == prompt_rewrite`。
- **修复方向**：`current` 不在启用序列时，回退到"用固定全序 `ALL_STAGES` 找下一个存在于启用序列的阶段"；
  或更保守——`load_session_snapshot` 恢复时若 `current_stage` 已从启用序列消失则向前推进并记录调整。
  原报告 I1 给出的全序回退代码方向可直接沿用，仅把配置判定从 `DESIGN_AGENT_ENABLED` 换成 `H3_REWRITE_ENABLED`。
- **验收**：容器内脚本演练"启用态执行改写 → 关闭开关 → 恢复会话并点确认"，断言推进至 `video_generation`、不死锁。
  可新增 `tmp_verify/verify_stage_disable_migration.py`（配置键名用 `h3_rewrite`）。

### 9.2 I4 — `_make_item` 的 `versions` 与 `prev_item` 共享引用

- **位置**：`video-claw/video-claw/backend/core/agents/prompt_rewrite_agent.py`
  `_make_item`（约 L396-410，问题行 L410）；`_append_version`（约 L419-424）；`_process_revisions`（约 L582-617，`superseded` 追加约 L615）。
- **根因**：`_make_item` 直接引用旧 list，导致 `item["versions"] is prev_item["versions"]`；
  重生成/修订流程里对 `prev_item` 与 `item` 的两次 `append` 落在同一 list。当前主流程下 `prev_item` 会被
  orchestrator 的按 id 合并覆盖，用户暂看不到污染；一旦引入"版本历史"UI 或 regenerate 只覆盖指定 id 时读到未替换旧条目，
  就会暴露"上一版条目突然多了一条 agent 版本"。属隐性引用陷阱。
- **修复**：`_make_item` 显式浅拷贝：`"versions": list((prev_item or {}).get("versions") or []),`。
- **验收**：容器内重生成/修订流程断言 `item["versions"] is not prev_item["versions"]`，且未被覆盖条目的历史版本不变。

### 9.3 M1 — `fetchEnabledStages` 错误路径不写缓存 → 后端停机时反复打 `/api/stages`

- **位置**：`video-claw/video-claw/frontend/components/TopBar.tsx` `fetchEnabledStages`（约 L35-48）。
- **根因**：`!resp.ok`（L39）与 `catch`（L45）两分支直接 `return DEFAULT_ENABLED_STAGES`，不写 `cachedEnabledStages`；
  `useEnabledStages` 的 effect 以 `cachedEnabledStages` 为门槛（L54），后端不可达时每次组件重挂都会重新发请求。
- **修复**：失败分支也写入 DEFAULT 负缓存，配合一个短 TTL（例如 10~30s）避免"后端恢复后仍长时间看不到 `prompt_rewrite` Tab"。
- **验收**：前端构建通过（`npm run lint`、`npm run build`）；后端停机时反复挂载不产生 `/api/stages` 请求风暴。
  （前端测试按项目规则允许在本机执行；若项目已有前端测试命令/环境则优先沿用。）

---

## 3. 复现方式（容器内 / 本机）

```bash
# 9.1 I1：确认 _get_next_stage 与 get_stage_order 现状
docker exec video-claw-backend grep -n "def _get_next_stage\|def get_stage_order\|H3_REWRITE_ENABLED" \
  /app/core/orchestrator.py

# 9.2 I4：确认 versions 共享引用与 superseded 追加点
docker exec video-claw-backend grep -n '"versions"\|_append_version\|source="superseded"' \
  /app/core/agents/prompt_rewrite_agent.py

# 9.3 M1：确认失败分支未写缓存（前端，本机读源码即可）
grep -n "cachedEnabledStages\|resp.ok\|catch" video-claw/video-claw/frontend/components/TopBar.tsx
```

---

## 4. 交接边界

- 本文仅登记**加固项的承接与验收口径**，不代表已实施；实施落点为 `native-h3-prompt-rewriter` tasks §9。
- 归档的 `minimax-h3-prompt-rewrite-stage` / `embed-openharness-h3-prompt-writer` 不再执行，其未勾选任务与本文重复的部分以本文为准（I1/I4/M1），I2 不再出现。
- 三项修复均为**行为兼容**改动，不改产物结构与前端交互契约；与 §1–§8 的数据源内嵌工作可在同一收尾批次处理。
