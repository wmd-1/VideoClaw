"""复现两个问题（零副作用）：
1) 设置页保存的校验路径是否报错（问题1：400 Bad Request）
2) 工作流模型解析是否找不到 local-llm（问题2：未注册）
运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/repro_issues.py
"""
import copy
import json
import sys
import urllib.request

sys.path.insert(0, "/app")
import yaml  # noqa: E402
import config as config_module  # noqa: E402
from config import CONFIG_PATH  # noqa: E402
from models.config_model import resolve_model_entry  # noqa: E402

# 0) 文件现状
with CONFIG_PATH.open("r", encoding="utf-8") as f:
    raw = yaml.safe_load(f) or {}
print("== config.yaml custom_models:", [(m.get("id"), m.get("provider"), m.get("abilities")) for m in raw.get("custom_models", [])])
print("== config.yaml providers:", sorted((raw.get("api_providers") or {}).keys()))
print("== config.yaml models:", raw.get("models"))

# 1) GET 有效配置
with urllib.request.urlopen("http://127.0.0.1:8000/api/config", timeout=30) as r:
    d = json.load(r)
eff = d["config"]
print("== GET custom ids:", [m["id"] for m in eff["custom_models"]])
print("== GET providers:", sorted(eff["api_providers"].keys()))

# 2) 模拟设置页保存：整树 PUT 的校验路径（不写盘）
try:
    coerced = config_module._coerce_config(copy.deepcopy(eff))
    effective, info = config_module._apply_env_overrides(coerced, baseline=config_module._FILE_CONFIG_VALUES)
    config_module._validate_effective_config(effective)
    print("== PUT 校验路径: PASS")
except Exception as exc:  # noqa: BLE001
    print(f"== PUT 校验路径 FAIL: {type(exc).__name__}: {exc}")

# 3) 工作流模型解析
print("== resolve local-llm:", resolve_model_entry("local-llm")[0])
for m in eff["custom_models"]:
    print(f"== resolve {m['id']}:", resolve_model_entry(m["id"])[0])