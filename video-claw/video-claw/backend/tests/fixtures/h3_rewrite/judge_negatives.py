"""真实评审负例：用同一会话上下文，评估三条低质草稿的分数分布。

仅在入口显式执行时调用模型；被 import 不会产生任何网络/模型调用。
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/app")

from config import Config  # noqa: E402
from core.agents.prompt_rewrite_agent import PromptRewriteAgent  # noqa: E402

SID = "1790047810282"
BASE = "http://127.0.0.1:8500"

NEGATIVES = {
    "abstract-and-vague": (
        "integrated_multimodal_description: [Shot 1] A beautiful cinematic scene of a man in a bank, "
        "very emotional and impressive.\noverall_soundscape: Music.\nnon_diegetic_music: Piano.\n"
    ),
    "authority-conflict": (
        "integrated_multimodal_description: [Shot 1] A young woman in a red dress stands in a bright cafe, "
        "smiling at the camera and waving.\noverall_soundscape: Cafe chatter.\nnon_diegetic_music: Upbeat pop.\n"
    ),
    "missing-key-action": (
        "integrated_multimodal_description: [Shot 1] A man stands still in a room and says a few words.\n"
        "overall_soundscape: Room tone.\nnon_diegetic_music: Low strings.\n"
    ),
}


def api(path):
    with urllib.request.urlopen(f"{BASE}/api/project/{SID}/artifact/{path}", timeout=30) as response:
        return json.load(response)["artifact"]


def main():
    storyboard = api("storyboard")
    character = api("character_design")
    segments = PromptRewriteAgent._collect_segments(storyboard)
    segment = next(seg for seg in segments if seg["id"] == "seg_01_02")

    agent = PromptRewriteAgent()
    sections = agent._render_sections(
        authoritative=agent._authoritative_context(segment, character),
        shot_content=segment["shot_content"],
        user_section="",
        continuity_summaries=[],
        grounding_desc="",
    )

    results = []
    for name, draft in NEGATIVES.items():
        quality = agent._run_judge(
            draft, "I2VA", segment["duration"], sections, segment["shot_content"],
            Config.H3_REWRITE_LLM_MODEL, 1,
        )
        results.append({
            "case": name,
            "verdict": quality.get("verdict"),
            "score": quality.get("score"),
            "threshold": quality.get("threshold"),
            "dimensions": quality.get("dimensions"),
            "issues": quality.get("issues"),
            "error": quality.get("error"),
        })

    print(json.dumps({
        "segment_id": segment["id"],
        "duration": segment["duration"],
        "model": Config.H3_REWRITE_JUDGE_MODEL or Config.H3_REWRITE_LLM_MODEL,
        "results": results,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
