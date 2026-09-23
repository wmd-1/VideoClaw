"""验证生成超时配置：默认 3 小时 + .env 覆盖 + 客户端应用。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_timeouts.py
"""
import sys

sys.path.insert(0, "/app")
import config as config_module  # noqa: E402
from config import Config  # noqa: E402
from models.custom_image import CustomImageClient  # noqa: E402
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


# 1) 默认 3 小时（无 env 覆盖时）
check("Config.TIMEOUT_IMAGE 默认 3 小时", Config.TIMEOUT_IMAGE == 10800.0, str(Config.TIMEOUT_IMAGE))
check("Config.TIMEOUT_VIDEO 默认 3 小时", Config.TIMEOUT_VIDEO == 10800.0, str(Config.TIMEOUT_VIDEO))

# 2) 客户端应用（read=3h、connect 仍 15s；视频轮询上限=3h）
img = CustomImageClient({"id": "m", "request_model": "x", "base_url": "http://127.0.0.1:1/v1", "protocol": "openai", "api_key": ""})
check("图像客户端 read 超时=3h", img._client.timeout.read == 10800.0, str(img._client.timeout.read))
check("图像客户端 connect 仍 15s", img._client.timeout.connect == 15.0, str(img._client.timeout.connect))

video = CustomVideoClient({"id": "m", "request_model": "x", "base_url": "http://127.0.0.1:1/v1", "protocol": "vllm-omni", "api_key": ""})
check("视频客户端 read 超时=3h", video._client.timeout.read == 10800.0, str(video._client.timeout.read))
check("视频轮询上限=3h", video._poll_timeout == 10800.0, str(video._poll_timeout))
check("显式传参仍可覆盖", CustomVideoClient(
    {"id": "m", "request_model": "x", "base_url": "http://127.0.0.1:1/v1", "protocol": "vllm-omni", "api_key": ""},
    poll_timeout=60.0, timeout=30.0,
)._poll_timeout == 60.0)

# 3) .env 覆盖解析（模块级模拟）
original = config_module._load_env_sources
config_module._load_env_sources = lambda: {"VC_TIMEOUT_IMAGE": "111", "VC_TIMEOUT_VIDEO": "222"}
try:
    img_t, vid_t = config_module._resolve_generation_timeouts()
finally:
    config_module._load_env_sources = original
check("VC_TIMEOUT_IMAGE 覆盖生效", img_t == 111.0, str(img_t))
check("VC_TIMEOUT_VIDEO 覆盖生效", vid_t == 222.0, str(vid_t))

config_module._load_env_sources = lambda: {"VC_TIMEOUT_VIDEO": "abc"}
try:
    _, bad_t = config_module._resolve_generation_timeouts()
finally:
    config_module._load_env_sources = original
check("非法值回退默认", bad_t == 10800.0, str(bad_t))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)