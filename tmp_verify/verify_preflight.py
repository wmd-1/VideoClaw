"""任务 4.1-4.3 验证脚本：预检 / 条目种子化 / 失败分类（容器内运行）。

  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - \
    < tmp_verify/verify_preflight.py
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as cfg

PORT = 18097
FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── 内嵌 mock：图像接口 401（模拟鉴权失败），对话接口返回无效 JSON（doctor 判定跳过重写）──
class Handler(BaseHTTPRequestHandler):
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
        self._read()
        path = self.path.rstrip("/")
        if path in ("/v1/images/generations", "/v1/images/edits"):
            return self._json(401, {"error": {"message": "Unauthorized: invalid api key"}})
        if path == "/v1/chat/completions":
            return self._json(200, {"choices": [{"message": {"content": "not-a-json-reply"}}]})
        return self._json(404, {})

    def do_GET(self):
        return self._json(404, {})


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}/v1"

# ── 注入测试供应商与模型 ──
cfg.Config.CONFIG.setdefault("api_providers", {}).update({
    "e2e-denied": {"protocol": "openai", "base_url": BASE, "api_key": "", "enable_proxy": False},
    "e2e-broken": {"protocol": "", "base_url": "", "api_key": "", "enable_proxy": False},
})
cfg.Config.CONFIG["custom_models"] = [
    {"id": "m-e2e-img", "provider": "e2e-denied", "model": "IMG1", "types": ["t2i", "i2i"], "abilities": []},
    {"id": "m-e2e-llm", "provider": "e2e-denied", "model": "LLM1", "types": ["llm"], "abilities": []},
    {"id": "m-e2e-broken", "provider": "e2e-broken", "model": "", "types": ["t2i"], "abilities": []},
    {"id": "m-e2e-ghost", "provider": "ghost", "model": "", "types": ["t2i"], "abilities": []},
    {"id": "m-e2e-ok", "provider": "e2e-denied", "model": "OK1", "types": ["t2i"], "abilities": []},
]

from fastapi import HTTPException

from api.routers.workflow import _preflight_stage_models


def expect_preflight(name, stage, values, error_code, field=None):
    try:
        _preflight_stage_models(stage, values)
        check(name, False, "未抛出 409")
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        ok = (
            exc.status_code == 409
            and detail.get("code") == "model_unavailable"
            and detail.get("error_code") == error_code
            and (field is None or detail.get("field") == field)
        )
        check(name, ok, str(detail))


# ── 4.1 预检 ─
expect_preflight("4.1 未注册模型 409", "character_design", {"image_t2i_model": "definitely-unregistered-zzz"}, "not_registered", "image_t2i_model")
expect_preflight("4.1 供应商不完整 409", "character_design", {"image_t2i_model": "m-e2e-broken"}, "provider_incomplete")
expect_preflight("4.1 供应商缺失 409", "character_design", {"image_t2i_model": "m-e2e-ghost"}, "provider_missing")
orig_ark = cfg.Config.ARK_API_KEY
try:
    cfg.Config.ARK_API_KEY = ""
    expect_preflight("4.1 内置缺凭据 409", "character_design", {"image_t2i_model": "doubao-seedream-5-0-260128"}, "missing_credentials")
finally:
    cfg.Config.ARK_API_KEY = orig_ark
expect_preflight(
    "4.1 第4阶段检查 it2i 字段",
    "reference_generation",
    {"image_t2i_model": "m-e2e-ok", "image_it2i_model": "definitely-unregistered-zzz"},
    "not_registered",
    "image_it2i_model",
)
try:
    _preflight_stage_models("character_design", {"image_t2i_model": "m-e2e-ok"})
    check("4.1 可用模型预检通过", True)
except HTTPException as exc:
    check("4.1 可用模型预检通过", False, str(exc.detail))
try:
    _preflight_stage_models("video_generation", {"video_first_frame_model": "definitely-unregistered-zzz"})
    check("4.1 非 2/4 阶段不预检", True)
except HTTPException:
    check("4.1 非 2/4 阶段不预检", False)

# ── 4.2 第 2 阶段条目种子化 ──
from core.orchestrator import WorkflowEngine, WorkflowStage

engine = WorkflowEngine()
sid = "tc-seed-verify"
engine.create_session(sid, {"idea": "种子化验证", "expand_idea": False})
state = engine.get_or_create_state(sid)
state.artifacts["script_generation"] = {
    "characters": [
        {"character_id": "char_a", "name": "小明", "description": "少年"},
        {"character_id": "char_b", "name": "小红", "description": "少女"},
    ],
    "settings": [{"setting_id": "set_a", "name": "客厅", "description": "温馨"}],
}
state.status["character_design"] = "pending"

engine.prepare_stage_execution(sid, "character_design", {})
cd_art = engine.get_state(sid).artifacts.get("character_design") or {}
chars = cd_art.get("characters") or []
sets = cd_art.get("settings") or []
check("4.2 条目已种子化", len(chars) == 2 and len(sets) == 1, str([c.get("id") for c in chars]))
check("4.2 条目含名称与 pending 状态", chars and chars[0]["name"] == "小明" and chars[0]["status"] == "pending" and chars[0]["versions"] == [])
check("4.2 阶段状态未被重算", engine.get_state(sid).status.get("character_design") == "pending")

# 已有条目不被覆盖/不重复种子化
state2 = engine.get_or_create_state(sid)
state2.artifacts["character_design"]["characters"][0]["versions"] = ["already.png"]
state2.artifacts["character_design"]["characters"][0]["selected"] = "already.png"
engine.prepare_stage_execution(sid, "character_design", {})
chars2 = engine.get_state(sid).artifacts["character_design"]["characters"]
check("4.2 已有条目不被覆盖", chars2[0]["versions"] == ["already.png"] and len(chars2) == 2)
engine.delete_session(sid)

# ── 4.3 失败分类：角色阶段 E2E（图像 401）──
engine2 = WorkflowEngine()
sid2 = "tc-e2e-classify"
engine2.create_session(sid2, {
    "idea": "失败分类验证",
    "expand_idea": False,
    "style": "realistic",
    "video_ratio": "16:9",
    "video_resolution": "720P",
    "llm_model": "m-e2e-llm",
    "vlm_model": "m-e2e-llm",
    "image_t2i_model": "m-e2e-img",
    "image_it2i_model": "m-e2e-img",
    "video_first_frame_model": "m-e2e-img",
})
state3 = engine2.get_or_create_state(sid2)
state3.artifacts["script_generation"] = {
    "characters": [{"character_id": "char_x", "name": "小红", "description": "女孩"}],
    "settings": [],
}
_, input_data = engine2.prepare_stage_execution(sid2, "character_design", {})
# 提供 progress_callback：与真实 SSE 链路一致（安装进度合并回调，error_type 随之写入 artifact）
asyncio.run(engine2.execute_stage(
    state3,
    WorkflowStage.CHARACTER_DESIGN,
    input_data,
    progress_callback=lambda phase, step, percent, data=None: None,
))
artifact = engine2.get_state(sid2).artifacts.get("character_design") or {}
item = (artifact.get("characters") or [{}])[0]
check(
    "4.3 模型级失败条目带 error_type",
    item.get("status") == "failed" and item.get("error_type") == "model_unavailable" and bool(item.get("error")),
    str({k: item.get(k) for k in ("status", "error_type", "error")})[:160],
)

# 参考图阶段：_generate_one 直调（401 → 失败标记）
from core.agents.reference_agent import ReferenceGeneratorAgent
from models.image_client import ImageClient

agent = ReferenceGeneratorAgent()
seg = {"segment_id": "seg_01_01", "plot": "plot", "visual_prompt": "vp", "shots": [{"content": "c"}]}
result = agent._generate_one(
    ImageClient(), sid2, seg, "visual prompt", [], "realistic",
    "m-e2e-img", "m-e2e-img", "16:9", "720P", "m-e2e-llm",
    character_description="", setting_description="", llm_model="m-e2e-llm",
)
marker = result[2] if len(result) > 2 else None
check(
    "4.3 参考图失败返回模型级标记",
    isinstance(marker, dict) and marker.get("error_type") == "model_unavailable",
    str(marker)[:160],
)
engine2.delete_session(sid2)

# 4.3 反例：内容类错误不误报（复用适配层分类断言）
from models.custom_common import classify_model_unavailable

check("4.3 内容类错误不误报", classify_model_unavailable(RuntimeError("内容审核未通过：包含敏感元素")) is False)

server.shutdown()
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项未通过: {FAILURES}")
    sys.exit(1)
print("ALL PASS")