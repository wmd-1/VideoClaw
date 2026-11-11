"""运行时质量评审：解析、降级、状态机预算与评分版本标注。"""
from __future__ import annotations

import asyncio
import json

from config import Config
from core.agents.prompt_rewrite_agent import (
    PromptRewriteAgent,
    _extract_json_object,
    _mark_quality_applies,
)
from core.observability import content_version


def _segment(duration: int = 5) -> dict:
    return {
        "id": "seg_01_01",
        "name": "第1集-片段1",
        "index": 1,
        "episode": 1,
        "shot_content": "小陈站在银行大堂",
        "duration": duration,
        "characters": [],
        "location": "银行大堂",
    }


def _valid_prompt(tag: str = "v1") -> str:
    return (
        f"integrated_multimodal_description: [Shot 1] {tag} a young man stands in a bank hall.\n"
        "overall_soundscape: Quiet interior room tone with distant footsteps.\n"
        "non_diegetic_music: Low strings fade in and last 5 seconds.\n"
    )


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def query(self, *args, **kwargs):
        return self.reply


def _sections():
    return PromptRewriteAgent._render_sections("", "小陈站立", "", [], "")


def _patch_state_machine(monkeypatch, agent, outputs, judge_result):
    requests = []

    def fake_query(system_prompt, guide, request_text, llm_model):
        requests.append(request_text)
        index = len(requests) - 1
        return outputs[index] if index < len(outputs) else outputs[-1]

    judge_calls = []

    def fake_judge(*args, **kwargs):
        judge_calls.append(args)
        result = dict(judge_result)
        result.setdefault("judged_prompt_hash", content_version(args[0]))
        return result

    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(agent, "_query_llm", fake_query)
    monkeypatch.setattr(agent, "_run_judge", fake_judge)
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_ENABLED", True)
    return requests, judge_calls


def test_extract_json_object_handles_fence_and_plain_text():
    assert _extract_json_object('{"score": 4}') == {"score": 4}
    assert _extract_json_object('说明\n```json\n{"score": 3}\n```') == {"score": 3}
    assert _extract_json_object("not json") is None
    assert _extract_json_object("") is None


def test_run_judge_parses_and_normalizes_issues(monkeypatch):
    agent = PromptRewriteAgent()
    agent._llm = _FakeLLM(json.dumps({
        "score": 4,
        "needs_revision": False,
        "dimensions": {"faithfulness": 4, "visual_alignment": 4, "specificity": 5, "continuity": 4},
        "issues": [{"dimension": "faithfulness", "severity": "minor", "message": "缺少手部动作"}],
    }))
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_THRESHOLD", 3)

    quality = agent._run_judge("draft", "T2VA", 5, _sections(), "小陈站立", "fake-model", 1)

    assert quality["verdict"] == "pass"
    assert quality["score"] == 4
    assert quality["issues"][0]["message"] == "缺少手部动作"
    assert quality["judged_revision"] == 1
    assert quality["judged_prompt_hash"] == content_version("draft")


def test_run_judge_low_score_or_blocker_needs_revision(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_THRESHOLD", 4)

    agent._llm = _FakeLLM(json.dumps({"score": 3, "needs_revision": False, "issues": []}))
    assert agent._run_judge("d", "T2VA", 5, _sections(), "s", "m", 1)["verdict"] == "needs_revision"

    agent._llm = _FakeLLM(json.dumps({
        "score": 5,
        "needs_revision": False,
        "issues": [{"dimension": "faithfulness", "severity": "blocker", "message": "与权威事实冲突"}],
    }))
    assert agent._run_judge("d", "T2VA", 5, _sections(), "s", "m", 1)["verdict"] == "needs_revision"


def test_run_judge_degrades_on_bad_output_or_exception(monkeypatch):
    agent = PromptRewriteAgent()
    agent._llm = _FakeLLM("无法解析")
    assert agent._run_judge("d", "T2VA", 5, _sections(), "s", "m", 1)["verdict"] == "unavailable"

    class _Boom:
        def query(self, *args, **kwargs):
            raise RuntimeError("provider down")

    agent._llm = _Boom()
    quality = agent._run_judge("d", "T2VA", 5, _sections(), "s", "m", 1)
    assert quality["verdict"] == "unavailable"
    assert "provider down" in quality["error"]


def test_run_judge_missing_score_is_unavailable(monkeypatch):
    agent = PromptRewriteAgent()
    agent._llm = _FakeLLM(json.dumps({"needs_revision": False, "issues": []}))
    quality = agent._run_judge("d", "T2VA", 5, _sections(), "s", "m", 1)
    assert quality["verdict"] == "unavailable"
    assert "score" in quality["error"]


def test_run_judge_missing_dimensions_keeps_score(monkeypatch):
    agent = PromptRewriteAgent()
    agent._llm = _FakeLLM(json.dumps({"score": 4, "needs_revision": False}))
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_THRESHOLD", 3)
    quality = agent._run_judge("d", "T2VA", 5, _sections(), "s", "m", 1)
    assert quality["verdict"] == "pass"
    assert quality["score"] == 4
    assert quality["dimensions"] == {}
    assert quality["issues"] == []


def test_judge_disabled_never_calls_judge(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(Config, "H3_REWRITE_JUDGE_ENABLED", False)
    judge_calls = []
    monkeypatch.setattr(agent, "_run_judge", lambda *a, **k: judge_calls.append(1) or {})
    monkeypatch.setattr(agent, "_query_llm", lambda *a, **k: _valid_prompt())

    rewritten, violations, _, quality = asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", _sections(),
    ))

    assert rewritten == _valid_prompt().strip()
    assert violations == []
    assert quality is None
    assert judge_calls == []


def test_judge_triggers_one_revision_with_draft(monkeypatch):
    agent = PromptRewriteAgent()
    requests, judge_calls = _patch_state_machine(
        monkeypatch, agent,
        [_valid_prompt("v1"), _valid_prompt("v2")],
        {"verdict": "needs_revision", "score": 2, "threshold": 3,
         "issues": [{"dimension": "faithfulness", "severity": "blocker", "message": "缺少关键动作"}],
         "revision_attempted": False, "model": "judge"},
    )

    rewritten, violations, _, quality = asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", _sections(),
    ))

    assert rewritten == _valid_prompt("v2").strip()
    assert violations == []
    assert len(requests) == 2
    assert len(judge_calls) == 1
    assert "被拒草稿" in requests[1]
    assert "缺少关键动作" in requests[1]
    assert quality["revision_attempted"] is True
    assert quality["applies_to_final"] is False


def test_judge_budget_exhausted_keeps_third_result(monkeypatch):
    agent = PromptRewriteAgent()
    requests, judge_calls = _patch_state_machine(
        monkeypatch, agent,
        ["bad one", "bad two", _valid_prompt("v3")],
        {"verdict": "needs_revision", "score": 2, "threshold": 3,
         "issues": [{"dimension": "specificity", "severity": "minor", "message": "过于抽象"}],
         "revision_attempted": False, "model": "judge"},
    )

    rewritten, violations, _, quality = asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", _sections(),
    ))

    assert rewritten == _valid_prompt("v3").strip()
    assert violations == []
    assert len(requests) == 3
    assert len(judge_calls) == 1
    assert quality["verdict"] == "needs_revision"
    assert quality["applies_to_final"] is True


def test_judge_revision_fails_validation_returns_judged_version(monkeypatch):
    agent = PromptRewriteAgent()
    requests, judge_calls = _patch_state_machine(
        monkeypatch, agent,
        [_valid_prompt("v1"), "bad revision", "bad revision again"],
        {"verdict": "needs_revision", "score": 2, "threshold": 3,
         "issues": [{"dimension": "specificity", "severity": "blocker", "message": "需要更具体"}],
         "revision_attempted": False, "model": "judge"},
    )

    rewritten, violations, _, quality = asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", _sections(),
    ))

    assert rewritten == _valid_prompt("v1").strip()
    assert violations == []
    assert len(requests) == 3
    assert len(judge_calls) == 1
    assert quality["verdict"] == "needs_revision"
    assert quality["revision_attempted"] is True
    assert quality["applies_to_final"] is True


def test_mark_quality_applies_compares_final_hash():
    quality = {"judged_prompt_hash": content_version("v1")}
    _mark_quality_applies(quality, "v1")
    assert quality["applies_to_final"] is True
    _mark_quality_applies(quality, "v2")
    assert quality["applies_to_final"] is False
