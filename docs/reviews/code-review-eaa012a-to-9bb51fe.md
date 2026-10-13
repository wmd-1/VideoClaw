# VideoClaw 提交区间代码评审报告（第四轮）

- **评审日期**：2026-10-12
- **评审范围**：`eaa012a..9bb51fe`（上轮报告终点之后的 18 个提交，评审基准 HEAD `9bb51fe`，评审时工作区干净；
  本报告入库时 HEAD 已推进至 `452e96d`，即本报告自身的提交）
  - `fa78e83` fix(sandbox)：媒体入参统一守卫扩至 i2i/vlm，远程形态原样透传（上轮 I-1/I-2 承接）
  - `ac437a4` test(models-filter)：删永真式并叠加供应商完整性谓词（上轮 M-1/M-2 承接）
  - `781c2e0` chore(env)：.env.example 补终止换行（上轮 M-4 承接）
  - `98594fe` / `d7eea99` / `c425fbb` docs(review)：第三轮报告、补记与断言条数勘误
  - `2e0c529` feat(model-selector)：首页/运行时选择器兜底占位与设置页非阻塞提示
  - `904e8a5` feat(video)：MiniMax-H3 768 短边档位与采样调度参数可声明，设置页高级能力参数 UI
  - `afd29dc` fix(h3-rewrite-ui)：改写阶段逐条进度条 + 条目列表滚动修复
  - `71df3ec` fix(workflow-ui)：深链冷启动进入可选注入阶段不再丢产物与 stage 参数
  - `4943960` chore(tmp_verify)：提示词改写前端取证夹具脚本
  - `b10f98c` docs(workflow)：阶段与停点序号改为名称/ID 化
  - `daea6bb` fix(video)：任务类型与推理服务 partition 对齐，含图请求不再因 --task-type 限制被 500
  - `4068ab1` chore(lint)：eslint 配置对齐 Next 16
  - `993e4bb` feat(h3-rewrite)：增量改写条目流式预览
  - `c808ecb` docs(workflow)：记录提示词改写阶段
  - `0069859` test(h3-rewrite)：增量条目回归覆盖
  - `9bb51fe` docs(structure)：docs 目录分目录管理并新增索引
  - 规模：65 files，+3211/−424（其中 `frontend/package-lock.json` +606/−118）
- **对照基准**：`docs/reviews/code-review-3ac13de-to-eaa012a.md`（上轮 I-1/I-2/M-1~M-5 验收口径）、
  `openspec/changes/prompt-rewrite-incremental-items/`（993e4bb/afd29dc/0069859 的需求来源）、
  `openspec/changes/video-generation-parameter-system/` 与 `video-audio-video-references/`（904e8a5/daea6bb 关联）
- **评审方式**：按 Superpowers `requesting-code-review` 流程派发独立 Code Reviewer 子代理做只读静态评审
  （不派发二级评审、不执行测试）；报告撰写者对 Critical/Important 结论逐条以源码交叉核实，
  其中**核实阶段修正了评审者一处过度表述**（本报告 I-2 的轮询持续性，风险本质不变，见该条备注）
- **前轮报告**：`docs/reviews/code-review-3ac13de-to-eaa012a.md`（第三轮）、
  `code-review-07b4bdb-to-3ac13de.md`（第二轮）、`code-review-6ba2f19-to-cdea2a1.md`（第一轮）
- **编号约定**：本报告问题编号为本轮新发现（C-1/I-1/I-2/M-1~M-4）；引用上轮问题时标注"上轮"

> **修订记录（2026-10-13）**：依据外部复审意见修订 8 处外围偏差（计数、行号归属、证据转述、文件路径、
> HEAD 时态），全部经源码/git 复核后采纳；三条问题结论（C-1/I-1/I-2）与"Not ready to merge"判定不变。

---

## 一、优点（Strengths）

1. **上轮 I-1/I-2/M-3 的守卫层承接兑现到位**
   `backend/api/routers/sandbox.py` L215-242：`_guard_media_input` + `_guard_media_inputs` 列表助手；
   i2i（L429-433，`req.image`）、vlm（L347-351，`req.images` 逐条）均套用且在 `_start_active_task`
   **之前**拒绝；video 的 `input_data` 存守卫后的规范化路径（L488-498），历史回放口径一致（上轮 M-3）。

2. **`ac437a4` 测试整改质量高，直击"自洽假绿"病根**
   `tmp_verify/verify_models_filter.py`：永真式已删（L75-81 恢复纯 `ids == expected`）；
   `custom_provider_ok`（L46-58）在脚本内独立重实现谓词，配 5 项合成自检（L107-117，含
   `CUSTOM_PROTOCOLS` 非空防自检空转）+ 与后端 `model_availability` 逐项漂移交叉校核（L120-125）。
   期望集与被测路径真正解耦。

3. **`993e4bb` 与 openspec design 契约吻合严格**
   后端 `_event_item_summary`（`prompt_rewrite_agent.py` L79-100）只扩事件字段；
   `orchestrator.py` merge_progress_artifact 的 shell 白名单（L1038-1050）**确实未扩**
   （仍为 `id/status/selected/versions(+rewrite_result/error_type/error)`），执行期正文不落盘落实；
   `prompt_rewrite_agent.py` 的 `EVENT_BODY_LIMIT=8000` 集中定义且注释明确"非契约、未经真实分布验证"
   （`prompt_rewrite_agent.py` L44-47）。
   前端 `lib/assetMerge.ts` 的 `preserveRewriteBodies` 对 `truncated` 的"后端已交正文则完全以后端为准"
   例外处理（L109-137）是真机发现缺陷的回归修复，并有对应断言（`verify_frontend_asset_merge.mjs` #5/#5b）。

4. **`0069859` 回归覆盖验证真实行为，非自洽断言**
   `tmp_verify/verify_incremental_items_accept.py` 走真实 `WorkflowEngine.execute_stage` + 真实合并路径 +
   FakeLLM，断言含截断前缀是最终正文前缀（2.3）、中间写盘体积最小值小于完成时体积即正文只在完成时
   进入产物（3.3，`min(PERSIST) < final_json`，非严格单调性证明）、重生成幂等/versions/selected/
   失败双语义（4.1-4.5）；`verify_frontend_asset_merge.mjs` 直接 `import` 出货的 `assetMerge.ts`
   （无复制实现）。

5. **`71df3ec` 深链修复点位完整且注释交代根因**
   `WorkflowPanel.tsx` L256-267（URL 参数按 `ALL_stageOrder` 校验）、L1202-1211（状态恢复）、
   L1283-1291（轮询启动遍历）三处统一改用全量已知集合，注释明确"冷启动瞬间 stageOrder 还是默认六阶段"
   的竞态来源。

6. **`daea6bb` partition 对齐向后兼容且不静默丢图**
   `custom_video.py` L399-401：未声明 `tasks` 时零改动；含图但任务不支持时明确 raise 并给出修复指引
   （L412-418）；降级 ref2va 时记录 adjustment（L409-411）。

7. **`2e0c529` 兜底占位不洗值、不放宽过滤**
   `ProviderSelect.tsx` + `modelRegistry.ts` `withSlotPlaceholder`：占位项与正常项共存、保留历史选择、
   异常时仍保留可见占位（L94-97）；`describeSlotIssue` 设置页提示明确"不阻止保存"（L106-108）。
   首页与顶栏共 10 处下拉均已接线（HomePage 5 + TopBar 5），未引入"可用性倒退"。

8. **openspec tasks 诚实记录未覆盖子项**（5.4-① 条目级 running 事件另案、5.4-② 场景 B FAIL 且归属
   `workflow-poll-timeout-visibility`），符合"允许如实记为未覆盖"的约定。

---

## 二、问题（本轮新发现）

### Critical（必须修）

#### C-1　`file://` 被当作"远程形态"透传，重新打开 I-A 守卫封死的本地读取外传面

- **位置**：`video-claw/video-claw/backend/api/routers/sandbox.py` L212、L225-226

```python
212: _REMOTE_MEDIA_PREFIXES = ("http://", "https://", "data:", "file://", "oss://")
...
225:     if value.startswith(_REMOTE_MEDIA_PREFIXES):
226:         return value
```

- **问题**：`file://` 对本仓库多个适配层**不是远程形态，而是本地读取形态**（撰写者已逐一核实）：
  - `models/custom_common.py` `to_data_url` L162-166：`file://` 剥前缀后本地 `open(local_path, "rb")`
    再 base64 外传；
  - `models/vlm_client.py` L157-158：`file://` 直接进 `file_urls` 交给 DashScope SDK
    （`vlm_dashscope.py` L40 注释自证 SDK 会处理 `file://` 本地路径；`image_client.py` L276 注释
    "DashScope SDK (via MultiModalConversation) handles file://" 同证）；
  - `models/image_client.py` L279-280：DashScope 分支同样把 `file://` 原样交给 SDK。
- **影响**：零鉴权下 `POST /api/sandbox/vlm {"images":["file:///root/.ssh/id_rsa"], ...}` 即任意本地
  文件读取 + 上传外传，与上轮 I-1 堵掉的 `/etc/passwd` 绝对路径**完全同构**。上轮报告 I-2 修复说明中
  "远程形态不经本地 open，无越界读取面"的断言对 `file://` 不成立——其引用的证据 `custom_common.py`
  恰是一个会本地 open `file://` 的函数。
- **修法**：从 `_REMOTE_MEDIA_PREFIXES` 移除 `"file://"`；若确需支持 `file://` 输入，先剥前缀解析出本地
  路径再走 `resolve_within_allowed_dirs` 白名单（与裸路径同口径）。同步修正
  `verify_sandbox_path_guard.py` 的远程形态断言组（当前只测了 `https://` 与 `data:`，`file://` 透传
  恰在守卫层"绿"而适配层是洞）。修复后必须在既有 `video-claw-backend` 容器内复跑守卫套件取证。

### Important（应当修）

#### I-1　"远程形态透传"只验证到守卫层：i2i 走自定义图片模型时 URL 输入实际不可用

- **位置**：`video-claw/video-claw/backend/models/custom_image.py` L92-111、L121-150

```python
 94:        for field_name, mode in self._edit_field_variants():
 95:            try:
 96:                response = self._post_edit(...)   # mode=="file" 时 open(path, "rb")
107:            except httpx.HTTPError as exc:        # FileNotFoundError 不在其中
```

- **问题**：`_post_edit` 的 file 模式对 `https://...` 直接 `open()`，抛出的 `FileNotFoundError`
  （`OSError` 子类）不被 `except httpx.HTTPError` 捕获，直接冒泡终结整个请求——sglang/vllm-omni 的
  `("url", "dataurl")` 回退分支（`to_data_url` 对 http(s) 原样返回，本可正确处理 URL）**永不可达**。
  另 openai 协议的 `_edit_field_variants`（L114-115）只有 file 模式、连回退设计都没有。
  结果：i2i 选自定义图片模型 + URL 输入模式 → `No such file or directory: 'https://...'`。
- **为何重要**：`fa78e83` 与上轮报告声称"适配层本就支持"远程形态，但 `verify_sandbox_path_guard.py`
  的远程形态组用的是 **patch 掉适配层的 CAPTURED mock**——只证明守卫放行，未证明适配层可用。这正是
  本项目反复踩的"守卫层自洽假绿"变体（与上轮 I-2 漏网同因：只验拒绝/放行路径，不验合法输入的
  端到端正向行为）。
- **修法**：`_post_edit` 在 file 模式前先分流远程形态（http(s)/data: 直接走 dataurl/url 分支），或至少
  把 `except` 扩为 `(httpx.HTTPError, OSError)` 使回退可达（openai 协议需补 url 变体）；并补一条
  **不打 mock** 的适配层级 URL 用例。

#### I-2　改写条目草稿随产物引用更新被全量清空，后台单条重生成期间会丢用户未保存编辑

- **位置**：`video-claw/video-claw/frontend/components/stages/PromptRewriteStage.tsx` L104-115

```ts
104:  const items: RewriteItem[] = useMemo(
105:    () => (Array.isArray(artifact.items) ? artifact.items : []),
106:    [artifact.items],
107:  );
...
113:  useEffect(() => {
114:    setDrafts({});
115:  }, [items]);
```

- **问题**：`items` 随每次 artifact 更新产生新引用；`pollForCompletion` 的回填
  （`WorkflowPanel.tsx` L227-230 `update.artifact = currentArtifact`）与 SSE 增量事件都会产生新引用 →
  `setDrafts({})` 全量清空**所有**条目的草稿。后台单条重生成期间阶段非 running、`isRunning=false`，
  textarea 可编辑——用户在**其他条目**里的未保存草稿会在重生成完成回填时被一并清掉。
- **备注（撰写者核实修正）**：评审者原表述为"每 2 秒清一次"。经核实，因条目级 running 事件尚未实施
  （`prompt-rewrite-item-running-events` 变更未落地），`hasRunningArtifactItems` 恒为 false，轮询会在
  首个 2s 周期检测到"阶段非 running 且无 running 条目"后退出——**当前实际为一次性清空**而非周期性；
  但条目级 running 落地后将转为周期性清空，风险只会加重。核心问题（可编辑窗口内全量清草稿）不变。
- **为何重要**：openspec design 声称"不存在'用户编辑被覆盖'的窗口"（依据是执行期 `isRunning` 禁用），
  但后台重生成路径恰好落在该论证之外；且 `drafts` 的"切换/重生成后以最新产物为准"意图也只需重置
  **被更新条目**。
- **修法**：`setDrafts` 改为按条目 id 增量清理（仅当该条 `rewritten_prompt` 实际变化时清除其草稿），
  或以内容签名而非数组引用作为 effect 依赖。

### Minor（可改可不改）

| 编号 | 问题 | 位置 | 说明 |
|---|---|---|---|
| M-1 | Seedream 适配层静默丢弃 `file://` 与不存在的参考：既非 `http` 前缀、`os.path.exists` 又为 False 时被直接跳过，违反"不得静默丢弃"精神 | `models/image_seedream.py` L183-194 | 至少记一条 warning/adjustment |
| M-2 | 设置页 `tasks` 输入为自由文本，填入 `t2va/fl2va/ref2va` 之外的值会被 `custom_video._supported_tasks`（L384）静默过滤为空 = 等于未声明，用户无感知 | `components/settings/CustomModelsSection.tsx` L563-564 | 加 pattern 提示或下拉多选 |
| M-3 | `withSlotPlaceholder` 每次值变化都打一次 `/api/model_availability`，无缓存；首页 + 顶栏共 10 个实例，切换模型即一轮请求 | `modelRegistry.ts` L70-103 | 复用 `videoCapsCache`（L52-63）同型 TTL 缓存 |
| M-4 | `preserveRewriteBodies` 只按 `nextVal` 的 id 集合映射，存在毫秒级"条目闪没"窗口：轮询请求飞行中 SSE 刚追加的条目会被整体覆盖，下一轮轮询（2s）自愈 | `lib/assetMerge.ts` L118-137 | 如需闭环，合并时把 `prev` 有而 `next` 缺失的条目按 id 补回 |

---

## 三、上轮遗留项核实表

| 项 | 声称 | 核实结果 |
|---|---|---|
| 上轮 I-1（守卫扩至 i2i/vlm） | 已修 | **兑现**：sandbox.py L347-351 / L429-433 / L483-484，三端均守卫且先于任务启动 |
| 上轮 I-2（远程形态透传） | 已修 | **守卫层兑现**（L212/L225-226），但 `file://` 透传本身构成本轮 C-1 漏洞；适配层正向支持未验证（本轮 I-1） |
| 上轮 M-1/M-2（models_filter） | 已修 | **兑现**：永真式已删、谓词 + 合成自检 + 漂移校核均在（见优点 2） |
| 上轮 M-3（input_data 口径） | 已修 | **兑现**：L488-498 存守卫后规范化值 |
| 上轮 M-4（.env.example 换行） | 已修 | **兑现**：文件末尾有终止换行 |
| 上轮 M-5（capsReady 悬挂） | 维持现状（已拍板） | 未动码，符合拍板结论 |

## 四、openspec 验收完成度对照

| 项 | 状态 |
|---|---|
| `prompt-rewrite-incremental-items` tasks 1-4、5.1-5.3、6.x | 与源码一致（事件字段、shell 不落正文、双通道合并、渲染兜底、文档），验收脚本在库 |
| 同上 tasks 5.4-①（条目级 running 事件） | 未覆盖，如实登记另案 `prompt-rewrite-item-running-events`，合规 |
| 同上 tasks 5.4-②（关闭态 + 持久化 running） | FAIL 不修，如实登记另案 `workflow-poll-timeout-visibility`，合规 |
| `904e8a5` / `daea6bb` vs 两个 video openspec | 未越界：未声明 `tasks`/caps 时请求与既有实现完全一致；short_edge 声明值优先、用户显式值仅未声明时生效（`custom_video.py` L250-267） |

---

## 五、建议（Recommendations）

1. **"透传类"修复必须补适配层端到端正向用例**：本轮 C-1/I-1 的共同根因是守卫层与适配层之间的契约
   只靠文档断言维系，mock 验证无法暴露。建议在 `verify_sandbox_media_refs.py` 增加一组**不打 mock** 的
   "合法输入形态"用例（http/data/本地路径各一，按协议分派），并把"`file://` 等价于本地路径"写进
   `backend/docs/api.md` 的守卫约定。
2. **`_REMOTE_MEDIA_PREFIXES` 应按"消费方是否本地 open"分类**，而不是按字符串前缀：`file://` 在本仓库
   三个消费点都是本地读取形态。建议将该常量的注释改为"仅限适配层确认不落盘读取的形态"，并在
   `to_data_url`/dashscope 路径加防御性断言。
3. **前端草稿状态与产物对象身份解耦**（I-2）：`drafts` 的生命周期应绑定"条目内容变化"而非"产物对象
   引用变化"——这也是后续 `prompt-rewrite-item-running-events` 落地后会再撞一次的坑，建议在该变更的
   tasks 中预挂此验收。
4. 流程上延续上轮教训：**提交信息承诺与 diff 逐条对齐**。"远程形态原样透传（适配层本就支持）"这句
   在本轮被证明是半真半假——若当时按建议补了正向断言，C-1/I-1 会在提交前暴露。

---

## 六、结论（Assessment）

**Ready to merge?** **No**（需先修 C-1；I-1/I-2 应同批或紧随）

**理由**：本轮主体工作（增量改写事件、模型选择器兜底、partition 对齐、测试整改）实现质量与 openspec
契约吻合度高、验收脚本真实可信；但 `file://` 透传在零鉴权端点上重新打开了 I-A 守卫本应封死的任意本地
文件读取 + 外传面（Critical），且"远程形态透传"在自定义图片模型的 i2i 路径上实际不可用，属承诺与实现
不符——两者同源于一个验证盲区，修复面小（一个前缀列表 + 一个异常分流），但必须先行。

**修复验证要求**（按项目 Docker 测试规则）：C-1/I-1 的修复与验证必须在既有 `video-claw-backend` 容器内
执行（复用既有镜像与 `tmp_verify/` 脚本，不新建测试镜像），前端项（I-2/M-3/M-4）按既有前端流程
（`npx tsc --noEmit` / `npm run lint` / 浏览器取证）验证。
