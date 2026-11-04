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
