"""任务 3.1 验证脚本：四门面注册表优先路由（custom → 适配层；unknown → 报错；builtin 分支不变）。

在既有 Docker 容器内运行：
  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - \
    < tmp_verify/verify_routing.py
"""
import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as cfg

PORT = 18098
OUT_DIR = "/tmp/verify_routing"
os.makedirs(OUT_DIR, exist_ok=True)

PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"ROUTE-MP4-" + b"y" * 64

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self._read()
        path = self.path.rstrip("/")
        if path == "/v1/chat/completions":
            return self._json(200, {"choices": [{"message": {"content": "ROUTE-MOCK-OK"}}]})
        if path == "/v1/images/generations":
            return self._json(200, {"data": [{"b64_json": PNG_1X1_B64}]})
        if path == "/v1/videos":
            return self._json(200, {"id": "route-job-1", "status": "queued"})
        return self._json(404, {})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/v1/videos/") and path.endswith("/content"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(MP4_BYTES)))
            self.end_headers()
            self.wfile.write(MP4_BYTES)
            return
        if re.match(r"^/v1/videos/route-job-1$", path):
            return self._json(200, {"id": "route-job-1", "status": "completed", "url": "/v1/videos/route-job-1/content"})
        return self._json(404, {})


server = ThreadingHTTPServer(("127.0.0.1", PORT), MockHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}/v1"

cfg.Config.CONFIG.setdefault("api_providers", {}).update({
    "route-mock": {"protocol": "openai", "base_url": BASE, "api_key": "", "enable_proxy": False},
})
cfg.Config.CONFIG["custom_models"] = [
    {"id": "route-llm", "provider": "route-mock", "model": "ROUTE-LLM", "types": ["llm", "vlm"], "abilities": []},
    {"id": "route-img", "provider": "route-mock", "model": "ROUTE-IMG", "types": ["t2i", "i2i"], "abilities": []},
    {"id": "route-vid", "provider": "route-mock", "model": "ROUTE-VID", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]},
]

from models.custom_common import ModelNotRegisteredError
from models.image_client import ImageClient
from models.llm_client import LLM
from models.video_client import VideoClient
from models.vlm_client import VLM

TEST_IMAGE = os.path.join(OUT_DIR, "test.png")
with open(TEST_IMAGE, "wb") as f:
    f.write(base64.b64decode(PNG_1X1_B64))

# ── custom：四门面走适配层 ──
answer = LLM().query("你好", model="route-llm")
check("3.1 LLM 自定义路由到适配层", answer.strip() == "ROUTE-MOCK-OK", answer)

answer = VLM().query("看图", image_paths=[TEST_IMAGE], model="route-llm")
check("3.1 VLM 自定义路由到适配层", answer.strip() == "ROUTE-MOCK-OK", answer)

image_paths = ImageClient().generate_image("画图", model="route-img", save_dir=OUT_DIR, video_ratio="16:9", resolution="720P")
check("3.1 ImageClient 自定义路由到适配层", len(image_paths) == 1 and os.path.exists(image_paths[0]))

video_save = os.path.join(OUT_DIR, "route.mp4")
remote = VideoClient().generate_video(prompt="动起来", image_path=None, save_path=video_save, model="route-vid", duration=2)
check("3.1 VideoClient 自定义路由到适配层", os.path.exists(video_save) and remote.endswith("/content"), remote)

# ── unknown：四门面均明确报错 ──
for label, fn in (
    ("LLM", lambda: LLM().query("x", model="definitely-unknown-xyz")),
    ("VLM", lambda: VLM().query("x", model="definitely-unknown-xyz")),
    ("ImageClient", lambda: ImageClient().generate_image("x", model="definitely-unknown-xyz")),
    ("VideoClient", lambda: VideoClient().generate_video(prompt="x", image_path=None, save_path=os.path.join(OUT_DIR, "x.mp4"), model="definitely-unknown-xyz")),
):
    try:
        fn()
        check(f"3.1 {label} 未注册模型明确报错", False, "未抛错")
    except ModelNotRegisteredError as exc:
        check(f"3.1 {label} 未注册模型明确报错", "未注册" in str(exc), str(exc)[:50])


# ── builtin：原分支未受影响（以桩替换内部客户端断言被调用）──
class StubDashscopeImage:
    called = None

    def generate_image(self, **kwargs):
        StubDashscopeImage.called = kwargs
        return ["/tmp/stub_image.png"]

    def edit_image(self, **kwargs):
        StubDashscopeImage.called = kwargs
        return ["/tmp/stub_image.png"]


class StubKlingVideo:
    called = None

    def generate_video(self, *args, **kwargs):
        StubKlingVideo.called = kwargs
        return "https://stub/kling.mp4"


class StubGPT:
    called = None

    def query(self, prompt, image_urls=None, model="", web_search=False):
        StubGPT.called = {"prompt": prompt, "model": model}
        return "stub-gpt"


class StubGemini:
    called = None

    def chat(self, text, images=None, model=""):
        StubGemini.called = {"prompt": text, "model": model}
        return "stub-gemini"


img_client = ImageClient()
img_client._dashscope_client = StubDashscopeImage()
paths = img_client.generate_image("写实风格", model="wan2.7-image")
check("3.1 内置图像模型仍走 DashScope 分支", StubDashscopeImage.called is not None and paths == ["/tmp/stub_image.png"])

video_client = VideoClient()
video_client._kling_client = StubKlingVideo()
url = video_client.generate_video(prompt="p", image_path=None, save_path=os.path.join(OUT_DIR, "k.mp4"), model="kling-v2", duration=5)
check("3.1 内置视频模型仍走 Kling 分支", StubKlingVideo.called is not None and url == "https://stub/kling.mp4")

llm_client = LLM()
llm_client._gpt_client = StubGPT()
check("3.1 内置 LLM 仍走 GPT 分支", llm_client.query("hi", model="gpt-4o") == "stub-gpt" and StubGPT.called["model"] == "gpt-4o")

vlm_client = VLM()
vlm_client._gemini_client = StubGemini()
check("3.1 内置 VLM 仍走 Gemini 分支", vlm_client.query("看", model="gemini-2.0-flash") == "stub-gemini")

server.shutdown()
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项未通过: {FAILURES}")
    sys.exit(1)
print("ALL PASS")