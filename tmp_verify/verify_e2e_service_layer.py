# 任务 8.1（服务层真实端到端）：启用 h3_rewrite → 注入前序产物 → execute/prompt_rewrite（真实 LLM）
# → 停点确认 → continue → video_generation，并断言两级模式契约（resolver ↔ _task_for_input）
# 运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_e2e_service_layer.py
import asyncio
import copy
import os
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


import config
config.Config.H3_REWRITE_ENABLED = True
config.Config.H3_REWRITE_GROUNDING_ENABLED = False  # 本环境 VLM 无 key；grounding 关闭（独立开关语义）

from core.orchestrator import WorkflowEngine, WorkflowStage
from models.custom_video import CustomVideoClient

engine = WorkflowEngine()
created = engine.create_session("e2e-h3", {"idea": "e2e test", "video_generation_mode": "first_frame"})
state = engine.get_state("e2e-h3")

IMG = "/tmp/h3g/e2e.png"
os.makedirs("/tmp/h3g", exist_ok=True)
with open(IMG, "wb") as f:
    f.write(b"\x89PNG-e2e-ref")

# 注入前序产物（等效参考图/角色/分镜阶段已完成并确认）
state.artifacts["character_design"] = {"characters": [{"name": "林夏", "description": "短发女孩，穿黑色风衣，左眉有疤"}]}
state.artifacts["storyboard"] = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "total_duration": 8, "characters": ["林夏"],
     "shots": [{"content": "林夏在雨夜霓虹小巷中缓慢前行，雨水打湿风衣", "duration": 8}]},
    {"segment_id": "seg_01_02", "total_duration": 8, "characters": ["林夏"],
     "shots": [{"content": "林夏在巷口回望，霓虹灯闪烁", "duration": 8}]},
]}]}
state.artifacts["reference_generation"] = {"scenes": [
    {"id": "seg_01_01", "selected": IMG, "status": "done"},
    {"id": "seg_01_02", "selected": IMG, "status": "done"},
]}
for sid in ("script_generation", "character_design", "storyboard", "reference_generation"):
    state.status[sid] = "completed"
state.current_stage = WorkflowStage.PROMPT_REWRITE

# ── 1. 执行改写阶段（真实 LLM）──
result = asyncio.run(engine.execute_stage(
    state, WorkflowStage.PROMPT_REWRITE,
    {"session_id": "e2e-h3", "video_generation_mode": "first_frame"},
))
items = (state.artifacts.get("prompt_rewrite") or {}).get("items", [])
check("1a 执行返回停点", result.get("requires_intervention") is True, str(result)[:100])
check("1b 两分镜全部 done", len(items) == 2 and all(i["status"] == "done" for i in items),
      str([(i['id'], i['status'], i.get('error')) for i in items]))
check("1c 模式 I2VA（首帧选中）", all(i["input_mode"] == "I2VA" for i in items))
check("1d 产物含 continuity/grounding 字段", all("continuity" in i and "grounding" in i for i in items))
check("1e 引号外英文正文（抽查）", all(
    "Lin" in (i["rewritten_prompt"] or "") or "girl" in (i["rewritten_prompt"] or "").lower()
    or "walk" in (i["rewritten_prompt"] or "").lower() for i in items))
print("\n--- seg_01_01 改写预览（前 260 字）---")
print((items[0].get("rewritten_prompt") or "")[:260])

# ── 2. 停点确认 → continue 推进 ──
state.status["prompt_rewrite"] = "waiting"
cont = asyncio.run(engine.continue_workflow("e2e-h3"))
check("2a continue 推进至 video_generation（启用态正常链）",
      cont.get("status") == "ready" and cont.get("next_stage") == "video_generation", str(cont)[:120])

# ── 3. 两级模式契约：改写层 I2VA ↔ 传输层 _task_for_input ──
transport = CustomVideoClient._task_for_input(None, IMG, None, [])
check("3a 传输层 task=fl2va（I2VA 归并）", transport == "fl2va", transport)
check("3b 契约归类一致（I2VA→fl2va）", {"I2VA": "fl2va"}[items[0]["input_mode"]] == transport)

# ── 4. 视频阶段消费改写结果（VideoDirectorAgent 选择逻辑已单测；此处断言产物可被消费） ──
from core.agents.video_agent import VideoDirectorAgent
director = VideoDirectorAgent()
prompt = director._lookup_rewritten_prompt(state.artifacts.get("prompt_rewrite"), "seg_01_01")
check("4 视频阶段可取到改写提示词", prompt == items[0]["rewritten_prompt"] and len(prompt) > 50)

# 清理会话
try:
    engine.delete_session("e2e-h3")
except Exception:  # noqa: BLE001
    pass
config.Config.H3_REWRITE_ENABLED = False

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("SERVICE-LAYER E2E PASSED")
