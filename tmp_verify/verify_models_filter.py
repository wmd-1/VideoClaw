"""验证 /api/models 仅返回可用模型（未配置的内置模型被过滤，补齐凭据后自动回归）。"""
import json
import sys
import urllib.request

sys.path.insert(0, "/app")
import config as config_module  # noqa: E402
from models.config_model import model_availability  # noqa: E402

BASE = "http://127.0.0.1:8000"
failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


for mt, expect in (
    ("llm", {"local-llm"}),
    ("vlm", {"local-vlm"}),
    ("t2i", {"local-image-t2i"}),
    ("i2i", {"local-image-it2i"}),
    ("video", {"local-video"}),
):
    ids = {m["id"] for m in get(f"/api/models?model_type={mt}")["models"]}
    check(f"model_type={mt} 仅返回可用 local 模型", ids == expect, str(sorted(ids)))

ids = {m["id"] for m in get("/api/models")["models"]}
check("媒体工作流列表仅 local-*", ids and ids <= {"local-image-t2i", "local-image-it2i", "local-video"}, str(sorted(ids)))

ids = {m["id"] for m in get("/api/models?media_type=video&ability=first_frame_i2v&verified_only=true")["models"]}
check("Pipeline 查询（first_frame_i2v）含 local-video", ids == {"local-video"}, str(sorted(ids)))

# 联动：补齐内置凭据后自动回归列表（进程内模拟，不影响运行中的服务）
before = model_availability("qwen3.5-plus")
prov = config_module.Config.CONFIG["api_providers"]["dashscope"]
old_key = prov.get("api_key")
prov["api_key"] = "test-key"
config_module.Config.DASHSCOPE_API_KEY = "test-key"
after = model_availability("qwen3.5-plus")
prov["api_key"] = old_key
config_module.Config.DASHSCOPE_API_KEY = old_key or ""
check("内置缺 Key 时不可用（将被过滤）", not before.get("available"), str(before))
check("补齐凭据后可用（自动回归列表）", after.get("available"), str(after))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)