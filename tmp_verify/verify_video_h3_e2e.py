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
# 测试辅助：通过 VC_PATCHED_BACKEND 优先加载补丁代码副本（镜像重建后无需该变量）
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []
RECORDS = []
FAIL_ALL_POST = False  # 4xx 失败语义场景：置 True 后所有 POST 返回 400
FAIL_SYNC = False  # 场景开关：置 True 后 /videos/sync 返回 404（强制回退异步三段式）


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
        if FAIL_ALL_POST:
            self._json({"error": "media reference rejected"}, status=400)
            return
        if self.path.endswith("/videos/sync"):
            if FAIL_SYNC:
                self._json({"error": "sync endpoint missing"}, status=404)
                return
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

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
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

# ── 媒体参考场景（vllm-omni ref2va 音视频参考） ──
v1, v2 = "/tmp/h3_e2e_v1.mp4", "/tmp/h3_e2e_v2.mp4"
for path in (v1, v2):
    with open(path, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42" + b"v" * 16)

# 4) 图片参考 + 音频 URL：sync 端点携带 audio_reference JSON 字段
save4 = "/tmp/h3_e2e_audio.mp4"
make("vllm-omni").generate_video(
    prompt="A woman speaking with lip-sync.",
    image_path=p1,
    save_path=save4,
    model="MiniMax-H3",
    duration=5,
    audio_reference_url="http://127.0.0.1:9999/audio/ref.wav",
)
rec4 = RECORDS[3]
audio_text = rec4["body"].decode("utf-8", "ignore")
check("sync 音频参考：audio_reference 含 audio_url JSON", '{"audio_url": "http://127.0.0.1:9999/audio/ref.wav"}' in audio_text, audio_text[:160])
check("sync 音频参考：图片参考字段仍在", 'name="input_reference"' in audio_text, str(audio_text.count('name="input_reference"')))
check("sync 音频参考：产物落盘", os.path.exists(save4) and os.path.getsize(save4) > 0, f"{os.path.getsize(save4) if os.path.exists(save4) else 0}B")

# 5) 多视频混合参考（异步三段式）：input_references 重复携带 video/mp4
FAIL_SYNC = True  # 强制回退异步，验证三段式链路的视频参考
save5 = "/tmp/h3_e2e_multivideo.mp4"
remote5 = make("vllm-omni").generate_video(
    prompt="Merge subject video and background video.",
    image_path=None,
    save_path=save5,
    model="MiniMax-H3",
    duration=5,
    reference_video_paths=[v1, v2],
)
FAIL_SYNC = False
rec5 = RECORDS[5]  # RECORDS[4] 为 sync 404 请求（FAIL_SYNC 强制回退）
mv_text = rec5["body"].decode("utf-8", "ignore")
check("多视频：multipart 请求", "multipart/form-data" in rec5["ctype"], rec5["ctype"])
check("多视频：sync 404 后回退异步创建", rec5["path"].endswith("/videos") and not rec5["path"].endswith("/videos/sync"), rec5["path"])
check("多视频：两个 input_references 文件字段", mv_text.count('name="input_references"') == 2, str(mv_text.count('name="input_references"')))
check("多视频：文件 MIME 为 video/mp4", 'Content-Type: video/mp4' in mv_text, "")
check("多视频：extra_params task=ref2va", '"task": "ref2va"' in mv_text, mv_text[:140])
check("多视频：产物落盘", os.path.exists(save5) and os.path.getsize(save5) > 0, f"{os.path.getsize(save5) if os.path.exists(save5) else 0}B")
check("多视频：返回远端标识", bool(remote5), remote5[:80])

# 6) 失败语义：服务端 4xx → 最终错误包含服务端响应，且绝不出现"无参考"请求
FAIL_ALL_POST = True
RECORDS.clear()
save6 = "/tmp/h3_e2e_fail.mp4"
try:
    make("vllm-omni").generate_video(
        prompt="should fail with media reference",
        image_path=None,
        save_path=save6,
        model="MiniMax-H3",
        duration=5,
        reference_video_paths=[v1],
    )
    check("失败语义：带参考请求必须失败", False, "未抛出异常")
except RuntimeError as exc:
    message = str(exc)
    check("失败语义：最终错误包含服务端响应", "HTTP 400" in message and "media reference rejected" in message, message[:160])
    check("失败语义：错误包含目标地址", "/videos" in message, message[:120])
except Exception as exc:  # noqa: BLE001
    check("失败语义：仅允许明确失败", False, f"{type(exc).__name__}: {exc}")
all_with_media = all('filename="' in r["body"].decode("utf-8", "ignore") for r in RECORDS)
check("失败语义：无'无参考'请求记录", RECORDS and all_with_media, f"{len(RECORDS)} 次请求" + ("" if all_with_media else " 存在无参考请求"))
check("失败语义：失败时不落盘产物", not os.path.exists(save6), str(os.path.exists(save6)))
FAIL_ALL_POST = False

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)