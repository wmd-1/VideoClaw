"""验证 API Server / Common 的 env 覆盖层、server 端口对齐与适配层错误提示（在后端容器内运行）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_server_env.py
"""
import copy
import sys

sys.path.insert(0, "/app")
import httpx  # noqa: E402
import config as config_module  # noqa: E402
from config import Config  # noqa: E402

failures = []


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def run_with_env(env_map):
    original = config_module._load_env_sources
    config_module._load_env_sources = lambda: dict(env_map)
    try:
        coerced = config_module._coerce_config(copy.deepcopy(config_module.DEFAULT_CONFIG))
        effective, info = config_module._apply_env_overrides(coerced)
        file_view = config_module._strip_env_overridden(coerced, info, coerced)
        return effective, info, file_view
    finally:
        config_module._load_env_sources = original


# 1) VC_SERVER__* / VC_COMMON__* 覆盖
eff, info, fv = run_with_env({
    "VC_SERVER__PORT": "9001",
    "VC_SERVER__LOG_LEVEL": "debug",
    "VC_SERVER__ACCESS_LOG": "true",
    "VC_COMMON__PROXY": "http://127.0.0.1:7890",
    "VC_COMMON__PRINT_MODEL_INPUT": "1",
})
check("VC_SERVER__PORT 生效", eff["server"]["port"] == 9001, f"port={eff['server']['port']}")
check("VC_SERVER__LOG_LEVEL 规范化", eff["server"]["log_level"] == "DEBUG", str(eff["server"]["log_level"]))
check("VC_SERVER__ACCESS_LOG 布尔", eff["server"]["access_log"] is True)
check("VC_COMMON__PROXY 生效", eff["api_providers"]["common"]["proxy"] == "http://127.0.0.1:7890")
check("VC_COMMON__PRINT_MODEL_INPUT 布尔", eff["api_providers"]["common"]["print_model_input"] is True)
check("env_overrides 含 server/common 字段",
      "server.port" in info["fields"] and "api_providers.common.proxy" in info["fields"],
      str(info["fields"]))
check("保存剥离：server.port 恢复文件原值", fv["server"]["port"] == 8000, f"file={fv['server']['port']}")
check("保存剥离：common.proxy 恢复文件原值", fv["api_providers"]["common"]["proxy"] == "")

# 2) BACKEND_PORT / BACKEND_HOST 回退对齐（未显式 VC_SERVER__* 时）
eff2, info2, fv2 = run_with_env({"BACKEND_PORT": "8123", "BACKEND_HOST": "0.0.0.0"})
check("BACKEND_PORT 回退对齐 server.port", eff2["server"]["port"] == 8123, f"port={eff2['server']['port']}")
check("BACKEND_HOST 回退对齐 server.host", eff2["server"]["host"] == "0.0.0.0")
check("回退时保存剥离 server.port", fv2["server"]["port"] == 8000)
eff3, _, _ = run_with_env({"BACKEND_PORT": "8123", "VC_SERVER__PORT": "9555"})
check("VC_SERVER__PORT 优先于 BACKEND_PORT", eff3["server"]["port"] == 9555)
eff4, _, _ = run_with_env({"VC_SERVER__PORT": "abc"})
check("非法 server.port 被忽略", eff4["server"]["port"] == 8000)

# 3) 容器进程环境实际收集与运行配置
real_env = config_module._load_env_sources()
check("进程环境收集 BACKEND_PORT", "BACKEND_PORT" in real_env, str(real_env.get("BACKEND_PORT")))
check("运行中 Config server.port 对齐 .env", int(Config.CONFIG["server"]["port"]) == 8000, f"={Config.CONFIG['server']['port']}")

# 4) 适配层错误信息携带目标地址与容器网络提示
from models.custom_common import connection_hint  # noqa: E402

hint = connection_hint(httpx.ConnectError("[Errno 111] Connection refused"), "http://127.0.0.1:8000/v1")
check("回环地址容器网络提示", "Docker" in hint and "127.0.0.1" in hint, hint[:60])
hint2 = connection_hint(httpx.ConnectError("[Errno 111] Connection refused"), "http://192.168.1.10:8000/v1")
check("非回环地址无额外提示", hint2 == "")

from models.custom_llm import CustomChatClient  # noqa: E402

client = CustomChatClient({"id": "local-llm", "request_model": "Qwen3-32B", "base_url": "http://127.0.0.1:9/v1", "protocol": "openai", "api_key": ""})
msg = ""
try:
    client.query("hi")
except RuntimeError as exc:
    msg = str(exc)
check("LLM 连接失败信息含目标与提示", "目标 http://127.0.0.1:9/v1" in msg and "Docker" in msg, msg[:130])

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)