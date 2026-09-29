"""验证 /api/models 仅返回可用模型（未配置的内置模型被过滤，补齐凭据后自动回归）。"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/app")
import config as config_module  # noqa: E402
from models.config_model import model_availability  # noqa: E402

BASE = f"http://127.0.0.1:{os.environ.get('BACKEND_PORT', '8000')}"  # 容器内外同端口
failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


custom_ids = {m["id"] for m in get("/api/config")["config"]["custom_models"]}
for mt in ("llm", "vlm", "t2i", "i2i", "video"):
    ids = {m["id"] for m in get(f"/api/models?model_type={mt}")["models"]}
    check(
        f"model_type={mt} 仅返回可用模型（无凭据的内置被过滤）",
        bool(ids) and ids <= custom_ids,
        str(sorted(ids))[:140],
    )

ids = {m["id"] for m in get("/api/models")["models"]}
check("媒体工作流列表为可用模型子集", bool(ids) and ids <= custom_ids, str(sorted(ids))[:140])

ids = {m["id"] for m in get("/api/models?media_type=video&ability=first_frame_i2v&verified_only=true")["models"]}
check("Pipeline 查询（first_frame_i2v）返回可用视频模型子集", bool(ids) and ids <= custom_ids, str(sorted(ids))[:140])

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