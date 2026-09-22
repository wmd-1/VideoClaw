"""验证连接快速失败与模型不可用重试短路（后端容器内运行）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_fast_fail.py
"""
import sys
import time

sys.path.insert(0, "/app")
import httpx  # noqa: E402
from models.custom_common import classify_model_unavailable, make_http_client  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


# 1) 超时配置：connect 独立短超时，read 保持长超时
client = make_http_client("http://192.0.2.1:9/v1", "", timeout=600.0)
check("connect 超时=15s", client.timeout.connect == 15.0, str(client.timeout.connect))
check("read 超时=600s", client.timeout.read == 600.0, str(client.timeout.read))
client_short = make_http_client("http://192.0.2.1:9/v1", "", timeout=10.0)
check("短超时场景 connect=min(10,15)", client_short.timeout.connect == 10.0, str(client_short.timeout.connect))

# 2) 实测：连接不可路由地址（TEST-NET-1）应在 connect 超时内快速失败，而非 600s
start = time.monotonic()
err = ""
try:
    client.post("/chat/completions", json={})
except httpx.HTTPError as exc:
    err = f"{type(exc).__name__}: {exc}"
elapsed = time.monotonic() - start
check("不可达地址快速失败（<25s）", elapsed < 25, f"elapsed={elapsed:.1f}s")
check("失败为连接类错误", "Connect" in err or "timed out" in err.lower(), err[:100])

# 3) 重试短路依据：连接类错误被判定为 model_unavailable
check("ConnectTimeout -> model_unavailable", classify_model_unavailable(httpx.ConnectTimeout("timed out")))
check("ConnectError -> model_unavailable", classify_model_unavailable(httpx.ConnectError("[Errno 111] Connection refused")))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)