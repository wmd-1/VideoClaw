# VideoClaw 提交区间代码评审报告

- **评审日期**：2026-09-27
- **核对日期**：2026-09-28（基于 HEAD `cdea2a1` 复核；区间后无新提交、无未提交代码改动，下列问题的修复建议均尚未落地）
- **评审范围**：`08502d5..cdea2a1`（4 个 feat 提交，58 files，+4491 / -209）
  - `6ba2f19` feat(video): MiniMax-H3 双协议全能力适配与生成超时可配，失败后可整段重跑
  - `07b4bdb` feat(video): 统一视频参数体系（fps/short_edge/时长夹取），能力声明联动前端生成配置
  - `fd5c486` feat(video): 自定义视频媒体参考（音频/视频）贯通适配层、沙盒与工作流
  - `cdea2a1` feat(workflow): 新增可选「提示词改写」阶段，接入 Design Agent Platform 的 MiniMax-H3 改写
- **对照规格**：`openspec/changes/{minimax-h3-prompt-rewrite-stage, video-generation-parameter-system, video-audio-video-references}`

---

## 一、优点（Strengths）

1. **单点参数映射，链路一致**
   `models/custom_video.py::_map_protocol_params` 把 vllm-omni / sglang / openai 三种协议的 `duration / fps / short_edge / ratio+resolution` 映射集中到一处；sync 与 async 三段式两条链路都通过同一函数生成 `protocol_fields`，从根上消除了“两条链路字段漂移”的经典坑。`tmp_verify/verify_video_params.py`（334 行）以矩阵式断言覆盖三协议 × 六类参数。

2. **能力夹取“绝不静默退化”**
   - `custom_video.py` 通过 `_record_adjustment` 收集所有回落/夹取；`video_agent.py` 将 `_adjustments` 通过 `payload` 透出到 API 响应。
   - 含参考但创建任务失败时抛出包含服务端响应和 URL 的明确异常（`custom_video.py` `_create_job` 内 L641-L644：`f"自定义视频模型 {self._model} 创建任务失败（目标 {self._base_url}/videos）: {last_error}"`，其中 `last_error` 含 `response.text[:200]`；`_try_sync_generate` 失败则返回错误信息并回退异步三段式），符合 spec.md “失败错误包含服务端响应与目标地址，绝不静默退化为无参考请求”。

3. **Design Agent 客户端错误分类干净**
   `models/design_agent_client.py`（新增，209 行）以 `DesignAgentUnavailable / SessionGone / SessionConflict / QuotaExceeded / TurnFailed / HttpError` 分层，且 `submit_turn` 只对网络类错误重试一次（`time.sleep(1.0)`），避免“5xx 风暴时死重试”。

4. **前端两个“已知坑”处理到位**
   - 遍历 status map 先按已知工作流阶段过滤内部键：`WorkflowPanel.tsx` 使用 `allStatusStages.filter(name => stageOrder.includes(name))`。
   - 确认按钮“后续阶段是否已开始”的判定排除 `waiting` 与 `stopped`：`st !== 'pending' && st !== 'waiting' && st !== 'stopped'`。

5. **测试脚本套件完整、断言真实**
   `tmp_verify/verify_video_h3_e2e.py`（214 行）启自建 mock HTTP 服务端、以真 httpx 走 multipart 与 JSON 两条链路；`verify_failure_paths.py` 断言不可达时阶段 <30s 快速失败；`verify_sandbox_media_refs.py` 覆盖 URL 透传/本地文件转 URL/文件缺失错误。均在 Docker 环境内以 `python - < script` 执行，符合项目测试规则，未新建/重建镜像。

6. **OpenSpec 与实现高度对齐**
   阶段启停（禁用六阶段/启用七阶段）、逐分镜串行、条目级失败隔离、会话复用/重建、参数不静默退化——三份 spec 中的 MUST 条款基本都可在代码与 verify 脚本中找到对应实现。

---

## 二、问题（Issues）

### Critical（必须修复）

#### C1. 沙盒音频参考路径穿越，导致任意本地文件可被公开访问

**位置**：`video-claw/video-claw/backend/api/routers/sandbox.py#L169-L201`（`_resolve_media_reference_url`）

**问题**：`_resolve_media_reference_url` 接收 `req.audio_url` 作为任意字符串：

- 若 `value` 是绝对路径，直接 `os.path.abspath(value)` 加入 `candidates`；
- 若命中且 `source` 不在 `code_dir` 前缀下，会被 `shutil.copy2` 复制到 `SANDBOX_MEDIA_UPLOAD_DIR`（该目录位于 `/code` 静态目录下，即公开可访问）；
- 最终 `return base_url + "/code/" + relative`，返回一个**公共 URL**。

后端未挂任何鉴权（`README.md` 亦明确“零鉴权本地服务”），任何能访问 API 端点的人都可以：

```json
POST /api/sandbox/generate_video
{ "audio_url": "/etc/passwd" }
```

或 `audio_url: "../../../../home/wmd/.ssh/id_rsa"`（相对 `code_dir` 用 `..` 逃逸），响应或后续产物 URL 会指向被复制到公共目录的敏感文件。这是**任意文件读取 + 落地扩散**，不只是路径遍历本身。代码里的 docstring 提到“TEMP_DIR 上传件”，但实现并未把候选限制在 `TEMP_DIR`/`CODE_DIR` 白名单内。

**影响范围**：`POST /api/sandbox/video` 沙盒接口（`sandbox_video`，`sandbox.py:427` 调用点），只在传 `audio_url` 时触发；`video_agent.py` 未走这个 helper，工作流侧不受影响。

**核对补充（2026-09-28）**：两条攻击路径的机制确认如下——
- **绝对路径**（如 `/etc/passwd`）：`candidates` 直接收 `os.path.abspath(value)`，命中后因不在 `code_dir` 前缀内被 `shutil.copy2` 复制到公开的 `SANDBOX_MEDIA_UPLOAD_DIR`，返回公共 URL。**任意文件读取 + 落地扩散**，确定可利用。
- **相对路径 `..` 逃逸**（如 `../xxx`）：`os.path.join(code_dir, value)` 不做规范化，得到的 `code_dir/../xxx` 字符串**能通过** `source.startswith(code_dir + os.sep)` 的前缀检查（不会进 copy 分支），最终 `os.path.relpath` 拼出 `base_url + "/code/../xxx"`。是否可下载取决于静态服务对 `/code` 之外路径的路由，危害弱于绝对路径场景，但同样应拒绝。

**修复**：把可访问路径严格限定在两个允许目录内，并使用 `os.path.realpath` 消除符号链接逃逸：

```python
ALLOWED_ROOTS = [os.path.realpath(settings.CODE_DIR), os.path.realpath(settings.TEMP_DIR)]
...
source = next((p for p in candidates if os.path.isfile(p)), None)
if not source:
    raise ValueError(f"音频参考文件不存在: {value}")
real = os.path.realpath(source)
if not any(real == r or real.startswith(r + os.sep) for r in ALLOWED_ROOTS):
    raise ValueError(f"音频参考路径越界: {value}")
```

另：`SANDBOX_MEDIA_UPLOAD_DIR` 建在 `/code` 静态目录内本身也是设计瑕疵——建议放 `TEMP_DIR`，只通过 `audio_url` 字段透传给推理端，不通过 Web 服务器暴露。

---

### Important（应当修复）

#### I1. `prompt_rewrite` 阶段被停用时，历史会话无法推进（`current_stage=PROMPT_REWRITE` → 死锁）

**位置**：`video-claw/video-claw/backend/core/orchestrator.py#L481-L490`（`_get_next_stage`）

**问题**：

```python
def _get_next_stage(self, current: WorkflowStage) -> Optional[WorkflowStage]:
    order = get_stage_order()  # 未启用时无 PROMPT_REWRITE
    try:
        idx = order.index(current)
        return order[idx + 1] if idx + 1 < len(order) else None
    except ValueError:
        pass
    return None
```

`get_stage_order()` 依 `Config.DESIGN_AGENT_ENABLED` 动态决定 `PROMPT_REWRITE` 是否出现在列表中。若某会话在启用状态下跑完 prompt_rewrite，随后运维把开关关掉：该会话 `state.current_stage == PROMPT_REWRITE`，`order.index(PROMPT_REWRITE)` 抛 `ValueError`，函数返回 `None`——工作流“确认按钮 → 推进下一阶段”点不动，用户没有任何 UI 提示，只能删会话重开。

**为什么重要**：`VC_DESIGN_AGENT__ENABLE` 是运维可随时改的 env，“中途关闭”是现实场景；spec 明确说“禁用时主流程保持原六阶段”，但没有覆盖“禁用瞬间存量 prompt_rewrite 阶段会话”的迁移。

**修复方向**：`_get_next_stage` 遇到 `current` 不在 `order` 时，回退到“用固定的全序 `ALL_STAGES` 找下一个存在于 `order` 中的阶段”：

```python
ALL = [s for s in WorkflowStage]  # 或模块级常量
try:
    idx_full = ALL.index(current)
except ValueError:
    return None
for nxt in ALL[idx_full + 1:]:
    if nxt in order:
        return nxt
return None
```

或更保守：在会话恢复 (`load_session_snapshot`) 时，若 `current_stage` 已从 `get_stage_order()` 消失，则自动向前推进到下一有效阶段并记录调整。

#### I2. `_ensure_session` 内若 `client.create_session()` 抛异常，客户端连接/会话句柄泄漏

**位置**：`video-claw/video-claw/backend/core/agents/prompt_rewrite_agent.py#L116-L129`（已核对：`client.create_session()` 于 L129 抛异常时 `client` 无人关闭；三个调用点 L230-L236、L242-L246、L261/L305 的 `finally: client.close()` 均在 `_ensure_session` 成功返回后才生效）

**问题**：

```python
def _ensure_session(self, prev_session_id):
    client = self._create_client()          # 可能持有 httpx.Client
    if prev_session_id:
        try:
            client.get_session(prev_session_id)
            return client, prev_session_id
        except Exception as exc:
            ...
    return client, client.create_session()  # 若这里抛异常
```

所有调用点（`process` / `_process_revisions` / regenerate 分支）形如：

```python
client, ext_session_id = await asyncio.to_thread(self._ensure_session, prev_session_id)
try: ...
finally: client.close()
```

一旦 `client.create_session()` 抛异常（网络不可达、500、429 都会），`_ensure_session` 抛出时 `client` 局部变量销毁，`finally: client.close()` 位于调用方 try 之外，**不会执行**。`DesignAgentClient` 内部若持有 `httpx.Client`（其连接池有 keep-alive 线程与 socket）就会持续泄漏；用户反复点“重生成”或整段重跑时会累积。

**修复**：把 `close()` 的责任收进 `_ensure_session`：

```python
def _ensure_session(self, prev_session_id):
    client = self._create_client()
    try:
        if prev_session_id:
            try:
                client.get_session(prev_session_id)
                return client, prev_session_id
            except Exception: ...
        sid = client.create_session()
        return client, sid
    except Exception:
        try: client.close()
        except Exception: pass
        raise
```

#### I3. 前端“音频 + 视频参考”组合时模型能力过滤漏一项

**位置**：`video-claw/video-claw/frontend/components/Sandbox/Sandbox.tsx#L614-L616`（`requiredAbility` 三目）

**问题**：

```ts
const requiredAbility = audioRefUrl
  ? 'audio_reference'
  : videoRefPaths.length > 0
    ? 'video_reference'
    : '';
```

三目链只能挑一个能力过滤。若用户**同时**上传音频与视频参考，只有 `audio_reference` 生效，模型只要声明 `audio_reference` 就会出现在列表中——即便它并未声明 `video_reference`。这违反 `custom-video-references/spec.md` “未声明对应能力的模型不出现在对应入口”。

**修复**：改为多能力 AND 过滤，或在后端 `/api/models?media_type=video&abilities=a,b` 提供 list 参数：

```ts
const requiredAbilities = [
  audioRefUrl ? 'audio_reference' : null,
  videoRefPaths.length > 0 ? 'video_reference' : null,
].filter(Boolean) as string[];
```

如果后端 API 只支持单 ability，最保守的临时修补是：把筛选改成对模型 `capabilities.ability_types` 的客户端二次过滤，要求 `requiredAbilities.every(a => m.abilities.includes(a))`。

#### I4. `_process_revisions` 中 `versions` 列表与 `prev_item` 共享同一引用

**位置**：`video-claw/video-claw/backend/core/agents/prompt_rewrite_agent.py#L133-L151`（`versions` 共享引用在 L147；两处 `_append_version` 写入同一 list 在 `_process_revisions` 的 L288 与 L291）

**问题**：

```python
def _make_item(..., prev_item):
    return {
        ...
        "versions": (prev_item or {}).get("versions", []),  # 直接引用旧 list
    }
```

随后：

```python
self._append_version(prev_item, old_text, source="superseded")   # 写入共享 list
item = self._make_item(..., prev_item)                             # item["versions"] is prev_item["versions"]
self._append_version(item, rewritten, source="agent")             # 再次写入共享 list
```

两个 `append` 都落在同一 list 上。当前流程下 `prev_item` 会被 `results[seg_id]`（即 `item`）通过 orchestrator 的 `merge_keys_by_stage` 覆盖，用户看不到 prev_item 的 versions 被污染；但：

- 一旦未来引入“版本历史”UI 或直接读 artifacts 中未替换掉的旧条目（例如 regenerate 只覆盖指定 id、其他 id 的 prev_item 仍留在 payload 之外），就会看到“上一版条目突然多了一条 agent 版本”；
- 阅读者极难看出 `item["versions"] is prev_item["versions"]`，是隐性陷阱。

**修复**：`_make_item` 里显式浅拷贝：

```python
"versions": list((prev_item or {}).get("versions") or []),
```

---

### Minor（可以更好）

#### M1. `fetchEnabledStages` 错误路径不写缓存 → 每次挂载都打后端

**位置**：`video-claw/video-claw/frontend/components/TopBar.tsx#L35-L48`（`fetchEnabledStages`）

`try { cachedEnabledStages = data } catch { return DEFAULT_ENABLED_STAGES }`——失败分支未赋值 `cachedEnabledStages`（`!resp.ok` 分支 L39 同样不写缓存），因此后端不可达时组件重挂会重新打 `/api/stages`，形成“后端挂 + 前端持续打”放大。建议失败时也把 DEFAULT 写入缓存（配合一个短 TTL）。

#### M2. sync 端点返回的“远端标识”是伪 URL

`custom_video.py` 在 vllm-omni sync 直出成功后（`_generate` 主流程 L135：`return f"{self._base_url}/videos/sync"`，`_try_sync_generate` 本身成功返回 `None`），用该字符串充当“远端任务 ID/URL”字段。这个字符串指向一个“POST 端点”，用户或后续逻辑试图 GET 时永远 405/404。目前只作为元信息透传不影响主流程，但命名易误导——建议改成 `sync://<sha1(content)>` 或干脆 `None` + 备注“同步直出，无远端可重取标识”。

#### M3. `.env.example` 结尾无换行

`diff` 中 `.env.example` 保留 `\ No newline at end of file`；不影响功能，纯整洁性。（核对更正：`README.md` 在 `cdea2a1` 已以换行结尾，原报告中 README 部分不再成立。）

---

## 三、建议（Recommendations）

1. **超时链路加统一入口断言**：目前 `Config.TIMEOUT_IMAGE / TIMEOUT_VIDEO / TIMEOUT_DESIGN_AGENT` 三处独立获取，`custom_common.py` 与 `custom_video.py` 分别读取。建议在 `Config` 上再加 `get_http_timeout_for(kind)` 单函数，避免以后加 `TIMEOUT_LLM` 之类时又要摸多个调用点。
2. **失败整段重跑时的“停止标记复位”路径值得加断言**：`orchestrator.reset_stop_event` 与前端 `stoppedRef.current = false` 是两处不同 reset；建议在 `tmp_verify/` 加一条 e2e：`stop → rerun` 覆盖，明确断言 `state.artifacts` 中被 `stop` 中断的条目回到 `pending` 且 `stoppedRef` 复位。目前 `verify_prompt_rewrite_stage.py` 覆盖了启停/复用/隔离，未覆盖“点停止后整段重跑”。
3. **OpenSpec 建议补一条“中途停用 prompt_rewrite 时存量会话推进”的 spec requirement**——现在实现 I1 之所以是坑，是因为规格本身没定义这个迁移场景。规格补上后再对齐代码修复更稳。
4. **`sandbox._resolve_media_reference_url` 与 `files.py` 的上传接口应当共用同一份“允许目录 + realpath 校验”的 helper**，避免以后加视频参考、图像参考时又漏一遍。

---

## 四、结论（Assessment）

### 核对记录（2026-09-28，基于 HEAD `cdea2a1`）

| 条目 | 状态 | 核对结果 |
| --- | --- | --- |
| C1 | **仍存在** | `sandbox.py` L169-L200 未做允许目录/realpath 校验；绝对路径复制扩散路径确认可利用（详见上文核对补充） |
| I1 | **仍存在** | `orchestrator.py` L481-L489 与报告描述一致 |
| I2 | **仍存在** | `prompt_rewrite_agent.py` L116-L129，`client.create_session()` 异常时 `client` 泄漏（位置更正为 L116-L129） |
| I3 | **仍存在** | `Sandbox.tsx` L614-L616 三目只取单能力 |
| I4 | **仍存在** | `prompt_rewrite_agent.py` L147 直接引用旧 list（位置更正为 L133-L151） |
| M1 | **仍存在** | `TopBar.tsx` L35-L48，`!resp.ok` 与 `catch` 两个分支均不写缓存 |
| M2 | **仍存在** | `custom_video.py` L135 sync 成功返回伪 URL（归属更正为调用侧 `_generate`） |
| M3 | **部分成立** | 仅 `.env.example` 缺结尾换行；README.md 已有（更正） |
| 优点 1-6 | **均核实** | 行数（334/209/214）、关键函数、`WorkflowPanel.tsx` 两处过滤逻辑、tmp_verify 脚本均与描述相符（仅原文两处行号/行数已更正） |

**是否可以合并？** **否——修复后可。**

**理由**：整体架构、协议适配、能力夹取、测试脚本质量都在良好水准，OpenSpec 对齐度高，两个“项目已知坑”（status map 过滤、确认按钮状态排除 waiting/stopped）都处理正确。但 **C1 路径穿越漏洞**是零鉴权本地服务下的**任意文件读取 + 静态目录扩散**，属于阻断级安全问题；同时 I1/I2 会在运维真实场景（停用开关 / 网络抖动）造成会话卡死与句柄泄漏。修完 C1、I1、I2 即可放行；I3、I4 建议同批处理，M1-M3 可后续跟进。
