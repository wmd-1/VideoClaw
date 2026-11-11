# 确定性校验误报报告

- 金标样例：`cases.json`（T2VA/I2VA/FL2VA/L2VA/Ref2VA 各 1 例，采用官方句式）
- 校验入口：`_validate_h3_prompt(text, mode, duration, picture_count=...)`
- 回归测试：`tests/test_prompt_rewrite_golden.py`、`tests/test_prompt_rewrite_semantic_validation.py`

## 规则与金标结果

| 编号 | 规则 | 金标命中 | 误报 | 构造反例（测试名） |
| --- | --- | --- | --- | --- |
| R1 | 对齐声明首行 + 空行 | 3/3 关键帧金标通过 | 无 | `test_alignment_must_be_first_line_with_blank_line` |
| R2 | I2VA 官方首帧句式 | 通过 | 无 | `test_i2va_requires_official_first_frame_sentence` |
| R3 | FL2VA/L2VA 尾帧对齐点 | 通过 | 无 | `test_fl2va_last_frame_mark_must_equal_duration` |
| R4 | `S.SS` 两位小数 | 通过 | 无 | `test_alignment_mark_requires_two_decimals` |
| R5 | 首镜头无时间戳 | 通过（金标无时间戳） | 无 | `test_first_shot_must_not_have_timestamp` |
| R6 | cut time 严格递增且不越界 | 通过（金标 `[Shot 2] At 00:04.000`） | 无 | `test_cut_time_must_be_strictly_increasing_and_within_duration` |
| R7 | 时间点不越界 | 通过 | 无 | 同上（越界分支） |
| R8 | `<Picture N>` 数量与素材一致 | 5/5 通过 | 无 | `test_picture_label_count_must_match_assets`；L2VA 接线级见 `test_l2va_production_input_uses_next_segment_frame` |
| R9 | 未定义标签（仅 Ref2VA） | 通过 | 无 | `test_ref2va_undefined_label_is_rejected_and_base_modes_are_exempt` |

## 明确未实现的规则及原因

| 候选规则 | 决定 | 依据 |
| --- | --- | --- |
| 区间式时间轴必须从 0.00s 连续覆盖到结尾 | 不实现 | `base-en.txt` L177 官方示例使用 `[Shot N]` + `At 00:05.000`，强制区间会误伤合法输出 |
| Ref2VA 已定义标签必须在详细描述段被引用 | 不实现 | `ref-en.txt` L37 明确允许标签只在定义内引用、后续不单独使用 |
| `<Video N>` / `<Audio N>` 素材一致性 | 不做确定性校验 | 当前改写输入链路只提供图片素材清单，硬查会变成猜测；该维度由评审覆盖 |

## 结论

五模式金标在新校验器下**全部零违规**；所有新增规则均有官方原文依据与对应反例测试，未发现需要降级为评审维度的规则。

## 真实模型补充结论

- 真实会话（`1790047810282`）的 12 条分镜为 I2VA；实测 2 条均通过确定性校验（每条 1 次生成）。
- 真实模型稳定不写「对齐声明后空行」，因此该格式在**校验前做确定性规范化**（仅补空行），校验器仍保留空行规则；这不是放宽硬校验，而是把纯格式差异从重试环中移除。
- 真实模型在强化 `system_zh.txt` 后能写出目标总时长，R7 未产生误报。
- R4/R6 的真实样本未出现误报；额外对齐点与后续镜头缺 cut time 的漏检已补上（见对应测试）。
