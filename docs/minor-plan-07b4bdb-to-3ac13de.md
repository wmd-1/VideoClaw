# 针对性方案：M-a~M-f（源自 `docs/code-review-07b4bdb-to-3ac13de.md` Minor 项）

> 日期：2026-09-30　基线：HEAD `3ac13de`（C-1/I-A 已修复）
> 本方案先经确认再实施；各项均已核实当前代码现状（含行号）。
>
> **实施状态（2026-09-30）**：按推荐组合（M-c=采纳现状、M-d=仅加汇总日志、M-e=维持预留）全部落地：M-a ✅ / M-b ✅ / M-c ✅（design.md 修订）/ M-d ✅（5c4/5c5 断言）/ M-e ✅（文案）/ M-f ✅（动态精确断言+灵敏度抽测）；容器内 7 套验证全绿，前端 tsc 零错误。实施中发现并修复一处验证脚本自身问题（捕获 handler 未设 logger 级别导致 INFO 被滤，属测试环境默认 WARNING 所致，应用自身日志已配 INFO）。openspec 落点：native-h3-prompt-rewriter tasks 10.1-10.4、video-audio-video-references tasks 7.5、video-generation-parameter-system tasks 6.1。

## 总览

| 项 | 性质 | 处置策略 | OpenSpec 落点 | 需拍板 |
|----|------|----------|---------------|--------|
| M-a | 文档残留 | 改措辞 | `native-h3-prompt-rewriter` 新增 10.1 | 否 |
| M-b | 模板残留 | 删旧注释行 | `video-audio-video-references` 新增 7.5 | 否 |
| M-c | 设计 vs 实现偏离 | **采纳现状、修订设计表述** | `native-h3-prompt-rewriter` design.md + 10.2 | **是** |
| M-d | 可观测性 | 文档明示 + 阶段完成时一次性降级汇总日志；不改 API 契约 | `native-h3-prompt-rewriter` spec 补 Scenario + 10.3 | **是** |
| M-e | 预留字段 | 文案标注"当前未生效"；不透传（避免行为变更） | `native-h3-prompt-rewriter` 10.4 | **是** |
| M-f | 测试精度回归 | 配置推导的**动态精确期望**，恢复相等断言 | `video-generation-parameter-system` 新增任务 | 否 |

## 逐项方案

### M-a　`api.md` "外部会话"残留（纯文档）
- 现状已核实：`backend/docs/api.md` L345「复用外部改写会话」、L346「向外部会话追加一轮修订」，与 L342「本地 LLM 调用」矛盾。
- 改法：L345 → 「单条目重生成（后台执行，基于本地版本链与 continuity 续写）」；L346 → 「按修改意见追加一轮修订（上一版与意见进请求上下文）」。
- 验证：文档 diff，`grep -rn '外部.*会话' backend/docs/` 清零。

### M-b　`.env.example` 旧变量注释（纯模板清理）
- 现状已核实：L139-140 仍列 `# VC_DESIGN_AGENT__BASE_URL=...`、`# VC_DESIGN_AGENT__LOGIN_NAME=...`，L136 已有废弃说明。
- 改法：删除 L139-140 两行（保留 L136 的废弃提示注释即可，避免"注释里还活着"的误导）。
- 验证：文件 diff；启动服务不受影响（纯注释）。

### M-c　`system_zh.txt` 指南附带策略（**建议采纳现状，需确认**）
- 现状已核实：design.md L30/L76 两处写「system_zh.txt 内联两份指南全文」；实际 `system_zh.txt` 39 行为提炼+引用，指南由 `_query_llm` 按模式单独附带（每请求只带相关指南）。
- 评估：实现的按需附带 **token 更省、知识保真不变**（两份指南仍逐字节入库、由 resolver 选路），优于字面"全文内联"（那会使每次请求都携带 563 行无关指南）。判定为合理精化而非缺陷。
- 改法：修订 design.md L30/L76 表述为「system_zh.txt 提炼原则并按模式引用指南；指南全文由 resolver 选路、`_query_llm` 按模式附带」；决策文档不动。
- 验证：design.md diff；`verify_native_h3_rewriter.py` 不涉及。

### M-d　VLM 缺省静默降级 + enable 不继承（**行为微调，需确认**）
- 现状已核实：`prompt_rewrite_agent.py` L288-300 VLM 缺省/异常逐分镜吞错回退 `text_only`，仅每分镜一条 warning；`enable` 不从旧 `design_agent.enable` 继承（外部数据源已死，判定合理，维持现状）。
- 改法（保持 D5"正交回退"契约不变，只补可观测性）：
  1. 阶段完成时输出一条**汇总日志**：`grounding 降级 N/M 条目（text_only）`（仅 N>0 时）；条目级 `grounding.text_only` 标记已有，不动；
  2. `api.md` 与设置页描述补一句「vlm 未配置/失败时该条目降级为纯文本改写，模式不变」；
  3. openspec spec「VLM 参考图 grounding」Requirement 增补 Scenario「整阶段零 VLM 配置时的降级可见性」。
- **不做**：不改 API 响应契约、不加前端横幅（避免范围膨胀；如需 UI 提示另立 change）。
- 验证：容器内 `verify_native_h3_rewriter.py` 增断言（5c 场景下汇总日志/计数可见，或函数级 `_grounding_degraded_count` 断言）。

### M-e　`temperature` 预留未透传（**建议维持预留，需确认**）
- 现状已核实：`config.py` L96 注明"预留"；设置页 `app/settings/page.tsx` L82 label「temperature 采样温度（预留字段）」。
- 评估：透传 temperature 会改变改写输出分布，动摇金标基线，属行为变更，**不应混入加固批次**。
- 改法：仅把设置页 label 明确为「temperature 采样温度（当前未生效，预留）」，`api.md` 对应字段说明同步；不透传。
- 验证：前端 `npx tsc --noEmit`；文案 diff。

### M-f　验证脚本断言精度恢复（测试质量）
- 现状已核实：`verify_models_filter.py` L31/L36/L39 由"精确相等"退化为 `bool(ids) and ids <= custom_ids`；`verify_prune.py` 同类。退化动因是 5a0052a 的跨环境适配（配置不同 → 期望集不同），方向对但丢了精度。
- 改法：**期望集从配置动态推导**，恢复相等断言——
  - 期望 = custom_models 中满足「media_type 匹配 ∧ 声明该 ability ∧ `api_contract_verified`」的 id 集（与后端 `list_api_models` 同一语义、但由脚本独立从 `/api/config` 原始数据推导，不复用被测代码路径）；
  - 断言：`ids == expected`（相等）+ 保留 `ids <= custom_ids`（子集）双保险；
  - `verify_models_filter.py`、`verify_prune.py` 两脚本同法恢复。
- 落点 openspec：`video-generation-parameter-system`（能力声明联动范畴）新增任务。
- 验证：容器内两脚本重跑 ALL PASS，且故意改一条能力声明应导致 FAIL（灵敏度抽测，测后还原）。

## 实施与提交顺序

1. openspec 修订（两 change 的 spec/design/proposal/tasks 增补，validate）；
2. 代码/文档改动（M-a→M-b→M-c→M-d→M-e→M-f）；
3. 容器内验证（M-f 两脚本 + `verify_native_h3_rewriter.py` 扩展回归 + 冒烟）；
4. 勾选任务；**单独一个 git 提交**（`fix(h3-rewrite): Minor 加固承接`，范围隔离同前：仅本轮文件，显式 add）。

> M-a/M-b/M-c/M-e 为文档/文案类，零行为变更；M-d 仅加一条汇总日志与断言；M-f 仅动测试脚本。风险面从小到大依次为：M-f（脚本）≈ M-d（一行日志）< 其余（文档）。
