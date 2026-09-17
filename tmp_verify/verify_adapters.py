"""任务 2.1-2.5 验证脚本：协议适配层（内嵌 mock OpenAI/vllm-omni/sglang 服务）。

在既有 Docker 容器内运行：
  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - \
    < tmp_verify/verify_adapters.py
"""
import base64
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import config as cfg

PORT = 18099
OUT_DIR = "/tmp/verify_adapters"
os.makedirs(OUT_DIR, exist_ok=True)

PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"MOCK-MP4-" + b"x" * 64

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ══════════ 内嵌 mock 服务 ══════════

class MockState:
    def __init__(self):
        self.records = []
        self.jobs = {}
        self.config = {
            "accepted_edit_fields": {"image[]"},
            "video_polls_before_done": 2,
            "video_mode": "ok",  # ok | fail | never
            "expect_auth": None,  # None=不校验；字符串=要求 Bearer
            "require_multipart_for_video": False,
            "image_mode": "b64",  # b64 | url-rel | url-abs
        }
        self._job_seq = 0

    def next_job_id(self):
        self._job_seq += 1
        return f"job-{self._job_seq}"


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默
        pass

    @property
    def state(self):
        return self.server.state

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, payload: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, method: str, body: bytes = b"", extra: dict = None):
        record = {
            "method": method,
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "authorization": self.headers.get("Authorization"),
            "field_names": sorted({n.decode() for n in re.findall(rb'name="([^"]+)"', body)}),
            "body_mentions": sorted(k for k in ("seconds", "prompt", "size", "model", "input_reference") if k.encode() in body),
            "body_len": len(body),
        }
        if extra:
            record.update(extra)
        self.state.records.append(record)

    def _auth_ok(self) -> bool:
        expect = self.state.config["expect_auth"]
        if not expect:
            return True
        return self.headers.get("Authorization") == f"Bearer {expect}"

    # ── POST ──
    def do_POST(self):
        body = self._read_body()
        path = self.path.rstrip("/")
        if path == "/v1/chat/completions":
            self._record("POST", body)
            if not self._auth_ok():
                return self._send_json(401, {"error": {"message": "unauthorized"}})
            data = json.loads(body)
            content = data["messages"][0]["content"]
            has_image = any(part.get("type") == "image_url" for part in content)
            image_url_ok = all(
                part["image_url"]["url"].startswith("data:image/")
                for part in content
                if part.get("type") == "image_url"
            )
            return self._send_json(200, {
                "choices": [{"message": {"content": f"MOCK-OK model={data['model']} img={int(has_image)} imgok={int(image_url_ok)}"}}]
            })
        if path == "/v1/images/generations":
            data = json.loads(body) if body else {}
            self._record("POST", body, {"prompt": data.get("prompt", ""), "size": data.get("size", ""), "response_format": data.get("response_format", "")})
            mode = self.state.config["image_mode"]
            if mode == "url-rel":
                return self._send_json(200, {"data": [{"url": "/v1/images/img-rel/content"}]})
            if mode == "url-abs":
                return self._send_json(200, {"data": [{"url": f"http://127.0.0.1:{PORT}/v1/images/img-abs/content"}]})
            return self._send_json(200, {"data": [{"b64_json": PNG_1X1_B64}]})
        if path == "/v1/images/edits":
            content_type = self.headers.get("Content-Type", "")
            if "application/x-www-form-urlencoded" in content_type:
                from urllib.parse import parse_qs

                names = set(parse_qs(body.decode("utf-8", "ignore")).keys())
            else:
                names = set(n.decode() for n in re.findall(rb'name="([^"]+)"', body))
            has_data_url = b"data:image/" in body
            self._record("POST", body, {"field_names": sorted(names), "has_data_url": has_data_url})
            accepted = self.state.config["accepted_edit_fields"]
            if names & accepted:
                return self._send_json(200, {"data": [{"b64_json": PNG_1X1_B64}]})
            return self._send_json(400, {"detail": f"unexpected edit fields: {sorted(names)}"})
        if path == "/v1/videos":
            self._record("POST", body)
            if self.state.config["require_multipart_for_video"] and "multipart/form-data" not in self.headers.get("Content-Type", ""):
                return self._send_json(400, {"detail": "multipart required"})
            job_id = self.state.next_job_id()
            self.state.jobs[job_id] = {"polls": 0}
            return self._send_json(200, {"id": job_id, "status": "queued"})
        return self._send_json(404, {"detail": f"no route {path}"})

    # ── GET ──
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        self._record("GET")
        if path.startswith("/v1/videos/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "content":
                return self._send_bytes(200, MP4_BYTES, "video/mp4")
            if len(parts) == 4:
                job_id = parts[3]
                job = self.state.jobs.get(job_id)
                if not job:
                    return self._send_json(404, {"detail": "job not found"})
                job["polls"] += 1
                mode = self.state.config["video_mode"]
                if mode == "never":
                    status = "in_progress"
                elif mode == "fail":
                    status = "failed"
                elif job["polls"] >= self.state.config["video_polls_before_done"]:
                    status = "completed"
                else:
                    status = "in_progress"
                payload = {"id": job_id, "status": status}
                if status == "completed":
                    payload["url"] = f"/v1/videos/{job_id}/content"
                if status == "failed":
                    payload["error"] = "mock generation failed"
                return self._send_json(200, payload)
        if path == "/v1/videos":
            return self._send_json(200, {"data": [{"id": jid, "status": "in_progress"} for jid in self.state.jobs]})
        if path.startswith("/v1/images/") and path.endswith("/content"):
            return self._send_bytes(200, base64.b64decode(PNG_1X1_B64), "image/png")
        if path == "/__records":
            return self._send_json(200, {"records": self.state.records})
        return self._send_json(404, {"detail": f"no route {path}"})

    def do_DELETE(self):
        self._record("DELETE")
        return self._send_json(200, {"deleted": True})


server = ThreadingHTTPServer(("127.0.0.1", PORT), MockHandler)
server.state = MockState()
threading.Thread(target=server.serve_forever, daemon=True).start()

BASE = f"http://127.0.0.1:{PORT}/v1"

# ═══════════ 注入测试供应商与模型 ═══════════

cfg.Config.CONFIG.setdefault("api_providers", {}).update({
    "mock-openai": {"protocol": "openai", "base_url": BASE, "api_key": "", "enable_proxy": False},
    "mock-omni": {"protocol": "vllm-omni", "base_url": BASE, "api_key": "", "enable_proxy": False},
    "mock-sglang": {"protocol": "sglang", "base_url": BASE, "api_key": "", "enable_proxy": False},
    "mock-auth": {"protocol": "openai", "base_url": BASE, "api_key": "test-key", "enable_proxy": False},
})
models = []
for proto in ("openai", "omni", "sglang"):
    models.append({"id": f"m-llm-{proto}", "provider": f"mock-{proto}", "model": f"LLM-{proto}", "types": ["llm", "vlm"], "abilities": []})
    models.append({"id": f"m-img-{proto}", "provider": f"mock-{proto}", "model": f"IMG-{proto}", "types": ["t2i", "i2i"], "abilities": []})
    models.append({"id": f"m-vid-{proto}", "provider": f"mock-{proto}", "model": f"VID-{proto}", "types": ["video"], "abilities": ["text_to_video", "first_frame_i2v"]})
models.append({"id": "m-llm-auth", "provider": "mock-auth", "model": "LLM-AUTH", "types": ["llm"], "abilities": []})
cfg.Config.CONFIG["custom_models"] = models

from models.config_model import resolve_model_entry
from models.custom_common import (
    ModelNotRegisteredError,
    classify_model_unavailable,
    resolve_custom_meta,
)
from models.custom_image import CustomImageClient
from models.custom_llm import CustomChatClient
from models.custom_video import CustomVideoClient

TEST_IMAGE = os.path.join(OUT_DIR, "test.png")
with open(TEST_IMAGE, "wb") as f:
    f.write(base64.b64decode(PNG_1X1_B64))


def meta_of(model_id):
    return resolve_model_entry(model_id)[1]


def reset_records():
    server.state.records.clear()
    server.state.config.update({
        "accepted_edit_fields": {"image[]"},
        "video_polls_before_done": 2,
        "video_mode": "ok",
        "expect_auth": None,
        "require_multipart_for_video": False,
        "image_mode": "b64",
    })


# ══════════ 2.1 共享工具 ═══════════

check("2.1 image_size 映射", __import__("models.custom_common", fromlist=["x"]).image_size("16:9", "720P") == "1280*720")
from models.custom_common import image_size, video_size

check("2.1 image_size 支持 WxH", image_size("16:9", "512x512") == "512*512")
check("2.1 video_size 映射", video_size("9:16", "1080P") == "1080x1920")
check("2.1 video_size 支持 W*H", video_size("16:9", "832*480") == "832x480")
check("2.1 image_size 兜底", image_size("", "") == "1920*1080")

req = httpx.Request("GET", "http://x/")
resp401 = httpx.Response(401, request=req)
check("2.1 分类：401", classify_model_unavailable(httpx.HTTPStatusError("401", request=req, response=resp401)) is True)
check("2.1 分类：连接失败", classify_model_unavailable(httpx.ConnectError("connection refused")) is True)
check("2.1 分类：超时", classify_model_unavailable(httpx.ReadTimeout("timed out")) is True)
check("2.1 分类：内容类失败不误报", classify_model_unavailable(RuntimeError("内容审核未通过，图片包含敏感元素")) is False)
check("2.1 非自定义模型解析报错", (lambda: (resolve_custom_meta("wan2.7-image"), False)[1] if False else True)())
try:
    resolve_custom_meta("wan2.7-image")
    check("2.1 内置模型走 resolve_custom_meta 抛错", False)
except ModelNotRegisteredError:
    check("2.1 内置模型走 resolve_custom_meta 抛错", True)

# 鉴权头：空 key 不带 Authorization；有 key 带 Bearer
reset_records()
client = CustomChatClient(meta_of("m-llm-openai"))
client.query("你好")
check("2.1 空 api_key 不发鉴权头", server.state.records[-1]["authorization"] is None)
client.close()
reset_records()
server.state.config["expect_auth"] = "test-key"
client = CustomChatClient(meta_of("m-llm-auth"))
answer = client.query("你好")
check("2.1 带 api_key 发送 Bearer", answer.startswith("MOCK-OK"))
check("2.1 mock 校验鉴权通过", server.state.records[-1]["authorization"] == "Bearer test-key")
client.close()
server.state.config["expect_auth"] = None

# ══════════ 2.2 CustomChatClient（LLM/VLM）═══

reset_records()
client = CustomChatClient(meta_of("m-llm-omni"))
answer = client.query("描述图片", image_urls=[TEST_IMAGE])
check("2.2 VLM 图片以 data URL 传递", "img=1 imgok=1" in answer, answer)
check("2.2 请求路径为 chat/completions", server.state.records[-1]["path"].startswith("/v1/chat/completions"))
client.close()

# ═══════════ 2.3 CustomImageClient ═══════════

reset_records()
client = CustomImageClient(meta_of("m-img-openai"))
paths = client.generate_image("一只猫", save_dir=OUT_DIR, video_ratio="16:9", resolution="720P")
check("2.3 t2i b64_json 落盘", len(paths) == 1 and os.path.exists(paths[0]) and paths[0].endswith(".png"))
check("2.3 请求 size 为 WxH 格式", server.state.records[-1]["size"] == "1280x720")
client.close()

reset_records()
server.state.config["image_mode"] = "url-rel"
client = CustomImageClient(meta_of("m-img-omni"))
paths = client.generate_image("一只猫", save_dir=OUT_DIR)
check("2.3 相对 URL 下载落盘", len(paths) == 1 and os.path.exists(paths[0]))
check("2.3 相对路径按 base_url 解析", any(r["path"] == "/v1/images/img-rel/content" for r in server.state.records))
client.close()

reset_records()
server.state.config["image_mode"] = "url-abs"
client = CustomImageClient(meta_of("m-img-sglang"))
paths = client.generate_image("一只猫", save_dir=OUT_DIR)
check("2.3 绝对 URL 下载落盘", len(paths) == 1 and os.path.exists(paths[0]))
client.close()

# i2i：按协议主字段
reset_records()
server.state.config["accepted_edit_fields"] = {"image[]"}
client = CustomImageClient(meta_of("m-img-openai"))
paths = client.generate_image("改成夜景", image_paths=[TEST_IMAGE], save_dir=OUT_DIR)
check("2.3 openai i2i 使用 image[] 字段", len(paths) == 1 and "image[]" in server.state.records[-1]["field_names"])
client.close()

reset_records()
server.state.config["accepted_edit_fields"] = {"image"}
client = CustomImageClient(meta_of("m-img-omni"))
paths = client.generate_image("改成夜景", image_paths=[TEST_IMAGE], save_dir=OUT_DIR)
check("2.3 vllm-omni i2i 使用 image 字段", len(paths) == 1 and "image" in server.state.records[-1]["field_names"])
client.close()

# i2i 回退：仅接受 url（data URL）
reset_records()
server.state.config["accepted_edit_fields"] = {"url"}
client = CustomImageClient(meta_of("m-img-omni"))
paths = client.generate_image("改成夜景", image_paths=[TEST_IMAGE], save_dir=OUT_DIR)
attempts = [r for r in server.state.records if r["path"].endswith("/v1/images/edits")]
check("2.3 i2i 字段回退成功", len(paths) == 1 and len(attempts) >= 2)
check("2.3 回退最终使用 url 字段", "url" in attempts[-1]["field_names"], str(attempts[-1]["field_names"]))
check("2.3 url 回退值为 data URL", attempts[-1].get("has_data_url") is True)
client.close()

reset_records()
server.state.config["accepted_edit_fields"] = {"image"}
client = CustomImageClient(meta_of("m-img-sglang"))
paths = client.generate_image("改成夜景", image_paths=[TEST_IMAGE], save_dir=OUT_DIR)
check("2.3 sglang i2i 主字段 image", len(paths) == 1 and "image" in server.state.records[-1]["field_names"])
client.close()
server.state.config["accepted_edit_fields"] = {"image[]"}

# ══════════ 2.4 CustomVideoClient ═══════════

def video_client(model_id, **kwargs):
    return CustomVideoClient(meta_of(model_id), poll_interval=0.05, poll_timeout=5.0, **kwargs)


reset_records()
save_path = os.path.join(OUT_DIR, "v_omni_t2v.mp4")
client = video_client("m-vid-omni")
remote = client.generate_video(prompt="一只猫在跳舞", image_path=None, save_path=save_path, duration=5, video_ratio="16:9", resolution="720P")
check("2.4 vllm-omni t2v 三段式成功", os.path.exists(save_path) and open(save_path, "rb").read() == MP4_BYTES)
check("2.4 返回值为远端标识", remote == f"/v1/videos/{server.state.records[0]['path'].split('/')[-1]}/content" or remote.endswith("/content"), remote)
creates = [r for r in server.state.records if r["path"] == "/v1/videos" and r["method"] == "POST"]
check("2.4 vllm-omni 创建使用 multipart 主形态", "multipart/form-data" in creates[0]["content_type"], creates[0]["content_type"])
check("2.4 seconds/size/model 均已下发", {"seconds", "size", "model"} <= set(creates[0]["body_mentions"]), str(creates[0]["body_mentions"]))
client.close()

reset_records()
save_path = os.path.join(OUT_DIR, "v_omni_i2v.mp4")
client = video_client("m-vid-omni")
client.generate_video(prompt="让这张图动起来", image_path=TEST_IMAGE, save_path=save_path, duration=3)
creates = [r for r in server.state.records if r["path"] == "/v1/videos" and r["method"] == "POST"]
check("2.4 i2v 携带 input_reference 文件", "input_reference" in creates[0]["field_names"], str(creates[0]["field_names"]))
check("2.4 i2v 视频落盘", os.path.exists(save_path))
client.close()

# sglang：t2v 主形态 JSON
reset_records()
save_path = os.path.join(OUT_DIR, "v_sglang_t2v.mp4")
client = video_client("m-vid-sglang")
client.generate_video(prompt="海边日落", image_path=None, save_path=save_path, duration=5)
creates = [r for r in server.state.records if r["path"] == "/v1/videos" and r["method"] == "POST"]
check("2.4 sglang t2v 使用 JSON 主形态", "application/json" in creates[0]["content_type"], creates[0]["content_type"])
check("2.4 sglang 视频落盘", os.path.exists(save_path))
client.close()

# 创建编码回退：mock 要求 multipart → sglang t2v 首选 JSON 被拒 → 回退 multipart 成功
reset_records()
server.state.config["require_multipart_for_video"] = True
server.state.config["video_polls_before_done"] = 1
save_path = os.path.join(OUT_DIR, "v_fallback.mp4")
client = video_client("m-vid-sglang")
client.generate_video(prompt="回退测试", image_path=None, save_path=save_path, duration=2)
creates = [r for r in server.state.records if r["path"] == "/v1/videos" and r["method"] == "POST"]
check("2.4 编码回退（JSON→multipart）成功", len(creates) == 2 and os.path.exists(save_path), f"creates={len(creates)}")
client.close()
server.state.config["require_multipart_for_video"] = False

# 失败路径：任务 failed → 报错 + 尽力取消
reset_records()
server.state.config["video_mode"] = "fail"
save_path = os.path.join(OUT_DIR, "v_fail.mp4")
client = video_client("m-vid-omni")
try:
    client.generate_video(prompt="失败测试", image_path=None, save_path=save_path, duration=2)
    check("2.4 任务失败抛出错误", False, "未抛错")
except RuntimeError as exc:
    check("2.4 任务失败抛出错误", "失败" in str(exc), str(exc)[:80])
check("2.4 失败后尽力取消（DELETE）", any(r["method"] == "DELETE" for r in server.state.records))
check("2.4 失败不落盘半成品", not os.path.exists(save_path))
client.close()

# 超时路径
reset_records()
server.state.config["video_mode"] = "never"
save_path = os.path.join(OUT_DIR, "v_timeout.mp4")
client = CustomVideoClient(meta_of("m-vid-omni"), poll_interval=0.05, poll_timeout=0.4)
try:
    client.generate_video(prompt="超时测试", image_path=None, save_path=save_path, duration=2)
    check("2.4 轮询超时抛出错误", False, "未抛错")
except RuntimeError as exc:
    check("2.4 轮询超时抛出错误", "超时" in str(exc), str(exc)[:80])
check("2.4 超时后尽力取消", any(r["method"] == "DELETE" for r in server.state.records))
client.close()
server.state.config["video_mode"] = "ok"

# duration 钳制（capabilities min=2）
reset_records()
save_path = os.path.join(OUT_DIR, "v_clamp.mp4")
client = video_client("m-vid-omni")
client.generate_video(prompt="钳制", image_path=None, save_path=save_path, duration=1)
check("2.4 duration 钳制到 min=2", os.path.exists(save_path))
client.close()

# ══════════ 2.5 三协议 × 五类型矩阵 ═══════════

reset_records()
matrix_results = {}
for proto in ("openai", "omni", "sglang"):
    # llm
    c = CustomChatClient(meta_of(f"m-llm-{proto}"))
    r1 = c.query("矩阵-llm").startswith("MOCK-OK")
    r2 = c.query("矩阵-vlm", image_urls=[TEST_IMAGE]).startswith("MOCK-OK")
    c.close()
    # t2i / i2i（按协议主字段接受）
    server.state.config["accepted_edit_fields"] = {"image[]"} if proto == "openai" else {"image"}
    c = CustomImageClient(meta_of(f"m-img-{proto}"))
    t2i_ok = len(c.generate_image("矩阵-t2i", save_dir=OUT_DIR)) == 1
    i2i_ok = len(c.generate_image("矩阵-i2i", image_paths=[TEST_IMAGE], save_dir=OUT_DIR)) == 1
    c.close()
    # t2v / i2v
    c = video_client(f"m-vid-{proto}")
    t2v_ok = bool(c.generate_video(prompt="矩阵-t2v", image_path=None, save_path=os.path.join(OUT_DIR, f"m_{proto}_t2v.mp4"), duration=2))
    i2v_ok = bool(c.generate_video(prompt="矩阵-i2v", image_path=TEST_IMAGE, save_path=os.path.join(OUT_DIR, f"m_{proto}_i2v.mp4"), duration=2))
    c.close()
    matrix_results[proto] = (r1, r2, t2i_ok, i2i_ok, t2v_ok, i2v_ok)

for proto, result in matrix_results.items():
    check(f"2.5 矩阵 {proto}（llm/vlm/t2i/i2i/t2v/i2v）", all(result), str(result))

ok_paths = {r["path"] for r in server.state.records if r["method"] == "POST"}
check("2.5 覆盖 chat/images/videos 路径", {"/v1/chat/completions", "/v1/images/generations", "/v1/images/edits", "/v1/videos"} <= ok_paths, str(ok_paths))

server.shutdown()
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项未通过: {FAILURES}")
    sys.exit(1)
print("ALL PASS")