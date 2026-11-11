# 验收小结：H3 改写校验补全与带草稿修订

## 环境

- 复用既有容器 `video-claw-backend`（镜像 `video-claw-backend:latest`），未重建镜像、未改 `docker-compose.yml`/`Dockerfile.backend`、未新增依赖。
- 解释器：容器内 `/app/.venv/bin/python`（自带 pytest 9.2.0）。
- 测试目录未挂载，按既有环境约束临时复制进容器：`docker compose cp ./video-claw/video-claw/backend/tests/. backend:/app/tests/`。

## 复现命令

```bash
cd /home/wmd/projects/VideoClaw
docker compose cp ./video-claw/video-claw/backend/tests/. backend:/app/tests/
docker compose exec -T backend /app/.venv/bin/python -m pytest \
  tests/test_prompt_rewrite_adjacency.py \
  tests/test_prompt_rewrite_failure_priority.py \
  tests/test_prompt_rewrite_golden.py \
  tests/test_prompt_rewrite_judge.py \
  tests/test_prompt_rewrite_revision_draft.py \
  tests/test_prompt_rewrite_semantic_validation.py \
  tests/test_prompt_templates_contract.py \
  tests/test_prompt_call_sites_contract.py \
  tests/test_observability.py \
  tests/test_mode_mapping.py \
  tests/test_grounding_cache_integration.py \
  tests/test_prompt_freshness.py \
  tests/test_prompt_freshness_view.py -q
docker compose exec -T backend /app/.venv/bin/python -m pytest tests -q

# 真实 LLM 实测（既有容器内执行；需会话 1790047810282 存在，只读产物、不写会话）
docker compose cp ./video-claw/video-claw/backend/tests/fixtures/h3_rewrite/real_llm_probe.py backend:/tmp/real_llm_probe.py
docker compose exec -T -w /app backend /app/.venv/bin/python /tmp/real_llm_probe.py
docker compose cp ./video-claw/video-claw/backend/tests/fixtures/h3_rewrite/judge_negatives.py backend:/tmp/judge_negatives.py
docker compose exec -T -w /app backend /app/.venv/bin/python /tmp/judge_negatives.py
```

真实实测环境：既有 `video-claw-backend` 容器、`/app/.venv/bin/python`、`deepseek-v4.1-flash`；未重建镜像、未改 compose/依赖。脚本依赖会话 `1790047810282` 的产物，换环境需改脚本内 `SID`；数据为一次性实测，表中数值来自上述命令输出。

## 结果

| 测试集 | 结果 |
| --- | --- |
| 改写 / 契约 / 观测 / 模式 / grounding 相关集合 | **100 passed** |
| 全量后端回归 | **709 passed** |
| 新增用例（金标 + 校验 + 评审 + 草稿修订） | **32 passed** |

## Spec 场景对照

| 场景 | 覆盖用例 | 结果 |
| --- | --- | --- |
| judge 关闭时调用次数与产物字段不变 | `test_judge_disabled_never_calls_judge` | 通过（1 次生成、0 次评审、无 `quality`） |
| 每条目至多评审一次 | `test_judge_triggers_one_revision_with_draft` | 通过（2 生成 + 1 评审） |
| 评审不可用不阻塞 | `test_run_judge_degrades_on_bad_output_or_exception` | 通过（`verdict=unavailable`） |
| 评审修订后硬校验失败 | `test_judge_revision_fails_validation_returns_judged_version` | 通过（返回被评审版本，`applies_to_final=true`，不判 failed） |
| 低分且预算耗尽保留结果 | `test_judge_budget_exhausted_keeps_third_result` | 通过（3 生成 + 1 评审，结果保留） |
| 最坏路径调用次数有界 | 上述计数断言 | 通过（生成 ≤3、评审 ≤1） |
| L2VA 尾帧来自下一分镜（接线级） | `test_l2va_production_input_uses_next_segment_frame` | 通过（`mode=L2VA`、`picture_count=1`） |
| 官方句式零误报 | `test_golden_cases_pass_deterministic_validation` | 通过（5/5 金标） |
| 带草稿修订与字段分节截断 | `test_retry_request_carries_draft_and_violations`、`test_oversized_draft_is_truncated_by_field_and_keeps_tail_fields` | 通过 |
| 连续两轮同问题回退完整重写 | `test_same_violations_twice_falls_back_to_full_rewrite` | 通过 |

## 与基线的对比

- 金标首次通过率：5/5（基线 5/5）。
- judge 关闭时正常路径仍为 1 次生成调用；校验失败路径上限 3 次，与基线一致。
- 新增确定性规则未造成金标误报（详见 `validation_report.md`）。

## 真实 LLM 实测

- 会话：`1790047810282`（已完成分镜/参考图/改写，含 12 条分镜；复用其产物，不写会话）
- 模型：`deepseek-v4.1-flash`（改写与评审同源）；VLM `qwen3.8-flash`
- 样本：`seg_01_01`（14s）、`seg_01_02`（11s），均为 I2VA

| 运行 | 条目 | 首次通过 | 生成调用 | 评审调用 | verdict | score | revision_attempted | applies_to_final | 耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| judge 关闭 | seg_01_01 | 是 | 1 | 0 | — | — | — | — | 22.0s |
| judge 关闭 | seg_01_02 | 是 | 1 | 0 | — | — | — | — | 19.6s |
| judge 开启 | seg_01_01 | 是 | 1 | 1 | pass | 5 | false | true | 63.8s |
| judge 开启 | seg_01_02 | 是 | 2 | 1 | needs_revision | 4 | true | false | 72.1s |

真实负例（同一上下文，三条低质草稿，仅调评审）：

| 用例 | verdict | score | 主要问题 |
| --- | --- | --- | --- |
| abstract-and-vague | needs_revision | 1 | 抽象、漏剧情与角色、无镜头结构 |
| authority-conflict | needs_revision | 2 | 人物/场景整体替换，违反权威事实 |
| missing-key-action | needs_revision | 1 | 漏关键动作与对白，主体姿态冲突 |

## 6.4 统计口径

- **首次通过率**：judge 关闭 2/2；judge 开启 2/2（均首次通过确定性校验）。
- **平均重试次数**：judge 关闭 0.0 次/条；judge 开启 0.5 次/条（2 条共 3 次生成，其中 1 条触发 1 次修订）。
- **评审调用率**：2/2 = 100%（judge 开启时每条目各 1 次评审）。
- **修订触发率**：1/2 = 50%（仅 seg_01_02 触发一次修订）。
- **`applies_to_final=true` 占比**：1/2 = 50%（另一条修订后未复评，旧分不适用于最终正文）。

## 阈值结论（tasks 4.6）

- 真实正例分数：5、4（其中 4 分伴随 `continuity=3` 的 major 问题，被模型判定 `needs_revision` 并触发一次修订）。
- 真实负例分数：1、2、1（全部含 blocker）。
- `judge_threshold=3` 落在负例上界（2）与正例下界（4）之间，**维持默认 3**；分布间隔明显，当前样本下无需调整。
- 样本量较小（2 正例 + 3 负例），后续扩大样本时按同一脚本复测；阈值调整不改变本变更的功能契约。

## 实测驱动的修正

1. **关键帧对齐空行**：真实模型稳定输出「对齐句\n字段」，3 次重试仍不补空行。已改为校验前做确定性规范化（仅补空行，不改正文），并在 `system_zh.txt` 增加显式结构要求；修正后同一条目首次通过（1 次调用）。
2. **时长声明**：`system_zh.txt` 增加"必须显式写出目标总时长"后，14 秒条目不再因时长声明失败；该问题在旧实现中同样存在（会话历史 `seg_01_01` 原为 failed）。
3. 评审延迟：judge 关闭 20–22s/条，开启 64–72s/条（含 1 次生成 + 1 次评审，必要时 +1 次修订），成本口径已写入文档。
