# Handoff 3：H3 提示词改写阶段 —— 待办与验证缺口

> 面向 `native-h3-prompt-rewriter` / `minimax-h3-prompt-rewrite-stage` 的接手会话。
> 本文档由「默认模型/配置」侧会话在 2026-09-29 验证时发现，**未做任何修改**，仅交接事实与复现方式。
> 所有验证必须在既有 `video-claw-backend` 容器内执行（项目测试规则）。

## 1. 背景

- 提示词改写阶段的外部服务方案（`design_agent` + `models/design_agent_client.py`）已被**原生实现**取代：配置段现为 `h3_rewrite`，`config.py` 检测到旧段 `design_agent` 会 warning 并 pop（`_coerce_config` 约 L184-190）。
- 相关在途 change：`native-h3-prompt-rewriter`（0/19）、`minimax-h3-prompt-rewrite-stage`（16/22）。

## 2. 已确认事实（不是缺陷，供参考）

| 项 | 结论 |
| --- | --- |
| 阶段是否默认关闭 | **是**。`DEFAULT_CONFIG["h3_rewrite"]["enable"] = False`；`config.yaml.example` 为 `enable: false`；容器内重启后实测 `Config.CONFIG['h3_rewrite']['enable'] is False` |
| 关闭时行为 | 阶段不进入流程/导航；`check_enabled()` 抛明确中文错误（`prompt_rewrite_agent.py` L623）：`提示词改写阶段未启用：请在设置页「H3 提示词改写」开启 enable，或在 .env 配置 VC_H3_REWRITE__ENABLE=true` |
| 旧配置段 | 运行时 `config.yaml` 中残留的 `design_agent` 段已清理（现无未知段残留） |

## 3. 待办缺陷

### 缺陷 A：模板占位符与 format 参数缺少一致性保护（曾导致运行时 KeyError）

- **现象**（2026-09-29 03:19 之前复现）：改写阶段全部条目失败，日志：
  `prompt_rewrite: 分镜 seg_01_01 改写失败: 'input_mode'`（即 `KeyError: 'input_mode'`）
- **直接原因**：`prompts/prompt_rewrite/rewrite_zh.txt` 第 3 行含占位符 `{input_mode}`，而当时**运行中的后端进程**（旧 `prompt_rewrite_agent.py`）在 `template.format(...)`（L343）未提供该 key。
- **当前状态**：工作树的 agent 已传 `input_mode=mode`（L344 / L406），重启后端后该 KeyError **不再复现**。
- **残留风险（需要处理）**：
  1. `prompts/` 与 `core/agents/` 都属只读挂载，二者可在**不同提交/不同机器**上单独更新 → 同类"模板与代码不同步"会再次以 500/条目级失败暴露，且异常被 catch 成一条短语（`改写失败: 'input_mode'`），排障信息很弱；
  2. 目前**没有任何断言**保证「模板里的占位符集合 ⊆ format 调用提供的 key 集合」。
- **建议**：
  - 在容器内新增一致性断言（读取 `prompts/prompt_rewrite/*.txt` 的 `string.Formatter().parse()` 占位符集合，与实际 format 参数比对），纳入 `tmp_verify/`；
  - catch 处日志补充模板名与缺失 key（`logger.warning("rewrite template missing key %s in %s", ...)`），避免只剩 `str(KeyError)` 的裸引号信息。

### 缺陷 B：`tmp_verify/verify_failure_paths.py` 整体仍针对已废弃的外部服务方案

- **现状**：脚本设置 `config.Config.DESIGN_AGENT_ENABLED / DESIGN_AGENT_BASE_URL`（L37/38/49/78/79）并 `from models.design_agent_client import DesignAgentUnavailable`（L47 附近）——这些字段/模块已被 `h3_rewrite` 取代。
- **后果**：
  - 场景 1、2 因错误文案巧合仍显示 PASS（**假绿**：实际走的是 `check_enabled` 分支，并非在测"未配置地址/服务停机"）；
  - 场景 **2c 直接 FAIL**，异常为 `ValueError: 提示词改写阶段未启用…`（脚本没有开启 `h3_rewrite.enable`）。
- **需要修**：把脚本迁移到 `Config.CONFIG['h3_rewrite'] = {...,'enable': True}` + 原生 rewriter 的失败注入点（不再依赖 `design_agent_client`），并确认原「场景 2 不可达时阶段快速失败（<30s）」在新实现下是否仍然成立（若原生实现改为本地 LLM 调用，失败语义可能已变，需要重新定义断言）。

### 缺陷 C：条目级失败隔离目前缺乏有效回归覆盖

- 2c 想验证的「单条不可达时该条目标记失败、阶段不崩溃、`requires_intervention is True`」目前**没有被任何通过中的用例覆盖**（B 修好后才能验证）。
- 相关未完成项：`minimax-h3-prompt-rewrite-stage` 任务的真实环境验证；`native-h3-prompt-rewriter` 尚 0/19。

## 4. 复现方式（容器内）

```bash
# 1) 观察当前失败形态（脚本过时导致的 check_enabled 异常）
docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_failure_paths.py

# 2) 确认默认关闭与配置段生效
docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python -c "
from config import Config
print('h3_rewrite:', Config.CONFIG.get('h3_rewrite'))
print('design_agent 残留:', 'design_agent' in Config.CONFIG)
"

# 3) 模板占位符（缺陷 A 的核查入口）
docker exec video-claw-backend grep -n "{[a-z_]*}" /app/prompts/prompt_rewrite/rewrite_zh.txt
```

## 5. 验收标准

1. `verify_failure_paths.py`（或替代脚本）在**新配置段**下全部断言通过，且不存在因错误文案巧合造成的假绿；
2. 新增模板/参数一致性断言并纳入回归清单（与 `verify_video_h3`、`verify_preflight` 等一起跑）；
3. 条目级失败隔离（原 2c）有真实通过的用例：部分条目失败时阶段不崩溃、失败条目可单独重试、`requires_intervention` 为 True；
4. 失败日志包含模板名/缺失 key/片段 id，不再只有裸 `'input_mode'`；
5. 若涉及真实服务，请在真实部署执行并在 `openspec/changes/minimax-h3-prompt-rewrite-stage/tasks.md` 勾选对应项（当前 16/22）。

## 6. 交接边界

- 本会话**不会**改动 `prompt_rewrite_agent.py`、`orchestrator.py`、`api/routers/stages.py`、`prompts/prompt_rewrite/**`、`frontend/app/settings/page.tsx`、`config.py`、`.env.example`——这些文件当前工作树中混有你们的未提交改动（`h3_rewrite` 重构），为避免打断你们的在途工作，本次提交只包含本会话的独立文件（提交 `5a0052a`）。
- 待你们收尾后，`config.py` / `config.yaml.example` 中还有一份**本会话的未提交改动**需要一并入库：`DEFAULT_CONFIG["models"]` 与模板 `models` 段改为全空（默认模型必须由用户在设置页或 `.env VC_MODEL_*` 指定，未指定时 start 明确 400），配套文案见已提交的 `api/routers/workflow.py`。
