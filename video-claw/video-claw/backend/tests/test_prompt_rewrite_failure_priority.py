"""C3 本机可完成项：解析失败不得当成功、事实/修订优先级。"""
from __future__ import annotations

import asyncio

from core.agents.prompt_rewrite_agent import PromptRewriteAgent


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


def test_rewrite_one_parse_failure_returns_empty_not_fallback(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(agent, "_query_llm", lambda *a, **k: "这不是合法提示词，缺少所有字段")

    sections = PromptRewriteAgent._render_sections("", "小陈站立", "", [], "")
    rewritten, violations, rejected = asyncio.run(agent._rewrite_one(
        _segment(), "I2VA", "system", "guide", "fake-model", sections,
    ))
    assert rewritten == ""
    assert violations
    assert rejected
    assert "这不是合法提示词" in rejected


def test_rewrite_segment_failure_marks_item_failed_without_version(monkeypatch):
    agent = PromptRewriteAgent()
    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_session_meta", lambda input_data: {})
    monkeypatch.setattr(agent, "_session_artifacts", lambda input_data: {})
    monkeypatch.setattr(agent, "_load_guide", lambda mode: "")
    monkeypatch.setattr(agent, "_grounding_describe", lambda *a, **k: ("", []))

    async def failed_rewrite(segment, mode, system_prompt, guide, llm_model, sections):
        return "", ["缺少必填字段：镜头运动"], "<bad output>"

    monkeypatch.setattr(agent, "_rewrite_one", failed_rewrite)
    result = asyncio.run(agent._rewrite_segments(
        [_segment()], {"video_generation_mode": "first_frame"}, {},
    ))
    item = result["seg_01_01"]
    assert item["status"] == "failed"
    assert item["rewritten_prompt"] == ""
    assert item["rejected_output"] == "<bad output>"
    assert item["versions"] == []
    assert "结构校验未通过" in item["error"]


def test_fact_grounding_user_revision_order_and_conflict_rule():
    agent = PromptRewriteAgent()
    sections = PromptRewriteAgent._render_sections(
        authoritative="【权威事实】\n银行大堂白天营业，小陈穿制服",
        shot_content="小陈站在大堂中央",
        user_section="【用户修改】\n把时间改为夜晚",
        continuity_summaries=["上一镜：小陈推门进入"],
        grounding_desc="首帧：小陈站在白天营业的银行大堂",
    )
    request = agent._build_request_text(_segment(), "FL2VA", sections)

    order = [
        request.index("银行大堂白天营业"),
        request.index("小陈站在大堂中央"),
        request.index("首帧：小陈站在白天营业"),
        request.index("把时间改为夜晚"),
        request.index("上一镜：小陈推门进入"),
    ]
    assert order == sorted(order), "事实→当前分镜→参考图→用户修改→连续性的顺序不得改变"
    assert "被固定的首帧/尾帧必须严格一致" in request
    assert "用户修改只作用于未被固定帧之间的时间轴" in request
    assert request.count("把时间改为夜晚") == 1
