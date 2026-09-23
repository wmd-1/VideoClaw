"""MiniMax-H3 双协议真实 HTTP 全链路 E2E（自包含 mock，无外部依赖）。

覆盖：vllm-omni 首尾帧（extra_params.frame_indices + input_references×2）、
      sglang 文生（JSON + task=t2va）、产物落盘与轮询/下载链路。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_video_h3_e2e.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/app")
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []
RECORDS = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        RECORDS.append({"path": self.path, "ctype": self.headers.get("Content-Type", ""), "body": body})
        if self.path.endswith("/videos/sync"):
            data = b"\x00\x00\x00\x18ftypmp42" + b"1" * 64
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"id": "v-1", "status": "queued"})

    def do_GET(self):
        if self.path.endswith("/content"):
            data = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"id": "v-1", "status": "completed"})

    def _json(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"== 自包含 mock 已启动: 127.0.0.1:{port}")

p1, p2 = "/tmp/h3_e2e_a.png", "/tmp/h3_e2e_b.png"
for path in (p1, p2):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)


def make(protocol):
    return CustomVideoClient(
        {"id": "h3", "request_model": "MiniMax-H3", "base_url": f"http://127.0.0.1:{port}/v1", "protocol": protocol, "api_key": ""},
        poll_interval=0.1,
    )


# 1) vllm-omni 首尾帧：真实 multipart 请求
save1 = "/tmp/h3_e2e_first_last.mp4"
remote1 = make("vllm-omni").generate_video(
    prompt="A cat walks from frame one to frame two.",
    image_path=p1,
    save_path=save1,
    model="MiniMax-H3",
    duration=5,
    last_image_path=p2,
)
rec = RECORDS[0]
text = rec["body"].decode("utf-8", "ignore")
check("vllm-omni 请求为 multipart/form-data", "multipart/form-data" in rec["ctype"], rec["ctype"])
check("vllm-omni 含 extra_params 且 task=fl2va", '"task": "fl2va"' in text, text[:120])
check("vllm-omni 含 frame_indices [0, -1]", "[0, -1]" in text, "")
check("vllm-omni 两个同名 input_references 文件字段", text.count('name="input_references"') == 2, str(text.count('name="input_references"')))
check("vllm-omni 产物落盘", os.path.exists(save1) and os.path.getsize(save1) > 0, f"{os.path.getsize(save1) if os.path.exists(save1) else 0}B")
check("vllm-omni 返回远端标识", remote1.startswith(f"http://127.0.0.1:{port}"), remote1)

# 2) sglang 文生：真实 JSON 请求（task 必填）
save2 = "/tmp/h3_e2e_t2v.mp4"
make("sglang").generate_video(
    prompt="A calico cat playing a piano on stage",
    image_path=None,
    save_path=save2,
    model="MiniMax-H3",
    duration=5,
)
rec2 = RECORDS[1]
payload = json.loads(rec2["body"])
check("sglang 请求为 JSON", "application/json" in rec2["ctype"], rec2["ctype"])
check("sglang JSON 含 task=t2va", payload.get("task") == "t2va", str(payload)[:140])
check("sglang 产物落盘", os.path.exists(save2) and os.path.getsize(save2) > 0, f"{os.path.getsize(save2) if os.path.exists(save2) else 0}B")

# 3) vllm-omni sync 同步端点：一次请求直出 mp4（官方 recipes 主形态）
save3 = "/tmp/h3_e2e_sync.mp4"
remote3 = make("vllm-omni").generate_video(
    prompt="sync endpoint path",
    image_path=p1,
    save_path=save3,
    model="MiniMax-H3",
    duration=5,
)
rec3 = RECORDS[2]
sync_text = rec3["body"].decode("utf-8", "ignore")
check("sync 端点为 /videos/sync", rec3["path"].endswith("/videos/sync"), rec3["path"])
check("sync 含 extra_params task=fl2va", '"task": "fl2va"' in sync_text, "")
check("sync 流程仅一次请求（无异步创建）", len(RECORDS) == 3, str(len(RECORDS)))
check("sync 产物落盘", os.path.exists(save3) and os.path.getsize(save3) > 0, f"{os.path.getsize(save3) if os.path.exists(save3) else 0}B")
check("sync 返回远端标识为 sync 端点", remote3.endswith("/videos/sync"), remote3)

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)