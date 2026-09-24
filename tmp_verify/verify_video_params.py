"""统一视频参数系统验证脚本（任务 1.1-1.3 / 2.1-2.3 / 3.1-3.2）。

覆盖：
  - 1.1 三协议字段矩阵（默认路径等价 + 高级参数触发的协议字段注入）
  - 1.2 尺寸推导（720P/1080P × 主流比例；short_edge 声明 → short_edge+aspect_ratio）
  - 1.3 时长一致性（clamped duration → extra_params.duration / seconds / target.duration_seconds 一致）
  - 2.1 capabilities 归一化（duration{min,max}/fps[]/short_edge/ratios[] 校验）
  - 2.2 越界夹取与忽略记录（fps 夹取/忽略、duration 夹取日志与 adjustments）
  - 2.3 /api/models capabilities 输出（含 fps/short_edge/duration）
  - 3.1 沙盒视频 Schema/路由 fps/short_edge 透传 + warnings 透出
  - 3.2 会话级参数（SESSION_PARAM_KEYS / 白名单 / _generate_one 透传）

运行：docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_video_params.py
"""
import asyncio
import io
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/app")

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"1" * 64
RECORDS = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        RECORDS.append({"path": self.path, "ctype": self.headers.get("Content-Type", ""), "body": body})
        if self.path.endswith("/videos/sync"):
            self._bytes(MP4, "video/mp4")
            return
        self._json({"id": "v-1", "status": "queued"})

    def do_GET(self):
        if self.path.endswith("/content"):
            self._bytes(MP4, "video/mp4")
            return
        self._json({"id": "v-1", "status": "completed", "url": self.path.rsplit("/", 1)[0] + "/v-1/content"})

    def _json(self, payload):
        self._bytes(json.dumps(payload).encode(), "application/json")

    def _bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"== 自包含 mock 已启动: 127.0.0.1:{port}\n")

from models.custom_video import CustomVideoClient  # noqa: E402
from models.custom_common import video_size, video_dimensions  # noqa: E402


def multipart_fields(body: bytes) -> dict:
    fields: dict = {}
    for match in re.finditer(rb'name="([^"]+)"\r\n\r\n(.*?)\r\n--', body, re.S):
        fields.setdefault(match.group(1).decode(), []).append(match.group(2).decode("utf-8", "ignore"))
    return fields


def make(protocol, capabilities=None):
    meta = {"id": "m", "request_model": "M", "base_url": f"http://127.0.0.1:{port}/v1", "protocol": protocol, "api_key": ""}
    if capabilities is not None:
        meta["capabilities"] = capabilities
    return CustomVideoClient(meta, poll_interval=0.05, poll_timeout=3.0)


def last_record():
    return RECORDS[-1]


# ═══ 1.2 尺寸推导 ═══

check("1.2 16:9 720P", video_dimensions("16:9", "720P") == (1280, 720))
check("1.2 16:9 1080P", video_dimensions("16:9", "1080P") == (1920, 1080))
check("1.2 9:16 720P", video_dimensions("9:16", "720P") == (720, 1280))
check("1.2 9:16 1080P", video_dimensions("9:16", "1080P") == (1080, 1920))
check("1.2 1:1 720P", video_dimensions("1:1", "720P") == (720, 720))
check("1.2 1:1 1080P", video_dimensions("1:1", "1080P") == (1080, 1080))
check("1.2 4:3 720P", video_dimensions("4:3", "720P") == (960, 720))
check("1.2 4:3 1080P", video_dimensions("4:3", "1080P") == (1440, 1080))
check("1.2 3:4 720P", video_dimensions("3:4", "720P") == (720, 960))
check("1.2 3:4 1080P", video_dimensions("3:4", "1080P") == (1080, 1440))
check("1.2 21:9 720P", video_dimensions("21:9", "720P") == (1680, 720))
check("1.2 21:9 1080P", video_dimensions("21:9", "1080P") == (2520, 1080))

H3_CAPS = {"duration": {"min": 4, "max": 15}, "fps": [24], "short_edge": 768}

# ═══ 1.1 默认路径等价（无高级参数、模型未声明 short_edge）═══

RECORDS.clear()
make("openai", {"duration": {"min": 2, "max": 10}}).generate_video(prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=5)
f = multipart_fields(last_record()["body"])
check("1.1 openai 默认含 size/seconds", f.get("size") == ["1280x720"] and f.get("seconds") == ["5"], str(f))
check("1.1 openai 默认无新增字段", not ({"width", "height", "fps", "short_edge", "target"} & set(f)), str(sorted(f)))

RECORDS.clear()
make("vllm-omni", {"duration": {"min": 2, "max": 10}}).generate_video(prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=5)
f = multipart_fields(last_record()["body"])
check("1.1 vllm-omni 默认含 size", f.get("size") == ["1280x720"], str(f))
check("1.1 vllm-omni 默认无 width/height/fps", not ({"width", "height", "fps", "short_edge"} & set(f)), str(sorted(f)))
check("1.1 vllm-omni 默认 extra_params.duration=5.0", '"duration": 5.0' in f.get("extra_params", [""])[0], str(f.get("extra_params")))

RECORDS.clear()
make("sglang", {"duration": {"min": 2, "max": 10}}).generate_video(prompt="t", image_path=None, save_path="/tmp/vp_s.mp4", duration=5)
payload = json.loads(last_record()["body"])
check("1.1 sglang 默认无 target", "target" not in payload, str(sorted(payload)))
check("1.1 sglang 默认 seconds=5", payload.get("seconds") == "5", str(payload.get("seconds")))

# ═══ 1.1 触发路径：vllm-omni width/height + fps ═══

RECORDS.clear()
make("vllm-omni", {"duration": {"min": 4, "max": 15}, "fps": [24]}).generate_video(
    prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=5, video_ratio="16:9", resolution="720P", fps=24)
f = multipart_fields(last_record()["body"])
check("1.1 vllm-omni 注入 width/height", f.get("width") == ["1280"] and f.get("height") == ["720"], str(f))
check("1.1 vllm-omni 触发时不再下发 size", "size" not in f, str(sorted(f)))
check("1.1 vllm-omni 注入 fps=24", f.get("fps") == ["24"], str(f))
check("1.1 vllm-omni seconds=5", f.get("seconds") == ["5"], str(f))

# ═══ 1.1/1.2 触发路径：short_edge 声明 → short_edge + aspect_ratio ═══

RECORDS.clear()
make("vllm-omni", dict(H3_CAPS)).generate_video(
    prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=5, video_ratio="9:16", resolution="720P")
f = multipart_fields(last_record()["body"])
check("1.2 vllm-omni short_edge=768", f.get("short_edge") == ["768"], str(f))
check("1.2 vllm-omni aspect_ratio=9:16", f.get("aspect_ratio") == ["9:16"], str(f))
check("1.2 vllm-omni 不下发 size/width/height", not ({"size", "width", "height"} & set(f)), str(sorted(f)))

# ═══ 1.1/1.3 sglang target 结构与时长一致性 ═══

RECORDS.clear()
make("sglang", dict(H3_CAPS)).generate_video(prompt="t", image_path=None, save_path="/tmp/vp_s.mp4", duration=20)
payload = json.loads(last_record()["body"])
check("1.3 duration 20 被夹取到 15", payload.get("seconds") == "15", str(payload.get("seconds")))
target = payload.get("target") or {}
check("1.1 sglang target.short_edge/aspect_ratio", target.get("short_edge") == 768 and target.get("aspect_ratio") == "16:9", str(target))
check("1.3 target.duration_seconds=15 与 seconds 一致", target.get("duration_seconds") == 15 and payload.get("seconds") == "15", str(target))

RECORDS.clear()
make("vllm-omni", dict(H3_CAPS)).generate_video(prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=20)
f = multipart_fields(last_record()["body"])
ep = json.loads(f.get("extra_params", ["{}"])[0])
check("1.3 extra_params.duration=15.0 与 seconds=15 一致", ep.get("duration") == 15.0 and f.get("seconds") == ["15"], f"{ep} {f.get('seconds')}")

# sglang fl2va（multipart）：target 以 JSON 字符串序列化
RECORDS.clear()
with open("/tmp/vp_img.png", "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
make("sglang", dict(H3_CAPS)).generate_video(prompt="t", image_path="/tmp/vp_img.png", save_path="/tmp/vp_s.mp4", duration=5)
f = multipart_fields(last_record()["body"])
target = json.loads(f.get("target", ["null"])[0]) if "target" in f else None
check("1.1 sglang multipart target JSON 字符串", isinstance(target, dict) and target.get("duration_seconds") == 5, str(f.get("target")))
check("1.1 sglang multipart 含 task", f.get("task") == ["fl2va"], str(f.get("task")))

# ═══ 2.2 fps 夹取与忽略 ═══

RECORDS.clear()
c = make("vllm-omni", {"duration": {"min": 4, "max": 15}, "fps": [24]})
c.generate_video(prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=5, fps=60)
f = multipart_fields(last_record()["body"])
check("2.2 fps 60 夹取到 24", f.get("fps") == ["24"], str(f))
check("2.2 fps 夹取记录 adjustments", any("fps 60 -> 24" in a for a in c._adjustments), str(c._adjustments))

RECORDS.clear()
c = make("vllm-omni", {"duration": {"min": 4, "max": 15}})
c.generate_video(prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=5, fps=24)
f = multipart_fields(last_record()["body"])
check("2.2 未声明 fps 时不注入", "fps" not in f, str(sorted(f)))
check("2.2 fps 忽略记录 adjustments", any("fps 24 已忽略" in a for a in c._adjustments), str(c._adjustments))

# ═══ 2.2 duration 夹取日志 ═══

log_stream = io.StringIO()
handler = logging.StreamHandler(log_stream)
handler.setLevel(logging.INFO)
vlog = logging.getLogger("models.custom_video")
vlog.addHandler(handler)
vlog.setLevel(logging.INFO)
RECORDS.clear()
make("vllm-omni", dict(H3_CAPS)).generate_video(prompt="t", image_path=None, save_path="/tmp/vp_o.mp4", duration=20)
vlog.removeHandler(handler)
check("2.2 duration 夹取写入日志", "duration 20s -> 15s" in log_stream.getvalue(), log_stream.getvalue()[:160])

# ═══ 2.1 capabilities 归一化 ═══

from models.config_model import _custom_model_capabilities  # noqa: E402

caps = _custom_model_capabilities(
    {"types": ["video"], "capabilities": {"duration": {"min": 4, "max": 15}, "fps": [30, 24, "24"], "short_edge": "768", "ratios": ["16:9", "9:16"]}},
    ["video"],
)
check("2.1 duration 4-15 生效", caps.get("duration", {}).get("min") == 4 and caps.get("duration", {}).get("max") == 15, str(caps.get("duration")))
check("2.1 fps 去重排序为 int 列表", caps.get("fps") == [24, 30], str(caps.get("fps")))
check("2.1 short_edge 转为 int", caps.get("short_edge") == 768, str(caps.get("short_edge")))
check("2.1 ratios 归一为字符串列表", caps.get("ratios") == ["16:9", "9:16"], str(caps.get("ratios")))

caps_bad = _custom_model_capabilities(
    {"types": ["video"], "capabilities": {"fps": "abc", "short_edge": "x", "duration": {"min": 20, "max": 5}}},
    ["video"],
)
check("2.1 非法 fps 声明被剔除", "fps" not in caps_bad, str(caps_bad.get("fps")))
check("2.1 非法 short_edge 声明被剔除", "short_edge" not in caps_bad, str(caps_bad.get("short_edge")))
check("2.1 duration min>max 收敛为 min", caps_bad.get("duration", {}).get("min") == 20 and caps_bad.get("duration", {}).get("max") == 20, str(caps_bad.get("duration")))

caps_default = _custom_model_capabilities({"types": ["video"], "abilities": []}, ["video"])
check("2.1 默认无 fps/short_edge 声明", "fps" not in caps_default and "short_edge" not in caps_default, str(sorted(caps_default)))
check("2.1 默认 duration 2-10", caps_default.get("duration", {}).get("min") == 2 and caps_default.get("duration", {}).get("max") == 10, str(caps_default.get("duration")))

# ═══ 2.3 /api/models capabilities 输出 ═══

import config as cfg  # noqa: E402

BASE = f"http://127.0.0.1:{port}/v1"
cfg.Config.CONFIG.setdefault("api_providers", {})["mock-vp"] = {"protocol": "sglang", "base_url": BASE, "api_key": "", "enable_proxy": False}
cfg.Config.CONFIG["custom_models"] = [
    {"id": "m-vp-h3", "provider": "mock-vp", "model": "MiniMax-H3", "types": ["video"], "abilities": ["text_to_video"],
     "capabilities": {"duration": {"min": 4, "max": 15}, "fps": [24], "short_edge": 768}},
]

class _Resp:
    def __init__(self):
        self.headers = {}

from api.routers.pipelines import get_api_models  # noqa: E402

models_payload = asyncio.run(get_api_models(response=_Resp(), media_type=None, model_type="video", ability=None, verified_only=False))
h3_entry = next((m for m in models_payload["models"] if m["id"] == "m-vp-h3"), None)
check("2.3 /api/models 下发 H3 模型", h3_entry is not None, str([m["id"] for m in models_payload["models"]])[:160])
if h3_entry:
    hcap = h3_entry.get("capabilities") or {}
    check("2.3 capabilities.duration 4-15", hcap.get("duration", {}).get("min") == 4 and hcap.get("duration", {}).get("max") == 15, str(hcap.get("duration")))
    check("2.3 capabilities.fps [24]", hcap.get("fps") == [24], str(hcap.get("fps")))
    check("2.3 capabilities.short_edge 768", hcap.get("short_edge") == 768, str(hcap.get("short_edge")))

# ═══ 3.1 沙盒 Schema/路由透传 ═══

from api.schemas.sandbox import SandboxVideoRequest  # noqa: E402

req = SandboxVideoRequest(model="m", prompt="p", fps=24, short_edge=768)
check("3.1 Schema 支持 fps/short_edge", req.fps == 24 and req.short_edge == 768)

import api.routers.sandbox as sb  # noqa: E402

captured = {}


def fake_generate(self=None, **kwargs):
    captured.update(kwargs)
    adjustments = kwargs.get("adjustments")
    if isinstance(adjustments, list):
        adjustments.append("duration 20s -> 15s（模型能力范围 4-15s）")
    return "mock://video"


VideoClientCls = __import__("models.video_client", fromlist=["VideoClient"]).VideoClient
VideoClientCls.generate_video = fake_generate
sb._start_active_task = lambda *a, **k: "t-1"
sb._add_record = lambda **k: "r-1"
sb._converted_video_path = lambda p: p

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(sb.router)
tc = TestClient(app)
resp = tc.post("/api/sandbox/video", json={"model": "m", "prompt": "p", "duration": 20, "fps": 24, "short_edge": 768})
data = resp.json()
check("3.1 路由透传 fps/short_edge", captured.get("fps") == 24 and captured.get("short_edge") == 768, str({k: type(captured.get(k)) for k in ("fps", "short_edge")}))
check("3.1 路由接收 adjustments", isinstance(captured.get("adjustments"), list), repr(captured.get("adjustments")))
check("3.1 响应透出 warnings", data.get("success") is True and any("duration" in w for w in (data.get("warnings") or [])), json.dumps(data, ensure_ascii=False)[:200])

# ═══ 3.2 会话级参数 ═══

from core.agents.base_agent import SESSION_PARAM_KEYS  # noqa: E402

check("3.2 SESSION_PARAM_KEYS 含 video_duration/fps/short_edge", {"video_duration", "video_fps", "video_short_edge"} <= set(SESSION_PARAM_KEYS))

import api.routers.workflow as wf_router  # noqa: E402
import inspect  # noqa: E402

src = inspect.getsource(wf_router)
check("3.2 会话 meta 更新白名单含新参数", all(k in src for k in ("video_duration", "video_fps", "video_short_edge")))

from core.agents.video_agent import VideoDirectorAgent  # noqa: E402

img = "/tmp/vp_seg.png"
with open(img, "wb") as fh:
    fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
agent = VideoDirectorAgent()
agent._next_version_path = lambda sid, seg: "/tmp/vp_seg_out.mp4"
captured2 = {}
VideoClientCls.generate_video = lambda self, **kwargs: (captured2.update(kwargs), "mock://video")[1]
agent._generate_one("s1", "seg1", "p", img, "m", 10, "on", "multi", "16:9", "720P", "first_frame",
                    None, None, "llm", video_duration=8, video_fps=24, video_short_edge=768)
check("3.2 会话时长覆盖分镜时长", captured2.get("duration") == 8, str(captured2.get("duration")))
check("3.2 会话 fps/short_edge 透传", captured2.get("fps") == 24 and captured2.get("short_edge") == 768, str({k: captured2.get(k) for k in ("fps", "short_edge")}))
captured2.clear()
agent._generate_one("s1", "seg2", "p", img, "m", 10, "on", "multi", "16:9", "720P", "first_frame",
                    None, None, "llm", None, None, None)
check("3.2 未设置时沿用分镜时长", captured2.get("duration") == 10, str(captured2.get("duration")))
check("3.2 未设置时不注入 fps/short_edge", captured2.get("fps") is None and captured2.get("short_edge") is None)

server.shutdown()
print()
print("ALL PASS" if not failures else f"FAILED: {len(failures)} 项未通过: {failures}")
sys.exit(0 if not failures else 1)
