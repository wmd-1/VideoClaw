"""验证「配置文件移除内置供应商 + local_* 设为默认」与「保存不回写内置」逻辑。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_prune.py
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/app")
import yaml  # noqa: E402
import config as config_module  # noqa: E402
from config import CONFIG_PATH  # noqa: E402

BUILTINS = {"openai", "gemini", "deepseek", "dashscope", "ark", "kling"}
LOCALS = {"local_llm", "local_vlm", "local_image_t2i", "local_image_it2i", "local_video"}
failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


# 1) 模块级：裁剪行为
view = {"api_providers": {"common": {}, "openai": {}, "dashscope": {}, "local_llm": {}}}
config_module._prune_absent_builtin_providers(view, {"common", "local_llm"})
check("未声明的内置被剔除", set(view["api_providers"].keys()) == {"common", "local_llm"}, str(sorted(view["api_providers"].keys())))
view2 = {"api_providers": {"common": {}, "openai": {}, "local_llm": {}}}
config_module._prune_absent_builtin_providers(view2, {"common", "openai", "local_llm"})
check("文件声明的内置被保留", set(view2["api_providers"].keys()) == {"common", "openai", "local_llm"})

# 2) 配置文件现状
with CONFIG_PATH.open("r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
raw_keys = set((raw.get("api_providers") or {}).keys())
check("配置文件无内置供应商键", not (raw_keys & BUILTINS), str(sorted(raw_keys)))
check("配置文件含 local_* 供应商", LOCALS <= raw_keys)
check("默认模型指向 local-*",
      raw["models"]["llm"] == "local-llm" and raw["models"]["image_it2i"] == "local-image-it2i"
      and raw["models"]["video_first_frame"] == "local-video")
check("custom_models 共 5 条", len(raw.get("custom_models") or []) == 5,
      str([m.get("id") for m in raw.get("custom_models") or []]))

# 3) API 级：模拟设置页保存（载荷=有效配置，含内置 stub）→ 文件不得写回内置
BASE = "http://127.0.0.1:8000"


def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read().decode())


effective = req("GET", "/api/config")["config"]
check("有效配置仍含内置 stub（coerce 保证，休眠无 Key 不影响）", "openai" in effective["api_providers"],
      str(sorted(effective["api_providers"].keys())))

saved = req("PUT", "/api/config", {"values": effective})
with CONFIG_PATH.open("r", encoding="utf-8") as f:
    after_raw = yaml.safe_load(f) or {}
after_keys = set((after_raw.get("api_providers") or {}).keys())
check("保存后文件仍无内置供应商", not (after_keys & BUILTINS), str(sorted(after_keys)))
check("保存后 local_* 保留", LOCALS <= after_keys)
check("保存后默认模型仍为 local-*", saved["config"]["models"]["llm"] == "local-llm")

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)