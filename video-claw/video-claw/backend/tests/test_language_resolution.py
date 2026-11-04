"""C2/Q4：会话语言统一解析、优先级与回填。"""
from __future__ import annotations

import uuid

from api.dependencies import workflow_engine
from core.agents.script_agent import ScriptWriterAgent
from core.language import (
    SOURCE_AMBIGUOUS,
    SOURCE_BODY,
    SOURCE_DEFAULT,
    SOURCE_INSTRUCTION,
    SOURCE_USER,
    normalize_language,
    resolve_language,
)


def test_explicit_user_setting_wins():
    assert resolve_language(explicit="en", instruction="用中文", body="全是中文" * 20) == {
        "language": "en", "source": SOURCE_USER,
    }
    assert normalize_language("en-US") == "en"
    assert normalize_language("中文") == "zh"
    assert normalize_language("enigma") is None
    assert normalize_language("zhihu") is None


def test_instruction_precedes_body():
    assert resolve_language(instruction="请用英文写", body="中文正文" * 20)["source"] == SOURCE_INSTRUCTION
    assert resolve_language(instruction="请用英文写", body="中文正文" * 20)["language"] == "en"


def test_narrative_words_do_not_trigger_language_switch():
    assert resolve_language(instruction="写一个英语老师的故事")["language"] == "zh"
    assert resolve_language(
        instruction="小陈：你英语真好。", body="小陈：你英语真好。" * 10,
    )["language"] == "zh"
    assert resolve_language(instruction="加一行 English 字幕")["language"] == "zh"
    assert resolve_language(instruction="改编自英文剧本的故事")["language"] == "zh"


def test_body_threshold_and_title_fallback():
    assert resolve_language(body="这是一段完全中文的正文内容用于测试") == {
        "language": "zh", "source": SOURCE_BODY,
    }
    assert resolve_language(body="This is an entirely English body used for testing.") == {
        "language": "en", "source": SOURCE_BODY,
    }
    assert resolve_language(title="English Only Title")["language"] == "en"


def test_ambiguous_keeps_current_or_defaults():
    mixed = "中文abc中文abc中文abc"
    assert resolve_language(body=mixed, current="en") == {
        "language": "en", "source": SOURCE_AMBIGUOUS,
    }
    assert resolve_language(body="") == {"language": "zh", "source": SOURCE_DEFAULT}


def _sid() -> str:
    return f"lang_{uuid.uuid4().hex[:8]}"


def test_backfill_on_stage_prepare_persists_language():
    sid = _sid()
    workflow_engine.create_session(sid, {"idea": "一个关于银行网点服务的宣传片创意"}, owner="alice")
    workflow_engine.prepare_stage_execution(sid, "script_generation", {})
    meta = workflow_engine.get_state(sid).meta
    assert meta["language"] == "zh"
    assert meta["language_source"] == SOURCE_BODY


def test_backfill_english_idea():
    sid = _sid()
    workflow_engine.create_session(
        sid, {"idea": "A promotional film about a bank branch and its digital services."}, owner="alice",
    )
    workflow_engine.prepare_stage_execution(sid, "script_generation", {})
    meta = workflow_engine.get_state(sid).meta
    assert meta["language"] == "en"


def test_backfill_ignores_narrative_english_words_in_idea():
    sid = _sid()
    workflow_engine.create_session(
        sid, {"idea": "写一个英语老师的故事，发生在中文课堂。"}, owner="alice",
    )
    workflow_engine.prepare_stage_execution(sid, "script_generation", {})
    meta = workflow_engine.get_state(sid).meta
    assert meta["language"] == "zh"


def test_request_language_validation():
    import pytest
    from pydantic import ValidationError

    from api.schemas.project import ProjectStartRequest

    assert ProjectStartRequest(idea="x", language="en-US").language == "en"
    assert ProjectStartRequest(idea="x", language="").language is None
    with pytest.raises(ValidationError):
        ProjectStartRequest(idea="x", language="ja")


def test_agent_prefers_stored_language_over_body():
    agent = ScriptWriterAgent()
    assert agent._session_language(
        {"_session_meta": {"language": "en"}}, body="中文正文" * 20,
    ) == "en"
    assert agent._session_language(
        {"_session_meta": {}}, body="中文正文" * 20,
    ) == "zh"


def test_smart_continue_language_routing_and_en_template():
    from core.agents.script_agent import ScriptWriterAgent
    from prompts.loader import load_prompt

    agent = ScriptWriterAgent()
    assert agent._continuation_language({"_session_meta": {"language": "en"}}) == "en"
    assert agent._continuation_language({"_session_meta": {"language": "zh"}}) == "zh"
    assert agent._continuation_language({}, body="This is an English script body. " * 5) == "en"
    assert agent._continuation_language(
        {"_session_meta": {"title": "An English Title For The Sequel"}},
    ) == "en"

    en = load_prompt("script", "smart_continue_script", "en")
    zh = load_prompt("script", "smart_continue_script", "zh")
    for placeholder in ("episodes_text", "chars_text", "settings_text", "sequel_idea",
                        "episodes_to_add", "start_episode_num", "film_type", "subject_mode"):
        assert "{" + placeholder + "}" in en
        assert "{" + placeholder + "}" in zh
    assert "Episode" in en and "第" in zh


def test_smart_continue_prompts_all_use_en(monkeypatch):
    import asyncio

    from core.agents import script_agent as sa

    recorded = []
    real_prompt = sa._get_script_prompt

    def fake_prompt(name, lang="zh"):
        recorded.append((name, lang))
        return real_prompt(name, lang)

    monkeypatch.setattr(sa, "_get_script_prompt", fake_prompt)
    monkeypatch.setattr("models.llm_client.LLM", lambda: object())

    agent = sa.ScriptWriterAgent()
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_save_result", lambda *a, **k: None)

    script = "Episode 2\n**2-1 Day INT Office**\nCharacters: Chen\n<action>Chen enters.</action>\nChen: Hello."

    def fake_query(llm, prompt, image_urls=None, model="m", safe_content=True, task_id=None, web_search=False):
        if "new_characters" in prompt and "new_settings" in prompt:
            return '{"new_characters": [], "new_settings": []}'
        if "episode_number" in prompt and "JSON array" in prompt:
            return '[{"episode_number": 2, "act_title": "Episode 2", "content": "x"}]'
        if "promotional-film screenwriter" in prompt or "Please optimize and revise" in prompt:
            return script
        return "ok"

    monkeypatch.setattr(agent, "_cancellable_query", fake_query)

    input_data = {
        "session_id": "s-en",
        "llm_model": "fake",
        "web_search": False,
        "episodes": [{"episode_number": 1, "content": "Episode 1"}],
        "characters": [{"name": "Chen"}],
        "settings": [{"name": "Office"}],
        "_session_meta": {"language": "en", "title": "English Series"},
        "meta": {"film_type": "介绍", "subject_mode": "旁白描述"},
    }
    result = asyncio.run(agent.process(
        input_data,
        {"action": "smart_continue", "sequel_idea": "Next episode", "episodes_to_add": 1},
    ))
    assert result["requires_intervention"] is True
    names = {name for name, _ in recorded}
    assert {"smart_continue_script", "eval_dialogue", "eval_plot", "revise_script",
            "meta_extract_sequel", "act_extract_sequel"} <= names
    assert all(lang == "en" for _, lang in recorded), recorded
