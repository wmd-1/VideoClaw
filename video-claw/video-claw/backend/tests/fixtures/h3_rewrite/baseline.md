# H3 改写基线（judge 关闭路径）

- 采集时点：本变更实施后，`h3_rewrite.judge_enable=false`（默认配置）
- 采集方式：`test_prompt_rewrite_golden.py` + `test_prompt_rewrite_revision_draft.py` 的 mock LLM 重放
- 数据来源：`backend/tests/fixtures/h3_rewrite/cases.json`（五模式金标）

## 金标首次通过

| 模式 | 用例 | 首次通过 | 单条目 LLM 调用 | 违规清单 |
| --- | --- | --- | --- | --- |
| T2VA | t2va-basic | 是 | 1 | 无 |
| I2VA | i2va-first-frame | 是 | 1 | 无 |
| FL2VA | fl2va-start-end | 是 | 1 | 无 |
| L2VA | l2va-last-frame | 是 | 1 | 无 |
| Ref2VA | ref2va-full-reference | 是 | 1 | 无 |

## 非金标路径（构造用例）

| 场景 | 生成调用 | 结果 |
| --- | --- | --- |
| 首次输出缺字段 | 2 | 第二次带被拒草稿与违规清单，通过后 `done` |
| 连续三次不合规 | 3 | `failed`，`error` 含完整违规清单 |
| 连续两轮问题相同 | 3 | 第三次回退为不带草稿的完整重写 |

## 结论

- 五模式金标在确定性校验器下**零违规**，可作为后续规则变更的误报对照。
- judge 关闭时，单条目生成调用上限为 3 次，正常路径 1 次，与变更前口径一致。
- 基线与复现命令见 `acceptance.md`；基线文件入库，不依赖 `tmp_verify/`。
