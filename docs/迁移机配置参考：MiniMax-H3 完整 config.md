# 迁移机配置参考：MiniMax-H3 完整 config

> 适用：一台跑 VideoClaw backend（Docker），推理服务在另一台机器（本例 sglang `53.192.56.195:30010`、vllm-omni `53.192.56.195:8000`）。
> 本文件是在你贴出的迁移机 config 基础上，修正 4 处必改项、补 2 处建议后的**完整可直接替换版**。
> 位置：宿主机 `./data/config/config.yaml`（容器内软链为 `/app/config.yaml`）。改完执行 `docker compose restart backend`。

## 1. 完整 config.yaml

```yaml
project_name: Video-Claw
server:
  host: 0.0.0.0
  port: 8000                 # 建议改为与 .env 的 BACKEND_PORT 一致（如 8500），避免与同机 vllm-omni 的 8000 冲突
  log_level: INFO
  access_log: false

api_providers:
  common:
    print_model_input: false
    proxy: ''

  # 对话/视觉：vllm 部署（OpenAI 标准接口）
  local_llm:
    protocol: openai
    base_url: http://53.192.29.2:8666/v1
    api_key: ''
    enable_proxy: false
  local_vlm:
    protocol: openai
    base_url: http://53.192.28.254:8999/v1
    api_key: ''
    enable_proxy: false

  # 图像：⚠ 下方地址仍是模板占位，必须改成你真实的图像服务地址（否则第 2/4 阶段只能走上传兜底）
  local_image_t2i:
    protocol: vllm-omni
    base_url: http://192.168.1.10:8091/v1
    api_key: ''
    enable_proxy: false
  local_image_it2i:
    protocol: vllm-omni
    base_url: http://192.168.1.10:8091/v1
    api_key: ''
    enable_proxy: false

  # 视频：MiniMax-H3 两种部署形态，分开登记便于按能力选择
  local_video_sglang:
    protocol: sglang
    base_url: http://53.192.56.195:30010/v1
    api_key: ''
    enable_proxy: false
  local_video_vllm:
    protocol: vllm-omni
    base_url: http://53.192.56.195:8000/v1
    api_key: ''
    enable_proxy: false

# 默认模型：必须是下方 custom_models 里真实存在的 id（悬空引用会在启动工作流时报"未注册"）
models:
  llm: Qwen3.6-35B
  vlm: Qwen3.8-flash-next-fp8
  image_t2i: local-image-t2i
  image_it2i: local-image-it2i
  video: MiniMax-H3-vllm            # 兼容旧键，与 video_first_frame 保持一致
  video_first_frame: MiniMax-H3-vllm
  video_start_end: MiniMax-H3-vllm
  video_reference: MiniMax-H3-vllm

generation:
  style: realistic
  video_ratio: '16:9'
  video_resolution: 768P            # MiniMax-H3 要求 768 短边；720P 会被 sglang 以 target.short_edge 不符拒绝
  video_generation_mode: first_frame

custom_models:
  # ── 对话 / 视觉 ──
  - id: Qwen3.6-35B
    provider: local_llm
    model: Qwen3.6-35B
    types: [llm]
    abilities: []
    concurrency: 1
  - id: Qwen3.8-flash-next-fp8
    provider: local_vlm
    model: Qwen3.8-flash-next-fp8
    types: [vlm]
    abilities: []
    concurrency: 1

  # ── 图像（若你的图像服务同时支持文生图与图生图，可合并为一条）──
  - id: local-image-t2i
    provider: local_image_t2i
    model: Qwen-Image
    types: [t2i]
    abilities: []
    concurrency: 1
  - id: local-image-it2i
    provider: local_image_it2i
    model: Qwen-Image-Edit
    types: [i2i]
    abilities: []
    concurrency: 1

  # ── 视频：MiniMax-H3（sglang 形态）──
  # abilities 必须包含各模式对应的能力标签，否则该模式的选择入口按能力过滤时不会出现
  # capabilities.short_edge=768 让 sglang 的 target.short_edge 固定为服务端要求的 768
  - id: MiniMax-H3-sglang
    provider: local_video_sglang
    model: MiniMaxAI/MiniMax-H3
    types: [video]
    abilities: [text_to_video, first_frame_i2v, start_end_frame_i2v, reference_to_video]
    concurrency: 1
    capabilities:
      short_edge: 768
      resolutions: ['768P']
      duration: { min: 4, max: 15 }
      fps: [24]
      # 采样调度参数（按你实际加载的权重启用其一；不写则不下发，走服务端默认）
      # 标准权重：      num_inference_steps: 50, flow_shift: 12
      # Turbo 4step：   num_inference_steps: 5，flow_shift: 12 或 6（按 artifact）
      # Turbo 8step：   num_inference_steps: 9
      # FastH3：        num_inference_steps: 4 且 fast_h3: true（此时不下发两个 shift）
      # num_inference_steps: 50
      # flow_shift: 12
      # audio_flow_shift: 3.0
      # fast_h3: false

  # ── 视频：MiniMax-H3（vllm-omni 形态，当前默认指向它）──
  - id: MiniMax-H3-vllm
    provider: local_video_vllm
    model: /models/MiniMax-H3
    types: [video]
    abilities: [text_to_video, first_frame_i2v, start_end_frame_i2v, reference_to_video]
    concurrency: 1
    capabilities:
      short_edge: 768
      resolutions: ['768P']
      duration: { min: 4, max: 15 }
      fps: [24]
```

## 2. 相对你贴出的配置改了什么

| # | 位置 | 原值 | 改为 | 原因 |
| --- | --- | --- | --- | --- |
| 1 | `models.video` | `local-video` | `MiniMax-H3-vllm` | **悬空引用**：`custom_models` 中不存在 `local-video`，启动视频阶段会报"模型 local-video 未注册" |
| 2 | `generation.video_resolution` | `720P` | `768P` | MiniMax-H3 两个部署形态都要求 768 短边；720P 会被 sglang 以 `target.short_edge` 不符拒绝 |
| 3 | 两个 H3 模型 | 无 `capabilities` | 声明 `short_edge: 768` 等 | 双保险：即使会话仍选 720P，适配层也按 768 下发；同时驱动生成配置面板的可选项 |
| 4 | `MiniMax-H3-sglang.abilities` | 含 `reference_image`、缺 `first_frame_i2v`/`start_end_frame_i2v` | 去掉图像侧标签、补齐视频能力 | `reference_image` 是图像能力，挂在视频模型上无效；缺能力标签会让"首帧/首尾帧"入口按能力过滤时选不到 |
| 5 | `MiniMax-H3-vllm.abilities` | 缺 `first_frame_i2v`/`start_end_frame_i2v` | 补齐 | 你把三个视频默认模型都指向它，缺标签会导致对应模式入口过滤掉它 |
| 6 | `local_image_t2i/it2i.base_url` | `192.168.1.10:8091`（模板占位） | 待填真实地址 | 否则第 2/4 阶段图像生成必然失败（只能走上传兜底） |
| 7 | `server.port` | `8000` | 建议 `8500`（与 `.env BACKEND_PORT` 一致） | 与 vllm-omni 的 8000 同机部署时会冲突；分开后日志/直连端口一致更清晰 |

## 3. 校验与生效

```bash
# 1) 语法与模型可达性自检（容器内，遵循项目"后端验证必须在既有 Docker 环境"的规则）
docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python -c "
import yaml
from models.config_model import resolve_model_entry, model_availability
raw = yaml.safe_load(open('/app/data/config.yaml'))
ids = {m.get('id') for m in raw.get('custom_models', [])}
print('悬空引用:', {k: v for k, v in (raw.get('models') or {}).items()
                 if v and resolve_model_entry(str(v))[0] == 'unknown'} or '无')
print('视频能力:', [(m['id'], (m.get('capabilities') or {}).get('short_edge'),
                   len(m.get('abilities') or []))
                  for m in raw['custom_models'] if 'video' in (m.get('types') or [])])
"

# 2) 生效
docker compose restart backend

# 3) 冒烟：设置页对两个 H3 模型各点一次「测试」；再跑一次 768P 首帧生成，
#    核对 sglang/vllm 日志中请求含 target.short_edge=768（sglang）或 short_edge=768（vllm-omni）
```

## 4. 相关能力说明（本版已支持）

- **768P 分辨率档位**：16:9→1344x768、9:16→768x1344、1:1→768x768、4:3→1024x768、3:4→768x1024、21:9→1792x768。
- **sglang `target` 无条件下发**：`short_edge` 优先取声明值，否则由 ratio+resolution 推导；含参考图时 `aspect_ratio=auto`。
- **调度参数**：`num_inference_steps` / `flow_shift` / `audio_flow_shift` 未声明则完全不下发（与旧行为一致）；声明后 vllm-omni 走顶层表单（`audio_flow_shift` 归 `extra_params`），sglang 走 JSON 顶层，同步端点与异步任务两条链路一致；`fast_h3: true` 时自动丢弃两个 shift。
- **设置页入口**：「设置 → 自定义模型 → 高级能力参数」可填写以上所有项，留空即不下发（不必手写 YAML）。
- 也可以继续用 `.env` 覆盖（优先级高于本文件）：`VC_MODEL_VIDEO_FIRST_FRAME=MiniMax-H3-vllm`、`VC_PROVIDER_LOCAL_IMAGE_T2I__BASE_URL=...` 等；修改 `.env` 后需 `docker compose up -d backend`（重建容器）。
