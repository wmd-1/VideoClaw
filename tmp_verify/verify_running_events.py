# prompt-rewrite-item-running-events 后端验收（零花费：in-process stub LLM + 真实引擎/合并/落盘路径）
# 覆盖 tasks 1.1（每条恰好 running→终态、running 不含正文）、1.2（running 不触发写盘）、
# 1.3（重试不产生中间事件抖动）。运行（既有 Docker 后端容器内）：
#   docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_running_events.py
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

import config
from core.orchestrator import WorkflowEngine, WorkflowState, WorkflowStage
import models.llm_client as llm_client

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def valid_body(repeat: int = 3) -> str:
    detail = ("A tabby cat crosses a rain-soaked lane at night, slow tracking shot at eye level, "
              "sodium-vapour lamps reflecting on wet asphalt, 50mm shallow depth of field. ")
    return ("```text\nintegrated_multimodal_description: 8 seconds total. " + detail * repeat
            + "\noverall_soundscape: wet footsteps, distant tram bell, low ambient hum.\n"
            "non_diegetic_music: none.\n```")


INVALID = "```\ncinematic cat.\n```"  # 抽象词 + 缺时长 → 校验必失败
MODE = {"seq": None}
CALLS = {"n": 0}


def stub_query(self, prompt, image_urls=[], model="", safe_content=True, task_id=None, web_search=False, **kw):
    CALLS["n"] += 1
    if MODE["seq"]:
        return MODE["seq"][min(CALLS["n"] - 1, len(MODE["seq"]) - 1)]
    return valid_body()


llm_client.LLM.query = stub_query
config.Config.H3_REWRITE_ENABLED = True
config.Config.H3_REWRITE_GROUNDING_ENABLED = False

SEG = lambda i: {"segment_id": f"seg_01_0{i}", "shots": [{"content": f"镜头{i}", "duration": 8}],
                 "total_duration": 8, "characters": ["cat"], "setting": "lane"}


def run_stage(segments, intervention=None):
    engine = WorkflowEngine()
    state = WorkflowState("zz-accept-running")
    state.artifacts = {"storyboard": {"episodes": [{"episode_number": 1, "segments": segments}]}}
    state.status["storyboard"] = "completed"
    state.meta = {"video_generation_mode": "first_frame", "llm_model": "stub", "vlm_model": "stub"}

    events, persist_status = [], []
    last = {"status": None}

    def _save(sid, meta=None):
        persist_status.append(last["status"])

    engine.save_session_to_disk = _save

    def on_progress(phase, step, percent, data=None):
        payload = (data or {}).get("asset_complete")
        if isinstance(payload, dict):
            events.append(payload)
            last["status"] = payload.get("status")

    asyncio.run(engine.execute_stage(
        state, WorkflowStage.PROMPT_REWRITE,
        {"session_id": "zz-accept-running", "_session_artifacts": state.artifacts, "_session_meta": state.meta},
        intervention=intervention, progress_callback=on_progress,
    ))
    return events, persist_status, state


print("=" * 78)
print("【A】3 条全成功：每条恰好 running→终态、running 不含正文（1.1）")
CALLS["n"] = 0
MODE["seq"] = None
events, persist_status, _ = run_stage([SEG(i) for i in (1, 2, 3)])
seq = [(e["id"], e["status"]) for e in events]
check("A.1 事件序列 = 每条 running 后紧跟终态（共 6 条，交替）",
      seq == [("seg_01_01", "running"), ("seg_01_01", "done"),
              ("seg_01_02", "running"), ("seg_01_02", "done"),
              ("seg_01_03", "running"), ("seg_01_03", "done")], str(seq))
runs = [e for e in events if e["status"] == "running"]
check("A.2 running 事件不含 rewritten_prompt / truncated",
      all(("rewritten_prompt" not in e) and ("truncated" not in e) for e in runs),
      str([sorted(e.keys()) for e in runs][:1]))
check("A.3 running 事件仍带身份/渲染字段（name/id/type）",
      all(e.get("id") and e.get("type") == "items" and "name" in e for e in runs))
check("A.4 终态 done 事件带非空正文",
      all(e.get("rewritten_prompt") for e in events if e["status"] == "done"))
check("A.5 running 不触发写盘（写盘时最近事件均非 running）",
      "running" not in persist_status, f"persist_statuses={persist_status}")

print("\n【B】单条后台重生成：仍只 running→终态，且阶段整体不被置 running（2.3/1.1）")
CALLS["n"] = 0
MODE["seq"] = None
segs = [SEG(i) for i in (1, 2)]
ev0, _, state = run_stage(segs)  # 先整体跑一遍，得到 done 条目
CALLS["n"] = 0
events, persist_status, state = run_stage(segs, intervention={"regenerate_items": ["seg_01_01"]})
seq = [(e["id"], e["status"]) for e in events]
check("B.1 后台重生成仅对目标条发 running→done（不影响其他条）",
      seq == [("seg_01_01", "running"), ("seg_01_01", "done")], str(seq))
check("B.2 running 不触发写盘", "running" not in persist_status, str(persist_status))

print("\n【C】重试不抖动：首次校验失败、二次成功 → 事件仍 running,done，LLM 调用 2 次（1.3）")
CALLS["n"] = 0
MODE["seq"] = [INVALID, valid_body()]
events, persist_status, _ = run_stage([SEG(1)])
seq = [e["status"] for e in events]
check("C.1 事件序列恰为 [running, done]（重试不产生中间 running/失败闪烁）",
      seq == ["running", "done"], str(seq))
check("C.2 LLM 实际调用 2 次（1 次失败 + 1 次成功）", CALLS["n"] == 2, f"calls={CALLS['n']}")
check("C.3 终态为 done 且带正文",
      events[-1]["status"] == "done" and bool(events[-1].get("rewritten_prompt")))
MODE["seq"] = None

print("=" * 78)
print(f"LLM 调用全部走 stub（零花费）；写盘次数见各段 persist_statuses")
if failures:
    print(f"FAILED {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
