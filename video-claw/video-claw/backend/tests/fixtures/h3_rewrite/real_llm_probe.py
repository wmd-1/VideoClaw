"""真实 LLM 实测：复用既有会话的分镜/角色/参考图产物，跑 2 条分镜的改写与评审。

仅在入口显式执行时调用模型；被 import 不会产生任何网络/模型调用。
依赖既有会话 `SID` 与容器内后端 API；换环境时改 SID/BASE。
"""
import asyncio
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/app")

from config import Config  # noqa: E402
from core.agents.prompt_rewrite_agent import PromptRewriteAgent  # noqa: E402

SID = "1790047810282"
BASE = "http://127.0.0.1:8500"


def api(path):
    with urllib.request.urlopen(f"{BASE}/api/project/{SID}/artifact/{path}", timeout=30) as response:
        return json.load(response)["artifact"]


async def run_one(segment, ordered, artifacts, judge_enabled):
    Config.H3_REWRITE_JUDGE_ENABLED = bool(judge_enabled)
    agent = PromptRewriteAgent()
    stats = {"rewrite": 0, "judge": 0}

    original_query = agent._query_llm

    def counting_query(*args, **kwargs):
        stats["rewrite"] += 1
        return original_query(*args, **kwargs)

    original_judge = agent._run_judge

    def counting_judge(*args, **kwargs):
        stats["judge"] += 1
        return original_judge(*args, **kwargs)

    agent._query_llm = counting_query
    agent._run_judge = counting_judge
    agent._report_progress = lambda *args, **kwargs: None
    agent._check_cancel = lambda: None

    input_data = {
        "video_generation_mode": "first_frame",
        "llm_model": Config.H3_REWRITE_LLM_MODEL or Config.LLM_MODEL,
        "session_id": SID,
        "_session_artifacts": artifacts,
        "_session_meta": {"video_generation_mode": "first_frame", "session_id": SID},
    }
    started = time.time()
    results = await agent._rewrite_segments([segment], input_data, {}, ordered_segments=ordered)
    item = results[segment["id"]]
    return {
        "judge_enabled": bool(judge_enabled),
        "id": segment["id"],
        "duration": segment["duration"],
        "input_mode": item.get("input_mode"),
        "status": item.get("status"),
        "error": item.get("error"),
        "quality": item.get("quality"),
        "rewrite_calls": stats["rewrite"],
        "judge_calls": stats["judge"],
        "elapsed_s": round(time.time() - started, 2),
        "prompt_head": (item.get("rewritten_prompt") or "")[:180],
        "rejected_head": (item.get("rejected_output") or "")[:800],
    }


async def main():
    storyboard = api("storyboard")
    artifacts = {
        "storyboard": storyboard,
        "character_design": api("character_design"),
        "reference_generation": api("reference_generation"),
    }
    previous = api("prompt_rewrite")
    segments = PromptRewriteAgent._collect_segments(storyboard)
    targets = [seg for seg in segments if seg["id"] in ("seg_01_01", "seg_01_02")]
    payload = {
        "session_id": SID,
        "llm_model": Config.H3_REWRITE_LLM_MODEL or Config.LLM_MODEL,
        "vlm_model": Config.H3_REWRITE_VLM_MODEL or getattr(Config, "VLM_MODEL", ""),
        "grounding_enabled": Config.H3_REWRITE_GROUNDING_ENABLED,
        "previous_input_modes": {i.get("id"): i.get("input_mode") for i in previous.get("items", [])},
        "previous_status": {i.get("id"): i.get("status") for i in previous.get("items", [])},
        "runs": [],
    }
    for judge_enabled in (False, True):
        for segment in targets:
            payload["runs"].append(await run_one(segment, segments, artifacts, judge_enabled))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
