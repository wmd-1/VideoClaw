# prompt-rewrite-incremental-items 验收：事件增量字段、shell 不落正文、幂等、versions/selected、失败语义
# 真实引擎 + 真实合并路径 + FakeLLM（零花费）。运行：
#   docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_incremental_items_accept.py
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

import config
from core.orchestrator import WorkflowEngine, WorkflowState, WorkflowStage
from core.agents.prompt_rewrite_agent import EVENT_BODY_LIMIT
import models.llm_client as llm_client

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


def h3_body(repeat: int) -> str:
    detail = ("A tabby cat crosses a rain-soaked lane at night, slow tracking shot at eye level, "
              "sodium-vapour lamps reflecting on wet asphalt, 50mm shallow depth of field. ")
    return ("```text\nintegrated_multimodal_description: 8 seconds total. " + detail * repeat
            + "\noverall_soundscape: wet footsteps, distant tram bell, low ambient hum.\n"
              "non_diegetic_music: none.\n```")


# 第 3 条用超长输出触发截断（repeat 足够大 → 正文 > EVENT_BODY_LIMIT）
STUB = [h3_body(3), h3_body(6), h3_body(320), h3_body(3), h3_body(3)]
CALLS = {"n": 0}


def stub_query(self, prompt, image_urls=[], model="", safe_content=True, task_id=None, web_search=False, **kw):
    CALLS["n"] += 1
    return STUB[min(CALLS["n"] - 1, len(STUB) - 1)]


llm_client.LLM.query = stub_query
config.Config.H3_REWRITE_ENABLED = True
config.Config.H3_REWRITE_GROUNDING_ENABLED = False

SEG = lambda i: {"segment_id": f"seg_01_0{i}", "shots": [{"content": f"镜头{i}", "duration": 8}],
                 "total_duration": 8, "characters": ["cat"], "setting": "lane"}
segments = [SEG(i) for i in (1, 2, 3)]

engine = WorkflowEngine()
state = WorkflowState("zz-accept-incremental")
state.artifacts = {"storyboard": {"episodes": [{"episode_number": 1, "segments": segments}]}}
state.status["storyboard"] = "completed"
state.meta = {"video_generation_mode": "first_frame", "llm_model": "stub", "vlm_model": "stub"}

EVENTS, SHELL_SNAPSHOTS, PERSIST = [], [], []
engine.save_session_to_disk = lambda sid, meta=None: PERSIST.append(
    len(json.dumps(state.to_dict(), ensure_ascii=False, default=str)))

REQUIRED = {"name", "index", "episode", "original_prompt", "input_mode", "duration",
            "continuity", "rewritten_prompt", "truncated"}


def on_progress(phase, step, percent, data=None):
    payload = (data or {}).get("asset_complete")
    if not isinstance(payload, dict):
        return
    arts = (state.artifacts.get("prompt_rewrite") or {}).get("items") or []
    SHELL_SNAPSHOTS.append({"keys": sorted(payload.keys()),
                            "shell_keys": sorted(arts[0].keys()) if arts else [],
                            "shell_has_body": bool(arts and arts[0].get("rewritten_prompt")),
                            "order": [a.get("id") for a in arts]})
    EVENTS.append(payload)


asyncio.run(engine.execute_stage(
    state, WorkflowStage.PROMPT_REWRITE,
    {"session_id": "zz-accept-incremental", "_session_artifacts": state.artifacts,
     "_session_meta": state.meta},
    progress_callback=on_progress,
))
items = (state.artifacts.get("prompt_rewrite") or {}).get("items") or []

print("=" * 78)
print("【1】事件字段扩展与顺序")
check("1.1 每条完成都发事件且顺序与分镜一致", [e["id"] for e in EVENTS] == [f"seg_01_0{i}" for i in (1, 2, 3)],
      str([e["id"] for e in EVENTS]))
check("1.2 事件包含全部必需正文字段", all(REQUIRED <= set(e.keys()) for e in EVENTS),
      "缺: " + str(sorted(REQUIRED - set(EVENTS[0].keys()))) if not all(REQUIRED <= set(e.keys()) for e in EVENTS) else "")
check("1.3 既有键未被动（type/id/status/error 仍在）",
      all({"type", "id", "status", "error"} <= set(e.keys()) for e in EVENTS))
check("1.4 done 事件带非空正文", all(e.get("rewritten_prompt") for e in EVENTS if e["status"] == "done"))
check("1.5 input_mode 与 duration 可读", EVENTS[0].get("input_mode") == "T2VA" and EVENTS[0].get("duration") == 8,
      f"{EVENTS[0].get('input_mode')} / {EVENTS[0].get('duration')}")

print("\n【2】截断标记与上限")
long_e = next((e for e in EVENTS if e.get("truncated")), None)
check("2.1 超长正文被标记 truncated 且长度=上限",
      long_e is not None and len(long_e["rewritten_prompt"]) == EVENT_BODY_LIMIT,
      f"limit={EVENT_BODY_LIMIT} 实长={len(long_e['rewritten_prompt']) if long_e else None}")
short_e = next((e for e in EVENTS if not e.get("truncated")), None)
check("2.2 未超限条目 truncated=False 且正文完整", short_e is not None and short_e["truncated"] is False)
check("2.3 截断前缀是最终完整正文的前缀（不跳变）",
      long_e is not None and any(
          (i.get("rewritten_prompt") or "").startswith(long_e["rewritten_prompt"][:80])
          for i in items if i["id"] == long_e["id"]),
      f"id={long_e['id'] if long_e else None}")

print("\n【3】shell 不落正文（U1=B）与写盘体积")
mid = next((s for s in SHELL_SNAPSHOTS if s["shell_keys"]), {"shell_keys": [], "shell_has_body": None})
check("3.1 执行中途 shell 字段集不含 rewritten_prompt", not mid["shell_has_body"], str(mid["shell_keys"]))
check("3.2 完成后条目含正文", bool(items) and bool(items[0].get("rewritten_prompt")))
final_json = len(json.dumps(state.to_dict(), ensure_ascii=False, default=str))
body_bytes = sum(len(json.dumps(i.get("rewritten_prompt") or "", ensure_ascii=False)) for i in items)
check("3.3 中间写盘体积 < 完成时体积（正文只在完成时进入产物）",
      (min(PERSIST) if PERSIST else 0) < final_json,
      f"中间各次={PERSIST} 最终={final_json} 正文字节合计={body_bytes}")

print("\n【4】重复投递幂等 + versions/selected 保留 + 失败语义")
before_ids = [i["id"] for i in items]
before_ver = {i["id"]: len(i.get("versions") or []) for i in items}
next(i for i in items if i["id"] == "seg_01_02")["selected"] = "/app/code/x/seg_01_02_v1.txt"
for _ in range(2):
    asyncio.run(engine.execute_stage(
        state, WorkflowStage.PROMPT_REWRITE,
        {"session_id": "zz-accept-incremental", "_session_artifacts": state.artifacts,
         "_session_meta": state.meta},
        intervention={"regenerate_items": ["seg_01_01"]},
        progress_callback=lambda *a, **k: None,
    ))
after = {i["id"]: i for i in (state.artifacts.get("prompt_rewrite") or {}).get("items") or []}
check("4.1 连续重生成不新增条目、id 集合不变", sorted(after) == sorted(before_ids), str(list(after)))
vers = [json.dumps(v, sort_keys=True, ensure_ascii=False) for v in (after["seg_01_01"].get("versions") or [])]
check("4.2 versions 累积且无重复条目（并集语义）",
      len(vers) > before_ver["seg_01_01"] and len(vers) == len(set(vers)),
      f"前 {before_ver['seg_01_01']} → 后 {len(vers)}，去重后 {len(set(vers))}")
check("4.3 selected 保留", after["seg_01_02"]["selected"] == "/app/code/x/seg_01_02_v1.txt",
      repr(after["seg_01_02"].get("selected")))

# 失败语义两种情形：无 selected → failed；有 selected → 既有归一为 done（U3，本 change 不改）
STUB[:] = ["```\nintegrated_multimodal_description: cat walks.\n```"]
asyncio.run(engine.execute_stage(
    state, WorkflowStage.PROMPT_REWRITE,
    {"session_id": "zz-accept-incremental", "_session_artifacts": state.artifacts,
     "_session_meta": state.meta},
    intervention={"regenerate_items": ["seg_01_03"]},
    progress_callback=lambda *a, **k: None,
))
after = {i["id"]: i for i in (state.artifacts.get("prompt_rewrite") or {}).get("items") or []}
check("4.4 无 selected 的条目失败 → 保持 failed 且带原因",
      after["seg_01_03"]["status"] == "failed" and bool(after["seg_01_03"].get("error")),
      after["seg_01_03"].get("status", "") + " / " + str(after["seg_01_03"].get("error", ""))[:40])
asyncio.run(engine.execute_stage(
    state, WorkflowStage.PROMPT_REWRITE,
    {"session_id": "zz-accept-incremental", "_session_artifacts": state.artifacts,
     "_session_meta": state.meta},
    intervention={"regenerate_items": ["seg_01_02"]},
    progress_callback=lambda *a, **k: None,
))
after = {i["id"]: i for i in (state.artifacts.get("prompt_rewrite") or {}).get("items") or []}
check("4.5 有 selected 的条目失败 → 仍按既有语义归一为 done（本 change 未改此规则，U3 另案）",
      after["seg_01_02"]["status"] == "done" and bool(after["seg_01_02"].get("error")),
      after["seg_01_02"]["status"] + " / selected=" + str(bool(after["seg_01_02"].get("selected"))))

print("=" * 78)
print(f"LLM 调用次数 = {CALLS['n']}（全部走 stub，零花费）；写盘次数 = {len(PERSIST)}")
if failures:
    print(f"FAILED {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
