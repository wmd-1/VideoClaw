"""WF-08／WF-09 接线回归：首帧原文来源与 H3 场景权威描述。

只允许在既有后端容器内执行（详见 tests/conftest.py）：
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \
    /app/.venv/bin/python -m pytest /tmp/vc_tests -q
"""
from __future__ import annotations

from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.agents.reference_agent import ReferenceGeneratorAgent

ORIGINAL = "第1集\n小陈走进办公室，把材料放在桌上。"


# ─────────────── WF-08：首帧原文取 meta.original_text ───────────────


def test_original_text_prefers_meta_field():
    assert ReferenceGeneratorAgent._script_original_text(
        {"meta": {"original_text": ORIGINAL}, "original_text": "顶层旧值"}
    ) == ORIGINAL


def test_original_text_falls_back_to_legacy_top_level():
    assert ReferenceGeneratorAgent._script_original_text({"original_text": "旧格式原文"}) == "旧格式原文"


def test_original_text_empty_meta_falls_back_and_tolerates_bad_input():
    assert ReferenceGeneratorAgent._script_original_text(
        {"meta": {"original_text": "   "}, "original_text": "顶层旧值"}
    ) == "顶层旧值"
    assert ReferenceGeneratorAgent._script_original_text({"meta": None}) == ""
    assert ReferenceGeneratorAgent._script_original_text(None) == ""


# ─────────────── WF-09：H3 地点字段与场景权威描述 ───────────────

STORYBOARD = {
    "episodes": [
        {
            "episode_number": 1,
            "segments": [
                {
                    "segment_id": "seg_01_01",
                    "total_duration": 8,
                    "characters": ["小陈"],
                    "location": "支行办公室",
                    "shots": [{"content": "小陈整理材料", "duration": 8}],
                },
                {
                    "segment_id": "seg_01_02",
                    "total_duration": 6,
                    "characters": [],
                    "setting": "旧字段场景",
                    "shots": [{"content": "门口有人经过", "duration": 6}],
                },
            ],
        }
    ]
}

CHARACTER_ART = {
    "characters": [{"id": "c1", "name": "小陈", "description": "三十岁客户经理，深蓝西装"}],
    "settings": [
        {"id": "s1", "name": "支行办公室", "description": "米色墙面，长条会议桌"},
        {"id": "s2", "name": "支行大厅", "description": "大理石地面，玻璃门"},
    ],
}


def test_collect_segments_reads_location_and_legacy_setting():
    segments = PromptRewriteAgent._collect_segments(STORYBOARD)
    by_id = {item["id"]: item for item in segments}
    assert by_id["seg_01_01"]["location"] == "支行办公室"
    # 旧产物只有 setting 时同样可用，且归一后不再保留 setting 键
    assert by_id["seg_01_02"]["location"] == "旧字段场景"
    assert "setting" not in by_id["seg_01_02"]


def test_authoritative_context_injects_matched_scene_description():
    text = PromptRewriteAgent._authoritative_context(
        {"characters": ["小陈"], "location": "支行办公室"}, CHARACTER_ART
    )
    assert "三十岁客户经理，深蓝西装" in text
    assert "米色墙面，长条会议桌" in text
    # 不匹配的场景描述不得串入
    assert "大理石地面" not in text


def test_authoritative_context_matches_scene_by_stable_id():
    text = PromptRewriteAgent._authoritative_context({"characters": [], "location": "s2"}, CHARACTER_ART)
    assert "大理石地面，玻璃门" in text


def test_authoritative_context_keeps_name_when_scene_unknown():
    text = PromptRewriteAgent._authoritative_context({"characters": [], "location": "不存在的场景"}, CHARACTER_ART)
    assert "不存在的场景" in text
    assert "米色墙面" not in text
    assert "大理石地面" not in text


def test_authoritative_context_tolerates_missing_artifact_fields():
    text = PromptRewriteAgent._authoritative_context({"characters": ["小陈"], "location": "支行办公室"}, {})
    assert "小陈" in text
    assert "支行办公室" in text
    assert PromptRewriteAgent._authoritative_context({"characters": [], "location": ""}, None) == ""


def test_first_frame_call_sites_use_helper():
    """接线守卫：两处首帧调用点必须走 `_script_original_text`。

    上面的 helper 单测在“调用点回退成 `script_json.get("original_text")`”时仍会全绿，
    这里直接检查调用点，避免接线退化不被发现。
    """
    from pathlib import Path

    import core.agents.reference_agent as mod

    source = Path(mod.__file__).read_text(encoding="utf-8-sig")
    assert 'original_text=script_json.get(' not in source
    assert source.count("original_text=self._script_original_text(script_json)") == 2
