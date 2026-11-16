"""补跑一轮、事件不抖动与末轮择优。

覆盖 openspec change `stage-stop-and-rewrite-resilience` 的 h3-prompt-rewriter 增量需求。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.prompt_source import SOURCE_H3, resolve_effective_prompt

CUT_VIOLATION = "后续镜头的 cut time 必须严格递增"
FATAL_VIOLATION = "缺少必填字段：overall_soundscape"


def _segment(seg_id: str = "seg_01_01") -> dict:
    return {
        "id": seg_id,
        "name": "第1集-片段1",
        "index": 1,
        "episode": 1,
        "shot_content": "小陈站在银行大堂",
        "duration": 8,
        "characters": [],
        "location": "银行大堂",
        "shots": [{"index": 1, "duration": 4}, {"index": 2, "duration": 4}],
    }


def _agent(monkeypatch, script) -> tuple[PromptRewriteAgent, List[Dict[str, Any]]]:
    agent = PromptRewriteAgent()
    events: List[Dict[str, Any]] = []
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(agent, "_session_meta", lambda input_data: {})
    monkeypatch.setattr(agent, "_session_artifacts", lambda input_data: {})
    monkeypatch.setattr(agent, "_load_guide", lambda mode: "")
    monkeypatch.setattr(agent, "_grounding_describe", lambda *a, **k: ("", []))

    def report(phase, step, percent, data=None):
        asset = (data or {}).get("asset_complete") or {}
        if asset:
            events.append({
                "status": asset.get("status"),
                "id": asset.get("id"),
                "percent": percent,
                "best_effort": asset.get("best_effort"),
                "validation_violations": asset.get("validation_violations"),
            })

    monkeypatch.setattr(agent, "_report_progress", report)
    if script is not None:
        monkeypatch.setattr(agent, "_rewrite_one", script)
    return agent, events


def _run(agent: PromptRewriteAgent, segments: List[dict]) -> Dict[str, Dict[str, Any]]:
    return asyncio.run(agent._rewrite_segments(segments, {"video_generation_mode": "first_frame"}, {}))


def test_repair_round_runs_only_for_validation_failures(monkeypatch):
    calls: List[str] = []

    async def script(segment, mode, system_prompt, guide, llm_model, sections, picture_count=None):
        calls.append(sections.get("revision_section") or "")
        # 单测层面 _rewrite_one 被整体替换：每次调用代表一轮，内部 1+2 重试由其它用例覆盖
        if len(calls) == 1:
            return "", [CUT_VIOLATION], "integrated_multimodal_description: partial draft", None
        return "integrated_multimodal_description: [Shot 1] ok for 8 seconds", [], "", None

    agent, events = _agent(monkeypatch, script)
    result = _run(agent, [_segment()])
    item = result["seg_01_01"]
    assert item["status"] == "done"
    assert item.get("best_effort") is None
    assert len(calls) == 2, "首轮失败一次 + 补跑轮一次成功"
    assert calls[0] == ""
    assert "【上一次输出未通过校验" in calls[1], "补跑轮首轮即携带被拒草稿"

    statuses = [event["status"] for event in events]
    assert statuses.count("running") == 1, "整轮只允许一次改写中"
    assert statuses.count("failed") == 1
    assert statuses.count("done") == 1


def test_passed_items_are_not_called_again(monkeypatch):
    calls: List[str] = []

    async def script(segment, mode, system_prompt, guide, llm_model, sections, picture_count=None):
        calls.append(segment["id"])
        if segment["id"] == "seg_01_02" and calls.count("seg_01_02") == 1:
            return "", [CUT_VIOLATION], "partial", None
        return "integrated_multimodal_description: ok for 8 seconds", [], "", None

    agent, _events = _agent(monkeypatch, script)
    result = _run(agent, [_segment("seg_01_01"), _segment("seg_01_02")])
    assert result["seg_01_01"]["status"] == "done"
    assert result["seg_01_02"]["status"] == "done"
    assert calls.count("seg_01_01") == 1, "已通过条目不得补跑"
    assert calls.count("seg_01_02") == 2


def test_exception_failures_are_not_repaired(monkeypatch):
    calls: List[int] = []

    async def script(segment, mode, system_prompt, guide, llm_model, sections, picture_count=None):
        calls.append(1)
        raise RuntimeError("model down")

    agent, _events = _agent(monkeypatch, script)
    result = _run(agent, [_segment()])
    item = result["seg_01_01"]
    assert item["status"] == "failed"
    assert len(calls) == 1, "调用异常不触发补跑"
    assert item.get("best_effort") is None


def test_best_effort_candidate_is_adopted_when_structure_usable(monkeypatch):
    async def script(segment, mode, system_prompt, guide, llm_model, sections, picture_count=None):
        return "", [CUT_VIOLATION], "integrated_multimodal_description: usable but unordered", None

    agent, events = _agent(monkeypatch, script)
    result = _run(agent, [_segment()])
    item = result["seg_01_01"]
    assert item["status"] == "done"
    assert item["best_effort"] is True
    assert item["rewritten_prompt"] == "integrated_multimodal_description: usable but unordered"
    assert item["validation_violations"] == [CUT_VIOLATION]
    assert item["versions"], "采用最优候选同样追加版本"
    # 补跑仍失败时不重复发失败终态：整轮只出现一次 running 与一次 failed；随后补发一次 done
    statuses = [event["status"] for event in events]
    assert statuses.count("running") == 1
    assert statuses.count("failed") == 1
    assert statuses.count("done") == 1
    done_event = [event for event in events if event["status"] == "done"][-1]
    assert done_event["best_effort"] is True, "完成事件需携带最优候选标识（否则执行期看不到徽标）"
    assert done_event["validation_violations"] == [CUT_VIOLATION]


def test_best_effort_is_rejected_when_candidate_is_not_structural(monkeypatch):
    async def script(segment, mode, system_prompt, guide, llm_model, sections, picture_count=None):
        return "", [FATAL_VIOLATION], "partial text without required fields", None

    agent, _events = _agent(monkeypatch, script)
    result = _run(agent, [_segment()])
    item = result["seg_01_01"]
    assert item["status"] == "failed"
    assert item.get("best_effort") is None
    assert item["validation_violations"] == [FATAL_VIOLATION]


def test_best_effort_item_feeds_video_prompt_resolution():
    item = {
        "id": "seg_01_01",
        "status": "done",
        "best_effort": True,
        "rewritten_prompt": "integrated_multimodal_description: best effort body",
        "input_fingerprint": "fp-1",
        "input_dimensions": {},
    }
    resolved = resolve_effective_prompt(
        {"shot_content": "分镜描述"}, item, {},
        current_fingerprint="fp-1",
    )
    assert resolved["source"] == SOURCE_H3
    assert resolved["text"] == "integrated_multimodal_description: best effort body"


def test_judge_runs_at_most_once_across_rounds(monkeypatch):
    """真实 _rewrite_one：首轮三次都不过校验（无评审）→ 补跑轮通过 → 评审恰好 1 次。"""
    from config import Config

    judge_calls: List[int] = []
    bad = "integrated_multimodal_description: [Shot 1] bad for 8 seconds"
    good = (
        "integrated_multimodal_description: [Shot 1] ok for 8 seconds.\n"
        "overall_soundscape: Quiet room tone.\n"
        "non_diegetic_music: N/A\n"
    )
    drafts = [bad, bad, bad, good]

    def judge(*args, **kwargs):
        judge_calls.append(1)
        return {"verdict": "pass", "score": 4, "issues": [], "threshold": 3, "model": "m"}

    agent, _events = _agent(monkeypatch, None)
    monkeypatch.setattr(agent, "_query_llm", lambda *a, **k: drafts.pop(0))
    monkeypatch.setattr(agent, "_run_judge", judge)
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_ENABLED", True)
    monkeypatch.setattr(Config, "H3_REWRITE_LLM_MODEL", "fake-model")

    result = _run(agent, [_segment()])
    assert result["seg_01_01"]["status"] == "done"
    assert len(judge_calls) == 1, "跨补跑轮评审至多 1 次/条目"


def test_cross_round_best_keeps_fewer_violations(monkeypatch):
    """首轮候选更优时，不得被补跑轮的更差候选覆盖（spec：违规最少优先）。"""
    calls: List[int] = []
    better = ["违规 A", "违规 B"]
    worse = ["违规 A", "违规 B", "违规 C"]

    async def script(segment, mode, system_prompt, guide, llm_model, sections, picture_count=None):
        calls.append(1)
        if len(calls) == 1:
            return "", better, "round1-better-draft", None
        return "", worse, "round2-worse-draft", None

    agent, _events = _agent(monkeypatch, script)
    result = _run(agent, [_segment()])
    item = result["seg_01_01"]
    assert item["best_effort"] is True
    assert item["rewritten_prompt"] == "round1-better-draft"
    assert item["validation_violations"] == better
    assert item.get("continuity") is not None
    assert "error" not in item, "已完成条目不得残留失败文案"
