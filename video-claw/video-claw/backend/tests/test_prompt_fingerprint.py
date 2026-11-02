"""Q1 回归：H3 输入指纹（覆盖全集、可复现、参考集按路径）。"""
from __future__ import annotations

import copy

from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.media_refs import ResolvedMedia
from core.prompt_fingerprint import build_input_fingerprint, media_ref

BASE = dict(
    shot_content="镜头：小陈推门进入办公室",
    duration=8,
    mode="I2VA",
    references=[("reference_generation", "scenes", "/code/result/_media/a.png")],
    style="写实",
    facts=[{"kind": "character", "id": "c1", "name": "小陈", "description": "银行职员"}],
)


def _fp(**overrides):
    return build_input_fingerprint(**{**BASE, **overrides})


def test_same_inputs_produce_same_fingerprint():
    assert _fp() == _fp()


def test_each_dimension_changes_fingerprint():
    base = _fp()
    assert _fp(shot_content="镜头：小陈离开") != base
    assert _fp(duration=10) != base
    assert _fp(mode="FL2VA") != base
    assert _fp(references=[("reference_generation", "scenes", "/code/result/_media/b.png")]) != base
    assert _fp(style="动漫") != base
    assert _fp(facts=[]) != base


def test_local_absolute_paths_are_normalized_to_basename():
    assert media_ref("/tmp/x/a.png") == media_ref("/home/y/a.png") == "a.png"
    assert _fp(references=[("r", "s", "/tmp/x/a.png")]) == _fp(
        references=[("r", "s", "/home/y/a.png")]
    )


def test_public_paths_are_kept_verbatim():
    assert media_ref("/code/result/_media/a.png") == "/code/result/_media/a.png"


def test_resolved_media_identity_uses_public_path():
    """同一素材：本地缓存形态（_refs）与 canonical public path 必须同指纹。"""
    resolved = ResolvedMedia("/code/work/_refs/o/abc.png", public_path="/code/result/_media/a.png")
    assert media_ref(resolved) == "/code/result/_media/a.png"
    assert _fp(references=[("r", "s", resolved)]) == _fp(
        references=[("r", "s", "/code/result/_media/a.png")]
    )


def test_http_query_is_stripped():
    assert media_ref("https://host/p/a.png?X-Amz-Signature=abc") == "https://host/p/a.png"


def test_grounding_model_changes_fingerprint():
    """grounding 文本不进指纹（派生结果），但模型配置变化必须失效。"""
    base = _fp()
    same = _fp(grounding_model="vlm-a")
    assert same != base
    assert same == _fp(grounding_model="vlm-a")
    assert same != _fp(grounding_model="vlm-b")


def test_fact_order_does_not_matter():
    facts = [
        {"kind": "character", "id": "c1", "name": "小陈"},
        {"kind": "character", "id": "c2", "name": "小周"},
    ]
    assert _fp(facts=list(reversed(facts))) == _fp(facts=facts)


SEGMENT = {"id": "seg_01_01", "shot_content": "镜头", "duration": 8,
           "characters": ["小陈"], "location": "支行办公室"}

ARTIFACTS = {
    "character_design": {
        "characters": [{"id": "c1", "name": "小陈", "selected": "/code/result/_media/c1.png",
                        "description": "银行职员"}],
        "settings": [{"id": "s1", "name": "支行办公室", "selected": "/code/result/_media/s1.png"}],
    },
    "reference_generation": {"scenes": [{"id": "seg_01_01", "selected": "/code/result/_media/f1.png"}]},
}


def test_agent_fingerprint_reacts_to_selected_reference_change():
    agent = PromptRewriteAgent()
    first = agent._input_fingerprint(SEGMENT, "Ref2VA", [SEGMENT], {}, ARTIFACTS, {}, {})
    assert first == agent._input_fingerprint(SEGMENT, "Ref2VA", [SEGMENT], {}, ARTIFACTS, {}, {})

    changed = copy.deepcopy(ARTIFACTS)
    changed["character_design"]["characters"][0]["selected"] = "/code/result/_media/c1_v2.png"
    assert agent._input_fingerprint(SEGMENT, "Ref2VA", [SEGMENT], {}, changed, {}, {}) != first


def test_agent_fingerprint_frame_mode_uses_scene_selection():
    agent = PromptRewriteAgent()
    scene_map = {"seg_01_01": {"id": "seg_01_01", "selected": "/code/result/_media/f1.png"}}
    base = agent._input_fingerprint(SEGMENT, "I2VA", [SEGMENT], scene_map, ARTIFACTS, {}, {})
    scene_map["seg_01_01"]["selected"] = "/code/result/_media/f2.png"
    assert agent._input_fingerprint(SEGMENT, "I2VA", [SEGMENT], scene_map, ARTIFACTS, {}, {}) != base


def test_make_item_carries_input_fingerprint():
    item = PromptRewriteAgent._make_item(
        {"id": "s1", "name": "第1集-片段1", "index": 1, "shot_content": "镜头", "duration": 8},
        "正文", "I2VA", fingerprint="abc123",
    )
    assert item["input_fingerprint"] == "abc123"


FACTS_ARTIFACTS = {
    "characters": [
        {"id": "c1", "name": "小陈", "description": "银行职员"},
        {"id": "c2", "name": "小陈的同事", "description": "其他"},
    ],
    "settings": [{"id": "s1", "name": "支行办公室", "description": "办公室"}],
}


def test_facts_use_the_same_matching_as_authoritative_context():
    """指纹与提示词必须命中同一集合：精确名称匹配，不做模糊。"""
    segment = {"id": "s1", "shot_content": "镜头", "duration": 8,
               "characters": ["小陈"], "location": "支行办公室"}
    facts = PromptRewriteAgent._authority_facts(segment, FACTS_ARTIFACTS)
    assert [(item["kind"], item["id"]) for item in facts] == [
        ("character", "c1"),
        ("setting", "s1"),
    ]

    context = PromptRewriteAgent._authoritative_context(segment, FACTS_ARTIFACTS)
    assert "银行职员" in context
    assert "其他" not in context


def test_unmatched_character_is_kept_in_both_facts_and_context():
    """分镜写了不存在的角色：提示词与指纹都要保留这行名称，否则两边不一致。"""
    segment = {"id": "s1", "shot_content": "镜头", "duration": 8,
               "characters": ["小陈", "路人甲"], "location": "支行办公室"}
    facts = PromptRewriteAgent._authority_facts(segment, FACTS_ARTIFACTS)
    unmatched = [item for item in facts if item["name"] == "路人甲"]
    assert unmatched and unmatched[0]["id"] == ""

    context = PromptRewriteAgent._authoritative_context(segment, FACTS_ARTIFACTS)
    assert "- 路人甲" in context
