# VideoClaw 提交区间代码评审报告（第五轮）

- **评审日期**：2026-10-19
- **评审范围**：`9bb51fe..2f720f8`（等价于 `452e96d` 及之后的 23 个提交；评审基准 HEAD `2f720f8`，评审时工作区干净）
  - `452e96d` docs(review)：第四轮区间评审报告入库并登记索引
  - `f2b83fb` fix(review-followup)：落实第四轮评审 C-1/I-1/M-1~M-3 修复与报告修订
  - `a051bc6` fix(script)：智能续写 is_zh 未绑定致 UnboundLocalError
  - `655c519` chore(lint)：消除前后端 error 级 lint 违规（ruff 231 / eslint 164 → 0）
  - `c2ac17c` feat(frontend)：品牌更名为交心-Video 并更换 Logo 为 jtyh.png
  - `1544a76` fix(build)：前端构建期端口改从 .env 注入，修复代理目标硬编码缺陷
  - `d94f4ad` chore(frontend)：删除零引用孤儿资源 logo.jpg
  - `4ac88cb` / `47e9b40` / `7d38b3e` docs(handoff)：Handoff 5 故障记录、复现命令与实施结果
  - `41dcf94` fix(build)：构建脚本如实标注端口来源并对默认值兜底告警
  - `9fffaaf` docs(build)：前端镜像构建入口统一指向 build-frontend.sh
  - `dfc833e` fix(build)：前端构建缺端口即失败，去掉 8000 静默兜底
  - `5915fcf` fix(video)：sglang 媒体引用改为服务端可拉取的 HTTP 形态，含图首选 conditions
  - `39d0bdf` fix(frontend)：首页文档上传改同源相对路径并补 upload_file rewrite
  - `04ca333` fix(frontend)：会话态按归属令牌隔离，切换/回主界面不再残留上一会话界面
  - `17306de` feat(script)：剧本阶段重构为介绍/宣传/营销三类宣传片导向
  - `e6d49af` docs(frontend)：寻址注释改引 capability 名
  - `01d0f57` fix(video)：视频阶段模型不可用失败可判别并触发兜底引导
  - `0ec7a1f` fix(rewrite)：改写阶段草稿按条目内容保留、合并补回缺失条目
  - `5446e6f` feat(rewrite)：改写阶段条目级「改写中」事件与进行中态呈现
  - `a8ac6b1` fix(workflow)：重启后陈旧 running 按归属凭据归一为可重做终态
  - `2f720f8` fix(workflow)：轮询耗尽进入可辨识状态同步超时态并提供恢复动作
  - 规模：115 files，+4450/−1123
- **对照基准**：`docs/reviews/code-review-eaa012a-to-9bb51fe.md`（第四轮 C-1/I-1/I-2/M-1~M-5 验收口径）、
  `openspec/changes/` 下本次涉及的变更（frontend-session-state-isolation、script-stage-promo-type-rework、
  video-stage-model-unavailable-fallback、video-audio-video-references、prompt-rewrite-draft-merge-preservation、
  prompt-rewrite-item-running-events、workflow-stale-running-recovery、workflow-poll-timeout-visibility 等）
- **评审方式**：子代理派发受环境频率限制不可用，改为撰写者在本线程直接做只读静态评审并分段落盘。
  **本次未执行任何测试或构建**，结论均来自源码与 git 历史交叉核实；涉及运行行为的判断在条目内注明依据来源
  （提交说明中的实测记录 / 验证脚本断言 / 源码可推导）
- **前轮报告**：`docs/reviews/code-review-eaa012a-to-9bb51fe.md`（第四轮）、
  `code-review-3ac13de-to-eaa012a.md`（第三轮）、`code-review-07b4bdb-to-3ac13de.md`（第二轮）、
  `code-review-6ba2f19-to-cdea2a1.md`（第一轮）
- **编号约定**：本报告问题编号为本轮新发现（C-1/I-1/M-1/…）；引用上轮问题时标注"上轮"

> **修订记录（2026-10-20）**：依据外部复审逐条回源，补记 C-1 修法的"允许根须排除 `code/data`"与别名覆盖要求；
> 新增「六、评审后复核与补充发现」：S-1（`project/start.file_path`，已并入守卫变更）、
> S-2（`upload_image.item_id` 路径穿越写入，已落成独立变更 `artifact-upload-path-safety`）；校正 C-1 中 `schemas/pipelines.py` 的行号漂移
> （L36-39 / L50-53）。C-1/I-1 结论与 "No" 判定不变。
>
> **二次修订（2026-10-20，同轮）**：S-2 措辞收紧为"受限命名模式写入，不构成任意文件覆盖"；三份修复变更同步第二轮口径
> （别名验证下沉守卫单测、`copy_input_file` 使用守卫返回值、capability 改名 `user-input-path-guard`、视频矩阵不含图片侧、
> M-6 覆盖 `--workers`）。C-1/I-1 结论与 "No" 判定不变。

## 修复更新（2026-10-20）

| 项 | 状态 | 提交与验证证据 |
|---|---|---|
| C-1 `/api/pipelines/*` 任意文件复制 + `/code` 暴露面 | 已修复 | `5a7b5bc`；允许根收窄为 `CODE_DIR/result`+`TEMP_DIR`，共享守卫 `api/media_paths.py`；`verify_pipeline_path_guard.py` 49 项 ALL PASS，既有 sandbox/media-ref 四套件 ALL PASS；`/code/data/*` 404 |
| S-1 `project/start.file_path` | 已随 C-1 收口 | `5a7b5bc`；`guard_uploaded_document_name` + join 后 realpath；HTTP 非法值 400 |
| I-1 custom_video 远程参考 | 已修复 | `d4f5d6c`；协议能力矩阵 + `_reference_uris` 远程单形态 + `OSError` 回退；`verify_media_ref_uri` 新增 15 项、多图用例 12 项 ALL PASS |
| M-1/M-4/M-5/M-6/M-7 | 已修复 | `8e41fed`；共享分隔符夹具、宿主构建双重断言、确定性别名匹配、单进程前提校验、阶段化兜底文案；容器内/前端验证见 `review-fifth-minor-followups/VERIFICATION.md` |
| S-2 `upload_image.item_id` / `session_id` | 已修复 | `53fb947`；标识白名单 + 读写装载删除复用 + realpath 预检/复核；`verify_upload_path_safety.py` 36 项 ALL PASS，存量 17 个会话/条目 ID 扫描无不合法项 |
| M-3 openai `url` 变体服务端接受性 | **未取证（保持未关闭）** | 无可用 openai 兼容端点；代码注释与 `custom-adapter-remote-inputs/VERIFICATION.md` 已标注未取证 |
| 浏览器取证（拼装后的真实前端） | 部分完成 | 首页/临时工作台/一键 Pipeline 均无白屏与控制台 error，`/code/` 请求无 404/403（截图 `tmp_verify/verify_33_*.png`）；唯一视频为 44 字节测试残留导致解码失败，与挂载无关。5.3 弹窗与 3.2 三类上传浏览器实测尚待构造场景 |

---

## 一、优点（Strengths）

1. **上轮 C-1/I-1/M-1 的修复真实落地，且补上了"守卫层自洽假绿"的验证盲区**
   `sandbox.py` L212-223 将 `file://` 移出远程前缀并新增 `_strip_file_uri`（含 `/C:/` Windows 形态），
   L239-240 剥前缀后与裸路径同口径走 `resolve_within_allowed_dirs` 白名单；
   `verify_sandbox_path_guard.py` 新增 #21/#22 两例（越界 `file:///etc/passwd` 拒绝且不触适配层、
   白名单内文件剥前缀后按 realpath 透传），断言测的是守卫的真实返回值而非自证。
   `custom_image.py` L94-98 在进入变体循环前按输入形态分流（远程输入只保留 `dataurl` 变体），
   L112-115 把 `except` 扩为 `(httpx.HTTPError, OSError)` 使 file 模式失败后可回退，
   L120-127 为 openai 协议补 `("url","dataurl")` 变体；`verify_custom_image_edit_remote.py` 的 8 条断言
   直接实例化 `CustomImageClient`（只替换 `_client.post`），能真实区分"尝试 open URL"与"走表单字段"。
   `image_seedream.py` L198-200 对被跳过的参考图显式 warning，不再静默丢弃。

2. **`0ec7a1f` 对上轮 I-2/M-4 的修复方式是正确的根治而非缩小窗口**
   `assetMerge.ts` L165-183 抽出纯函数 `pruneDraftsByContent`：以 `id + rewritten_prompt` 内容签名判定
   是否清草稿，`truncated/status` 变化不误清，无变化时返回入参引用以避免多余渲染；
   `PromptRewriteStage.tsx` L124-134 的渲染期同步改为按内容签名触发，不再依赖 `items` 数组引用；
   `preserveRewriteBodies` L141-160 把 prev-only 条目按 id 补回尾部，直接消除 M-4 的"条目闪没"窗口。
   验证脚本 `verify_frontend_asset_merge.mjs` 直接 `import` 出货实现，新增 #9-#13 覆盖五类边界（含"补回不重复、
   保持 next 顺序"），并与组件共用同一份纯函数（无复制实现）。

3. **`5446e6f` 条目级 running 事件的契约干净**
   `prompt_rewrite_agent.py` L561-583 在进入 `_rewrite_one`（含 1+2 次重试环）之前只发一次 running，
   事件体显式剔除 `rewritten_prompt/truncated`；落盘条件在 `orchestrator.py` L1113-1118 仍只认
   `done/failed`，running 天然不写盘（不放大写盘、不持久化易失运行态）；重试在单条内部完成，
   结构上保证"一条恰好 running→终态、不抖动"。

4. **`a8ac6b1` 把"执行所有权"变成了可持久化、可判定的凭据**
   `orchestrator.py` L189-192 在 `_load_sessions_from_disk` 之前生成 `_boot_id`；
   L1124-1146 在 `_state_lock` 临界区内与 `status=running` 同批写入并原子落盘；
   L262-290 的 `_recover_stale_running` 只修正"running 且 boot_id 不符"的阶段，产物/versions/selected 不动、
   不判失败，三处终态出口均清空 boot_id；启动批量（L1741-1744）与懒加载（L244-245）两个入口接线一致且幂等。
   验证脚本用临时目录隔离真实会话，断言包含"重复恢复 changed=False、写盘字节数不变、在途执行与后台重生成不误伤"。

5. **`2f720f8` 的超时态设计与既有状态机正交，恢复动作完整**
   `stages/types.ts` L10-15 新增 `syncState?: 'synced'|'timed_out'` 而不改 `StageStatus` 枚举，避免波及所有状态分支；
   `WorkflowPanel.tsx` L349-475 在轮询入门置 `synced`、耗尽后在 `owns(token)` 下写 `timed_out`（保留后端最后状态、
   不判失败/完成）；L1793-1816 的横幅提供「重新查询/停止任务」，`handleTimeoutStop` 先探活失败则保留横幅并给错误反馈
   （不静默清除）；TopBar 用"同步超时"角标替换"执行中"，口径明确。

6. **`01d0f57` 的视频失败可判别链路在"现场—事件—产物"三层闭合**
   `video_agent.py` L100-196：`_generate_one` 返回四元组，异常在捕获现场用共用判定
   `classify_model_unavailable` 分类，取消/缺参考图/缺输入图各有独立非模型类别；
   L498-510 只在片段仍为 failed 时把 marker 拷回 `_build_payload` 条目（成功片段字段集不变）；
   `workflow.py` L45-54/L94-96 抽出 `_active_video_model_field`，使预检报错的 field 与实际取值来源
   （含 legacy `video_model` 回退）严格一致；前端 `WorkflowPanel.tsx` L800-830 改为遍历五类容器，
   `ModelFallbackDialog.tsx` L24/L44/L56/L93 按阶段分流（视频阶段不出现「上传图片」），
   `VideoStage.tsx` L212-224 只对 failed 且带 error 的条目展示原因。

7. **`04ca333` 的会话归属令牌机制是完整的"单一写入口"改造**
   `WorkflowPanel.tsx` L115-123/L240-256 定义 `{sid, gen}` 令牌与 `owns` 双等判定（解决 A→B→A 迟到响应）；
   L286-321 三个漏斗集中所有会话态写入，全文件已无绕过漏斗的 `setStageStates/setGlobalStatusMap/setSessionId`；
   L328-346 `resetSessionView` 为四个退出路径共用，且明确不 abort SSE、不调 stopProject（避免杀死后端在跑阶段）；
   L1790 以 `key={sessionId}` 重建阶段子树，隔离 `drafts/editDescs` 等以条目 id 为键的本地态；
   L351-353 补的轮询入口去重守卫修掉了"重复调用叠加并发循环"。

8. **`5915fcf` 把"跨机可用"从文档断言变成了可回退的双形态**
   `custom_video.py` L495-510 仅在文件位于 `CODE_DIR` 内且真实存在时生成 `{base}/code/<相对路径>`
   （realpath 前缀校验阻断相对路径逃逸），file:// 保留为第二形态依次回退；
   L790-801 将含图请求的 conditions 提为首选（修正原先 multipart 首选导致"成功但生成与输入无关视频"的反序），
   并在 L521-538 给出区分"未配置基址/已配置但不可达"的可执行提示。`verify_media_ref_uri.py` 覆盖
   两种形态顺序、非 sglang 协议不注入 conditions、无关错误不追加提示等 20 项。

9. **构建链收口彻底，事故模式被写进代码约束**
   `build-frontend.sh` L19-49 用 `set -euo pipefail`，环境变量与 `.env` 都取不到 `BACKEND_PORT` 时
   `exit 1` 并打印后果与修复指引，不再静默回退 8000；`Dockerfile.frontend` L32-33 在 COPY 之前断言
   `ARG BACKEND_PORT` 非空，并保留 `--target deps` 不受影响的说明；README/compose/Handoff 5 同步更新。

10. **`655c519` 的大面积 lint 整改可核查**
    抽查后端 `models/*`、`pipelines/*` 与前端 `workflowApi.ts`/`WorkflowPanel.tsx`：改动集中在
    `# noqa: E402` 标注、导入排序、`any → unknown`/显式类型、死变量删除（如 `image_urls`、
    `asset_chars_str/asset_sets_str`、`style_map_zh`，均已确认全仓无引用）与换行格式，
    未发现借 lint 之名改变控制流的地方。

---

## 二、问题（本轮新发现）

### Critical（必须修）

#### C-1　`/api/pipelines/*` 仍可通过用户传入的本地路径把任意文件复制进公开 `/code` 目录并外传

- **位置**：
  - `backend/api/schemas/pipelines.py` L36-39（`ActionTransferPipelineRequest` 的 `image_path`/`video_path`）、L50-53（`DigitalHumanPipelineRequest` 的 `character_image_path`/`goods_image_path`）为自由字符串；
  - `backend/api/routers/pipelines.py` L276-289（三个端点与泛化端点均直接 `_start_task`，无路径守卫）；
  - `backend/pipelines/utils.py` L86-94 `copy_input_file`：`file://`、`http(s)`、`data:` 原样返回，其余路径
    `src.exists()` 后 `shutil.copy2(src, output_dir/prefix+suffix)`；
  - `backend/pipelines/action_transfer.py` L29-30、`backend/pipelines/digital_human.py` L59-61 直接调用；
  - `backend/pipelines/storage.py` L21-22/L35-36：`TASK_RESULT_DIR = RESULT_DIR/task`，而
    `backend/api/app.py` L54 把整个 `CODE_DIR` 挂到 `/code` 静态目录。
- **问题**：零鉴权下 `POST /api/pipelines/action_transfer/tasks`（或泛化的
  `POST /api/pipelines/{pipeline}/tasks`）传 `{"image_path": "/app/config.yaml", ...}` 即可：
  1. 后端把 `config.yaml` 复制到 `code/result/task/<task_id>/input_image.yaml`；
  2. 响应已返回 `task_id` 与 `output_dir`，攻击者按命名规则访问
     `/code/result/task/<task_id>/input_image.yaml` 直接取回文件内容；
  3. 该文件同时会被交给图像/视频适配层上传给第三方模型服务（数据外传）。
  复制发生在模型调用之前，**即使后续生成失败文件也留在公开目录**（`runner.py` L18-33 失败仅标记任务失败）。
  `file://` 前缀还会绕过 `src.exists()`/白名单，直接进入适配层的 `open()`（如 DashScope 分支）。
- **影响**：可把**任意进程可读文件**（`/app/config.yaml`、`/app/.env`、`/root/*` 等不在 `/code` 目录内的文件）
  搬运到公开目录并取回，同时外传给第三方模型服务，与上轮 C-1 在 sandbox 端点堵掉的面**完全同构**。
  上轮 C-1 的修复范围只覆盖 `sandbox.py`，pipeline 端点未纳入。
  （另注：`/code` 直接挂载整个 `CODE_DIR`，本身已使 `code/data/sessions/*.json` 可被匿名读取，属既有面，
  建议借本次修复一并收窄。）
- **备注（归属）**：该缺陷**非本区间引入**（`copy_input_file` 早已如此），但本轮 `f2b83fb` 声称完成
  "零鉴权本地读取面"收口，实际只收口了 sandbox；因此计入本轮问题并建议同批修复。
- **修法**：把 `_guard_media_input`（或抽出的公共 `resolve_within_allowed_dirs` + 远程形态分类）
  提升为共享模块，在 pipeline 参数进入 `copy_input_file` 前统一校验；**允许根必须排除 `CODE_DIR/data`**
  （只保留 `CODE_DIR/result` 与 `TEMP_DIR`），否则会话/任务元数据仍可经复制通道进入公开目录；`file://`
  一律剥前缀后按本地路径走白名单；校验键集覆盖实际别名（`image_asset` 等）；`/code` 静态目录建议只暴露
  `result/`，不要挂整个 `CODE_DIR`。修复后必须在既有 `video-claw-backend` 容器内补**不打端点 mock**的
  越界拒绝用例（`/etc/passwd`、`file:///app/config.yaml`、`code/data/sessions/*.json`、`../` 各一）。

### Important（应当修）

#### I-1　自定义视频适配层仍不支持 http(s) 参考输入：`open()` 抛 OSError 且不在捕获范围内

- **位置**：`backend/models/custom_video.py` L886-889（`_post_create` 对 `field_spec` 直接 `open(path)`）、
  L700-709（`_try_sync_generate` 只 `except httpx.HTTPError`）、L804-831（`_create_job` 同）；
  L426-434 `_media_reference_paths` 不做形态归一；L495-510 `_reference_uris` 对非本地路径会生成
  `file://` + `abspath("https://…")` 的错误 URI。
- **问题**：`sandbox.py` L231 的守卫注释仍声明"http(s)/data:/oss:// 形态不读本地盘，原样交给适配层
  （图片/视频参考的 URL 输入模式依赖此路径）"。但 `CustomVideoClient` 对参考图片/视频只走文件句柄：
  `POST /api/sandbox/video` 传 `reference_videos: ["https://example.com/a.mp4"]` + 自定义模型时：
  sglang 协议会先以 `file://` + `abspath("https://…")` 的错误 URI 试 conditions，再落到 multipart
  `open("https://…")`；vllm-omni/openai 则直接进入文件句柄分支。两条路径最终都抛 `FileNotFoundError`（OSError），
  该异常不在 `except httpx.HTTPError` 内，直接冒泡成 500/失败条目，错误信息也不是"输入形态不支持"。
- **为何重要**：这与上轮 I-1（custom_image URL 输入）同族——上轮只修了图片适配层，视频侧既未修也**没有对应的
  不打 mock 正向用例**；`verify_media_ref_uri.py` 只验证本地文件的 URL 拼装，未覆盖"参考输入本身是 URL"。
- **修法**：`_image_file_specs`/`_post_create` 与 `_try_sync_generate` 在进入文件模式前先按
  `http(s)/data:` 分流（能直接作为 `url`/`conditions[].uri` 的形态就透传，不能则明确报"该协议不支持 URL 输入"）；
  把 `except` 扩为 `(httpx.HTTPError, OSError)` 保证错误可读；同步修正 `_reference_uris` 对远程形态的断言，
  并补一条 URL 参考输入的适配层用例。

### Minor（可改可不改）

| 编号 | 问题 | 位置 | 说明 |
|---|---|---|---|
| M-1 | 前端 `tasks` 校验接受中文逗号与竖线分隔符，后端只按英文逗号/竖线拆分（中文逗号不识别），校验通过但值被静默过滤 = 等同未声明 | `CustomModelsSection.tsx` L75-83；`custom_video.py` L376-383 | 前后端共用一套分隔符口径，或由后端下发合法枚举 |
| M-2 | `custom_image._edit` 对"混合输入"（既有 http(s) 又有本地文件）统一走 `dataurl` 变体，而 `_post_edit` 的 dataurl 分支只发送 `image_paths[0]`，其余图被静默丢弃 | `custom_image.py` L94-98、L143-146 | 若产品不允许多图混合，至少显式拒绝；若允许则 dataurl 需支持多值 |
| M-3 | openai 协议的 `("url","dataurl")` 变体的服务端接受性未验证（测试只 mock `_client.post`，断言到"发出了什么"而非"服务端接受"） | `custom_image.py` L120-123；`verify_custom_image_edit_remote.py` L76-135 | 注释已写"多数兼容服务接受"；建议在真实 openai 兼容端点上补一次正向取证 |
| M-4 | "缺端口即失败"只覆盖 `./build-frontend.sh` 与 Dockerfile；宿主机 `npm run build` 仍由 `next.config.ts` 静默回退 8000 | `next.config.ts` L3-6；`build-frontend.sh` L33-43 | README 已要求宿主构建显式带环境变量，但代码层仍可复现同类事故；可给宿主构建加同样的断言脚本 |
| M-5 | `_normalize_film_type` 的包含匹配按字典枚举顺序而非语义优先级，"营销宣传片"会先命中"宣传" | `script_agent.py` L108-123 | 多类型词并存时按"更长/更具体别名优先"排序 |
| M-6 | `boot_id` 判据隐含"后端单进程"前提；若以 `uvicorn --workers N` 启动，其他 worker 加载会话时会把正在执行的阶段判为陈旧并置 stopped | `orchestrator.py` L189-192、L262-290 | 当前 `api_server.py` L20 为单进程，暂无实际影响；建议在 design/部署说明中写明该前提，或改用进程锁/心跳 |
| M-7 | `maybeShowFallbackFromArtifact` 也扫描 `items`，而 `stageModelField` 对 `prompt_rewrite` 返回空串，文案会落到"请更换可用的视频模型"分支 | `WorkflowPanel.tsx` L800-830、L780-798 | 当前改写条目不产生 `error_type`，不会触发；属防御性偏差，建议按 stage 给出兜底文案 |

---

## 三、上轮遗留项核实表

| 项 | 声称 | 核实结果 |
|---|---|---|
| 上轮 C-1（`file://` 本地读取形态） | 已修 | **sandbox 端兑现**：`sandbox.py` L212-223/L239-240、验证 #21/#22 均在；但同类面在 `/api/pipelines/*` 未收口（本轮 C-1） |
| 上轮 I-1（custom_image URL 回退不可达） | 已修 | **兑现**：分流 + `OSError` 捕获 + openai url 变体 + 适配层用例（`verify_custom_image_edit_remote.py`）；但视频适配层同型缺陷未修（本轮 I-1） |
| 上轮 I-2（改写草稿被全量清空） | 已修 | **兑现**：`pruneDraftsByContent` 内容签名清理（`assetMerge.ts` L165-183、`PromptRewriteStage.tsx` L124-134） |
| 上轮 M-1（Seedream 静默丢弃） | 已修 | **兑现**：`image_seedream.py` L198-200 记 warning |
| 上轮 M-2（tasks 自由文本静默过滤） | 已修 | **兑现**：黄字提示合法集合；存在中英文逗号口径偏差（本轮 M-1） |
| 上轮 M-3（可用性查询无缓存） | 已修 | **兑现**：`modelRegistry.ts` L69-77 10s TTL，异常不缓存 |
| 上轮 M-4（条目闪没窗口） | 已修 | **兑现**：`preserveRewriteBodies` 补回 prev-only 条目（`assetMerge.ts` L141-160） |
| 上轮 M-5（capsReady 悬挂） | 维持现状（已拍板） | 本区间未动码，符合拍板结论 |

---

## 四、openspec 验收完成度对照

| 变更 | 状态 |
|---|---|
| `frontend-build-port-fail-fast` | 已归档；脚本/Dockerfile/README 三处一致，无 8000 默认值 |
| `home-file-upload-relative-proxy` | 已归档；`upload_file` 走同源相对路径 + 精确 rewrite |
| `frontend-session-state-isolation` | 38/39；5.2（`ModelFallbackDialog` 上传滚动端到端）未勾，如实登记 |
| `script-stage-promo-type-rework` | 4.2（Docker 内三类灵感端到端目视）未勾，如实登记；判定/回退/归一化有容器外验证脚本覆盖 |
| `video-stage-model-unavailable-fallback` | 19/20；5.5（服务可用时成功产物端到端）未勾，如实登记，且未用契约级验证冒充端到端 |
| `video-audio-video-references` | 6.3（真实部署图+音频/多视频混合）未勾，如实登记 |
| `prompt-rewrite-draft-merge-preservation` / `prompt-rewrite-item-running-events` / `workflow-stale-running-recovery` / `workflow-poll-timeout-visibility` | 均已归档，tasks 与源码/验证脚本一致 |
| `prompt-rewrite-incremental-items` | 5.4 仍保留未勾（历史遗留），符合"允许如实记为未覆盖" |
| `provider-live-reachability-check` | 提案在库、未实施；不属本区间，未计入 |

---

## 五、建议（Recommendations）

1. **把"用户可控路径"的校验收敛为单一共享层**：sandbox 已证明"按端点各自加守卫"必然遗漏（本轮 C-1 就是
   遗漏的 pipeline 端点）。建议把"远程形态分类 + realpath 白名单 + 存在性校验"抽成公共函数，
   在 `/api/pipelines/*`、`/api/sandbox/*`、上传/产物替换等所有入口统一调用，并配一组
   **不打端点 mock** 的越界/合法用例矩阵（http、data、file://、绝对越界、`..`、符号链接、白名单内文件）。
2. **"透传类"能力按消费方声明，而不是按 URL 前缀想象**：`_REMOTE_MEDIA_PREFIXES` 的教训已在图片侧验证，
   视频侧（custom_video）仍把 URL 输入当成已支持。建议为每个适配器显式声明 `input_forms`，
   守卫只放行"该适配器声明支持"的形态；未声明的形态在入口就返回 400 并说明原因。
3. **把单进程前提写进设计文档**（`boot_id` 判据），或在代码里用进程锁/心跳替代纯进程标识，
   避免未来改为多 worker 时把正常执行误判为陈旧。
4. **前端校验与后端解析共用一套口径**（tasks 分隔符/大小写/枚举），避免"前端绿、后端静默忽略"。
5. **提交信息与承诺范围对齐**：本轮"C-1 收口"实际限定 sandbox、"缺端口即失败"实际限定 Docker 构建路径，
   建议在提交信息中写明边界，或在同类入口一并收口——这仍是本项目历史上反复出现的偏差模式。

---

## 六、评审后复核与补充发现（2026-10-20）

外部复审对报告逐条回源，事实性结论、规模数据与验收完成度均一致；据此修正了三份修复变更的 6 处实施缺口：
① 允许根未排除 `CODE_DIR/data`（复制通道残留）；② `copy_input_file` 远程集合缺 `oss://` 且 `file://` 提前返回；
③ pipeline 校验键集缺 `*_asset` 别名、设计与任务不一致；④ sglang `conditions[].type` 对所有 refs 硬编码 `image`；
⑤ `boot_id` 前提检查只覆盖 `api_server.py`、漏 `uvicorn --workers`；⑥ M-5 的"无法唯一确定"回退条款不可达。

### S-1（Important，已并入 `pipeline-input-path-guard`）`POST /api/project/start` 的 `file_path` 可读任意文档

- **位置**：`api/schemas/project.py` L8（自由字符串）、`api/routers/files.py` L135-151（`merge_uploaded_file_into_idea`）
- **事实**：`os.path.join(TEMP_DIR, file_path)` 在 `file_path` 为绝对路径时被后者覆盖，`../` 亦可逃逸；
  `FileReader.extract_text` 按 `.docx/.doc/.txt/.md/.pdf` 分派读取，内容被拼进 idea 并写入会话
  （可经 `/api/sessions` 读回）。
- **影响**：任意可读文档读取 + 提示词注入；属"用户可控输入路径"同族。
- **处置**：只接受 `TEMP_DIR` 内相对文件名（`basename(value) == value`、不含 `..`、非绝对），join 后再次
  realpath 复核；已写入 `pipeline-input-path-guard` 的 spec/design/tasks。

### S-2（Important，未纳入本次三份变更，建议单独提案）`upload_image` 的 `item_id` 未净化导致路径穿越写入

- **位置**：`core/orchestrator.py` L1601-1629（`_upload_item_config` / `_next_upload_path`）、
  `api/routers/workflow.py` L272-296（`upload_artifact_image`）
- **事实**：`reference_generation/scenes` 分支的 `base = item_id`（无前缀），`item_id` 含 `../` 时
  `os.path.join(save_dir, f"{base}_upload_v1.png")` 可逃逸 `save_dir`；写入文件名固定为
  `{base}_upload_v{n}{ext}`、扩展名被限制为图片类型。
- **影响**：可在任意可写目录内创建/写入符合该命名模式的图片文件；仅当目标目录已存在同名同后缀文件时
  发生覆盖。可污染产物目录或公开目录造成内容注入，方向与 C-1 相反（写入而非读取），不构成任意文件覆盖。
- **处置建议**：`item_id` 与 `session_id` 采用白名单（如 `^[A-Za-z0-9_-]{1,64}$`），并在 join 后 realpath
  确认位于会话上传根内；单独变更实施，不与媒体入参守卫混做。
- **状态（2026-10-20）**：已落成独立变更 `openspec/changes/artifact-upload-path-safety/`（4/4 工件，
  `openspec validate --strict` 通过），覆盖标识校验、realpath 包含性、会话落盘旁路与零副作用验收。

## 七、结论（Assessment）

**Ready to merge?** **Yes（附条件）**：C-1/I-1 与 M-1~M-7、S-1/S-2 已修复并附验证证据；M-3 未取证、5.3/3.2 浏览器取证待补，不阻塞代码合并。

**说明**：上方「修复更新（2026-10-20）」已记录各项修复提交与证据；以下理由保留评审当时的判断供追溯。

**理由**：本区间自身交付的功能质量高——会话归属隔离、陈旧 running 归一、轮询超时可见性、视频失败可判别、
改写草稿/条目级 running、构建链收口都做到了"机制 + 真实断言 + 边界说明"，并且对未覆盖项如实登记，
没有用契约级验证冒充端到端。但审查在核实上轮 C-1 收口范围时发现：同类"任意本地文件读取/外传"面在
`/api/pipelines/*` 仍然开放（可把 `/app/config.yaml` 复制进公开 `/code` 目录并直接取回），
严重度与上轮 C-1 同级；该缺陷非本区间引入，但修复成本低、与既有守卫同构，应作为合并前或紧随的必修项。
视频适配层的 URL 参考输入缺陷（I-1）与上轮 I-1 同族，建议一并补正向用例。

**验证要求**（按项目 Docker 测试规则）：C-1/I-1 的修复与验证必须在既有 `video-claw-backend` 容器内执行
（复用既有镜像与 `tmp_verify/` 脚本，不新建测试镜像）；前端项（M-1/M-5/M-7）按既有前端流程
（`npx tsc --noEmit` / `npm run lint` / 浏览器取证）验证；本次评审为静态审查，未执行测试或构建。
