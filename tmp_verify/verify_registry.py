"""任务 1.5/1.6 验证脚本：注册表合并 / resolve / 并发缺省 / provider_label / model_availability。

在既有 Docker 容器内运行：
  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - \
    < tmp_verify/verify_registry.py
"""
import sys

import config as cfg
from models import config_model as cm

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── 注入测试用供应商与模型（仅修改本进程内存中的有效配置，不写盘）──
cfg.Config.CONFIG.setdefault("api_providers", {}).update({
    "mock-openai": {
        "protocol": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "enable_proxy": False,
        "name": "本地 OpenAI 兼容",
    },
    "mock-omni": {
        "protocol": "vllm-omni",
        "base_url": "http://127.0.0.1:8091/v1",
        "api_key": "",
        "enable_proxy": False,
    },
    "mock-broken": {"protocol": "", "base_url": "", "api_key": "", "enable_proxy": False},
})
cfg.Config.CONFIG["custom_models"] = [
    {"id": "mock-llm", "name": "本地 LLM", "provider": "mock-openai", "model": "Qwen3-32B", "types": ["llm", "vlm"], "abilities": []},
    {"id": "mock-image", "name": "本地图像", "provider": "mock-omni", "model": "", "types": ["t2i", "i2i"], "abilities": []},
    {"id": "mock-video", "name": "本地视频", "provider": "mock-omni", "model": "", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]},
    {"id": "mock-video-default", "name": "本地视频默认能力", "provider": "mock-omni", "model": "", "types": ["video"], "abilities": []},
    {"id": "mock-conc3", "name": "并发3", "provider": "mock-omni", "model": "", "types": ["video"], "abilities": [], "concurrency": 3},
    {"id": "mock-missing-provider", "name": "断链", "provider": "ghost", "model": "", "types": ["llm"], "abilities": []},
    {"id": "mock-incomplete-provider", "name": "不完整", "provider": "mock-broken", "model": "", "types": ["llm"], "abilities": []},
]

# ── 1.5 合并视图与 resolve ──
merged = cm.merged_models()
check("1.5 merged 含自定义与内置", "mock-image" in merged and "wan2.7-image" in merged)

kind, meta = cm.resolve_model_entry("mock-llm")
check(
    "1.5 resolve 自定义命中（协议/下发模型名）",
    kind == "custom" and meta["protocol"] == "openai" and meta["request_model"] == "Qwen3-32B",
    str(meta)[:120],
)
check("1.5 provider_label 取供应商显示名", meta.get("provider_label") == "本地 OpenAI 兼容")
check("1.5 resolve 内置精确", cm.resolve_model_entry("wan2.7-image")[0] == "builtin")
check("1.5 resolve 未注册", cm.resolve_model_entry("no-such-model-xyz")[0] == "unknown")

# ── 1.5 并发缺省 ──
check("1.5 自定义并发缺省为 1", cm.get_max_concurrency("mock-video", True) == 1)
check("1.5 显式并发生效", cm.get_max_concurrency("mock-conc3", True) == 3)
check("1.5 关闭并发时为 1", cm.get_max_concurrency("mock-conc3", False) == 1)

# ── 1.5 能力标签推导 ──
caps = cm.model_type_capabilities("video", merged["mock-video"])
check(
    "1.5 视频能力含 first_frame_i2v 且 verified=True",
    "first_frame_i2v" in caps["adapter_ability_types"] and caps["api_contract_verified"] is True,
    str(caps)[:140],
)
img_caps = cm.model_type_capabilities("t2i", merged["mock-image"])
check(
    "1.5 图像能力 ability_type=image_generation",
    img_caps["ability_type"] == "image_generation" and "text_to_image" in img_caps["ability_types"],
)
default_caps = cm.model_type_capabilities("video", merged["mock-video-default"])
check(
    "1.5 视频未声明能力时默认 [t2v, first_frame_i2v]",
    default_caps["ability_types"] == ["text_to_video", "first_frame_i2v"],
    str(default_caps["ability_types"]),
)

# ── 1.5 列表筛选 / records / parse / capabilities ──
t2i_ids = [m["id"] for m in cm.get_models_by_type("t2i")]
check("1.5 t2i 筛选包含自定义", "mock-image" in t2i_ids)
video_records = cm.model_records(media_type="video")
rec = next((r for r in video_records if r["model"] == "mock-video"), None)
check("1.5 model_records 含 provider_label", bool(rec) and rec.get("provider_label") == "mock-omni")
abl = cm.list_api_models(media_type="video", required_adapter_abilities=["first_frame_i2v"], verified_only=True)
check("1.5 按能力+verified 筛选命中自定义", any(r["model"] == "mock-video" for r in abl))
provider, resolved = cm.parse_api_model("mock-image", "image")
check("1.5 parse_api_model 解析自定义", provider == "mock-omni" and resolved == "mock-image")
mc = cm.media_capabilities("mock-omni", "mock-image", "image")
check("1.5 media_capabilities 命中自定义 capabilities", mc.get("ability_type") == "image_generation")

# ── 1.6 model_availability 五类返回值 ──
av = cm.model_availability("mock-image")
check("1.6 available（自定义完整）", av == {"available": True, "code": "", "reason": ""}, str(av))
av = cm.model_availability("no-such-model-xyz")
check("1.6 not_registered", av["code"] == "not_registered", str(av))
av = cm.model_availability("mock-missing-provider")
check("1.6 provider_missing", av["code"] == "provider_missing", str(av))
av = cm.model_availability("mock-incomplete-provider")
check("1.6 provider_incomplete", av["code"] == "provider_incomplete", str(av))

orig_key = cfg.Config.DASHSCOPE_API_KEY
try:
    cfg.Config.DASHSCOPE_API_KEY = ""
    av = cm.model_availability("wan2.7-image")
    check("1.6 missing_credentials（缺 Key）", av["code"] == "missing_credentials", str(av))
    cfg.Config.DASHSCOPE_API_KEY = "sk-test"
    av = cm.model_availability("wan2.7-image")
    check("1.6 凭据补齐后 available", av["available"] is True, str(av))
finally:
    cfg.Config.DASHSCOPE_API_KEY = orig_key

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项未通过: {FAILURES}")
    sys.exit(1)
print("ALL PASS")