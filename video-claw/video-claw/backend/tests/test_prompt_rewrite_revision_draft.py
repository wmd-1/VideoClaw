"""校验失败重试：携带被拒草稿、字段分节截断与收敛保护。"""
from __future__ import annotations

import asyncio

from core.agents.prompt_rewrite_agent import PromptRewriteAgent, _truncate_draft_by_field


def _segment() -> dict:
    return {
        "id": "seg_01_01",
        "name": "第1集-片段1",
        "index": 1,
        "episode": 1,
        "shot_content": "小陈站在银行大堂",
        "duration": 5,
        "characters": [],
        "location": "银行大堂",
    }


def _valid_prompt() -> str:
    return (
        "integrated_multimodal_description: [Shot 1] A young man stands in a bank hall.\n"
        "overall_soundscape: Quiet interior room tone with distant footsteps.\n"
        "non_diegetic_music: Low strings fade in and last 5 seconds.\n"
    )


def _record(monkeypatch, agent, outputs):
    requests = []

    def fake_query(system_prompt, guide, request_text, llm_model):
        requests.append(request_text)
        index = len(requests) - 1
        return outputs[index] if index < len(outputs) else outputs[-1]

    monkeypatch.setattr(agent, "_query_llm", fake_query)
    return requests


def test_retry_request_carries_draft_and_violations(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    requests = _record(monkeypatch, agent, ["not a valid prompt", _valid_prompt()])

    sections = PromptRewriteAgent._render_sections("", "小陈站立", "", [], "")
    rewritten, violations, rejected, quality = asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", sections,
    ))

    assert rewritten == _valid_prompt().strip()
    assert violations == []
    assert quality is None
    assert "被拒草稿" not in requests[0]
    assert "被拒草稿" in requests[1]
    assert "缺少必填字段" in requests[1]
    assert "not a valid prompt" in requests[1]


def test_oversized_draft_is_truncated_by_field_and_keeps_tail_fields(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    long_field = "x" * 4000
    oversized = (
        f"integrated_multimodal_description: {long_field}\n"
        f"overall_soundscape: {long_field}\n"
        f"non_diegetic_music: {long_field}\n"
    )
    requests = _record(monkeypatch, agent, [oversized, _valid_prompt()])

    sections = PromptRewriteAgent._render_sections("", "小陈站立", "", [], "")
    asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", sections,
    ))

    retry_request = requests[1]
    assert "non_diegetic_music" in retry_request
    assert "已截断" in retry_request
    assert len(retry_request) < len(oversized) + 2000


def test_same_violations_twice_falls_back_to_full_rewrite(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    requests = _record(monkeypatch, agent, [
        "not a valid prompt",
        "not a valid prompt",
        _valid_prompt(),
    ])

    sections = PromptRewriteAgent._render_sections("", "小陈站立", "", [], "")
    rewritten, violations, _, _ = asyncio.run(agent._rewrite_one(
        _segment(), "T2VA", "system", "guide", "fake-model", sections,
    ))

    assert rewritten == _valid_prompt().strip()
    assert violations == []
    assert "被拒草稿" in requests[1]
    assert "被拒草稿" not in requests[2]


def test_truncate_by_field_keeps_every_declared_field():
    text = (
        "For the target video, at 0.00 seconds into the target video, <Picture 1> is fully referenced.\n\n"
        + "integrated_multimodal_description: " + "a" * 5000 + "\n"
        + "overall_soundscape: " + "b" * 5000 + "\n"
        + "non_diegetic_music: " + "c" * 5000 + "\n"
    )
    truncated = _truncate_draft_by_field(text, "I2VA")
    assert len(truncated) < len(text)
    assert "integrated_multimodal_description" in truncated
    assert "overall_soundscape" in truncated
    assert "non_diegetic_music" in truncated
    assert "已截断" in truncated
