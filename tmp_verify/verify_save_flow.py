"""验证自定义供应商的新增/保存/删除链路（等价前端「保存配置」调用，后端容器内运行）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_save_flow.py
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        return json.loads(resp.read().decode())


failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


current = req("GET", "/api/config")["config"]
before_keys = sorted(current.get("api_providers", {}).keys())
before_models = [m.get("id") for m in current.get("custom_models", [])]

# 1) 新增 local_llm 供应商并保存（等价前端：编辑后点击「保存配置」）
payload = json.loads(json.dumps(current))
payload.setdefault("api_providers", {})["local_llm"] = {
    "name": "Local LLM",
    "protocol": "openai",
    "base_url": "http://192.168.1.10:8000/v1",
    "api_key": "",
    "enable_proxy": False,
}
saved = req("PUT", "/api/config", {"values": payload})
after_add = sorted(saved["config"]["api_providers"].keys())
check("保存后 local_llm 生效", "local_llm" in after_add, str(after_add))

# 2) GET 视图可见（前端 savedConfig 刷新的同一数据源）
got = req("GET", "/api/config")
check("GET 可见 local_llm", "local_llm" in got["config"]["api_providers"])
check("env_overrides 中无 local_llm（非 env 来源）", "local_llm" not in got["env_overrides"]["providers"],
      str(got["env_overrides"]["providers"]))

# 3) 删除并保存（恢复原状）
restore = json.loads(json.dumps(current))
restored = req("PUT", "/api/config", {"values": restore})
final_keys = sorted(restored["config"]["api_providers"].keys())
check("删除后恢复内置集合", final_keys == before_keys, str(final_keys))
check("custom_models 未受影响", [m.get("id") for m in restored["config"].get("custom_models", [])] == before_models)

final = req("GET", "/api/config")["config"]
check("终态干净（无 local_llm）", "local_llm" not in final.get("api_providers", {}))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)