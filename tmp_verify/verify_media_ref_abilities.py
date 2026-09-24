"""验证媒体参考能力标签：audio_reference / video_reference 的能力推导与入口过滤。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_media_ref_abilities.py
"""
import os
import sys

sys.path.insert(0, "/app")
# 测试辅助：通过 VC_PATCHED_BACKEND 优先加载补丁代码副本（镜像重建后无需该变量）
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])
from models.config_model import (  # noqa: E402
    _custom_model_capabilities,
    model_records,
    model_type_capabilities,
)

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


# 1) 能力推导：声明 audio_reference / video_reference → 纳入 ability 标签与输入模态
item = {
    "id": "h3-demo",
    "types": ["video"],
    "abilities": ["reference_to_video", "audio_reference", "video_reference"],
}
caps = _custom_model_capabilities(item, ["video"])
check("推导：ability_types 含 audio_reference", "audio_reference" in caps["ability_types"], str(caps["ability_types"]))
check("推导：ability_types 含 video_reference", "video_reference" in caps["ability_types"], str(caps["ability_types"]))
check("推导：adapter_ability_types 同步包含两者", {"audio_reference", "video_reference"} <= set(caps["adapter_ability_types"]), str(caps["adapter_ability_types"]))
check("推导：input_modalities 含 audio", "audio" in caps["input_modalities"], str(caps["input_modalities"]))
check("推导：input_modalities 含 video", "video" in caps["input_modalities"], str(caps["input_modalities"]))
check("推导：图片模态保留", "image" in caps["input_modalities"], str(caps["input_modalities"]))

# 2) 未声明媒体参考能力：模态不包含 audio/video
caps_plain = _custom_model_capabilities({"id": "plain", "types": ["video"], "abilities": ["text_to_video"]}, ["video"])
check("未声明：模态不含 audio/video", "audio" not in caps_plain["input_modalities"] and "video" not in caps_plain["input_modalities"], str(caps_plain["input_modalities"]))

# 3) model_type_capabilities 透出能力（/api/models 下发链路）
metadata = {"id": "h3-demo", "provider": "custom-vllm", "capabilities": caps}
out = model_type_capabilities("video", metadata)
check("model_type_capabilities：透出 audio_reference", "audio_reference" in out.get("ability_types", []), str(out.get("ability_types")))
check("model_type_capabilities：透出 video 模态", "video" in out.get("input_modalities", []), str(out.get("input_modalities")))

# 4/5) 能力过滤：运行期临时注册自定义模型（含供应商配置），断言模型记录与 /api/models 入口过滤
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from api.app import app  # noqa: E402

config.Config.CONFIG.setdefault("custom_models", []).append({
    "id": "h3-demo",
    "provider": "demo-provider",
    "model": "MiniMax-H3",
    "types": ["video"],
    "abilities": ["reference_to_video", "audio_reference", "video_reference"],
})
config.Config.CONFIG.setdefault("api_providers", {})["demo-provider"] = {
    "protocol": "vllm-omni",
    "base_url": "http://127.0.0.1:1/v1",
    "api_key": "demo",
}
try:
    records = model_records(media_type="video")
    record = next((r for r in records if r["name"] == "h3-demo"), None)
    check("model_records：包含 h3-demo 条目", record is not None, str(sorted({r["name"] for r in records})[:8]))
    if record:
        tags = set(record.get("adapter_ability_types") or []) | set(record.get("ability_types") or [])
        check("记录：adapter_ability_tags 含 audio_reference/video_reference", {"audio_reference", "video_reference"} <= tags, str(sorted(tags)))

    api = TestClient(app)
    for ability in ("audio_reference", "video_reference"):
        models = api.get(f"/api/models?media_type=video&ability={ability}&verified_only=true").json().get("models", [])
        tags_ok = all(
            ability in set(m.get("adapter_ability_types") or []) | set(m.get("ability_types") or [])
            for m in models
        )
        check(f"/api/models ability={ability}：仅返回声明能力的模型", tags_ok and "h3-demo" in {m["id"] for m in models}, str([m["id"] for m in models])[:120])
    # 未声明能力的模型不出现在媒体参考入口
    audio_ids = {m["id"] for m in api.get("/api/models?media_type=video&ability=audio_reference").json().get("models", [])}
    plain_models = api.get("/api/models?model_type=video").json().get("models", [])
    undeclared = {
        m["id"] for m in plain_models
        if "audio_reference" not in set(m.get("adapter_ability_types") or []) | set(m.get("ability_types") or [])
    }
    check("未声明能力模型不出现在音频参考入口", not (undeclared & audio_ids), str(sorted(undeclared & audio_ids)))
finally:
    config.Config.CONFIG["custom_models"] = [
        item for item in config.Config.CONFIG.get("custom_models", []) if item.get("id") != "h3-demo"
    ]
    config.Config.CONFIG.get("api_providers", {}).pop("demo-provider", None)

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
