"""B3 回归：邻接只从完整有序分镜计算，目标集合只决定执行范围。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

import asyncio

from core.agents.prompt_rewrite_agent import PromptRewriteAgent


def _segment(index: int) -> dict:
    return {
        "id": f"seg_01_{index:02d}",
        "name": f"第1集-片段{index}",
        "index": index,
        "episode": 1,
        "shot_content": f"镜头{index}",
        "duration": 5,
        "characters": [],
        "location": "",
    }


SEGMENTS = [_segment(1), _segment(2), _segment(3)]
SCENE_MAP = {
    "seg_01_01": {"id": "seg_01_01", "selected": "a.png"},
    "seg_01_02": {"id": "seg_01_02", "selected": "b.png"},
    "seg_01_03": {"id": "seg_01_03", "selected": "c.png"},
}


def test_next_in_order_reads_full_list_and_handles_boundaries():
    assert PromptRewriteAgent._next_in_order(SEGMENTS, "seg_01_01")["id"] == "seg_01_02"
    assert PromptRewriteAgent._next_in_order(SEGMENTS, "seg_01_02")["id"] == "seg_01_03"
    assert PromptRewriteAgent._next_in_order(SEGMENTS, "seg_01_03") is None
    assert PromptRewriteAgent._next_in_order(SEGMENTS, "missing") is None


def test_grounding_images_uses_full_order_not_target_subset():
    agent = PromptRewriteAgent()
    target = SEGMENTS[0]
    assert agent._grounding_images(target, SEGMENTS, SCENE_MAP, "FL2VA") == ["a.png", "b.png"]
    # 反证：传入目标子集时尾帧消失——该参数必须是完整列表
    assert agent._grounding_images(target, [target], SCENE_MAP, "FL2VA") == ["a.png"]


def test_grounding_images_last_segment_has_no_tail():
    agent = PromptRewriteAgent()
    assert agent._grounding_images(SEGMENTS[-1], SEGMENTS, SCENE_MAP, "FL2VA") == ["c.png"]


def test_grounding_images_non_frame_mode_only_first():
    agent = PromptRewriteAgent()
    assert agent._grounding_images(SEGMENTS[0], SEGMENTS, SCENE_MAP, "I2VA") == ["a.png"]


def _patch_agent(monkeypatch, agent, seen, images_calls):
    """把 LLM/grounding 换掉，只记录“每条目解析出的模式”与“实际提交给 VLM 的图”。"""

    async def fake_rewrite_one(segment, mode, system_prompt, guide, llm_model, sections):
        seen[segment["id"]] = mode
        return "改写结果", [], ""

    def fake_grounding(images, vlm_model, session_id=""):
        images_calls.append(list(images))
        return "", []

    monkeypatch.setattr(agent, "_check_cancel", lambda: None)
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_session_meta", lambda input_data: {})
    monkeypatch.setattr(agent, "_session_artifacts", lambda input_data: {
        "reference_generation": {"scenes": list(SCENE_MAP.values())},
        "character_design": {},
    })
    monkeypatch.setattr(agent, "_load_guide", lambda mode: {})
    monkeypatch.setattr(agent, "_grounding_describe", fake_grounding)
    monkeypatch.setattr(agent, "_rewrite_one", fake_rewrite_one)


def test_single_target_mode_and_grounding_use_full_order(monkeypatch):
    """单条重写只处理 A 时：模式为 FL2VA，且实际交给 grounding 的图是 A、B。"""
    agent = PromptRewriteAgent()
    seen, images_calls = {}, []
    _patch_agent(monkeypatch, agent, seen, images_calls)

    input_data = {"video_generation_mode": "start_end_frame"}
    asyncio.run(agent._rewrite_segments([SEGMENTS[0]], input_data, {}, ordered_segments=SEGMENTS))
    assert seen == {"seg_01_01": "FL2VA"}
    assert images_calls == [["a.png", "b.png"]], "grounding 消耗的图必须与全量邻接一致"

    # 反证：不传 ordered 时只看到目标本身，模式退化为 I2VA、也无尾帧图
    seen.clear()
    images_calls.clear()
    asyncio.run(agent._rewrite_segments([SEGMENTS[0]], input_data, {}))
    assert seen == {"seg_01_01": "I2VA"}
    assert images_calls == [["a.png"]]


def test_non_contiguous_batch_keeps_full_adjacency(monkeypatch):
    """A、C 批量：A 仍引用 B（不是 C），C 为末段无尾帧。"""
    agent = PromptRewriteAgent()
    seen, images_calls = {}, []
    _patch_agent(monkeypatch, agent, seen, images_calls)

    asyncio.run(agent._rewrite_segments(
        [SEGMENTS[0], SEGMENTS[2]], {"video_generation_mode": "start_end_frame"}, {},
        ordered_segments=SEGMENTS,
    ))
    assert seen == {"seg_01_01": "FL2VA", "seg_01_03": "I2VA"}
    assert images_calls[0] == ["a.png", "b.png"], "A 的尾帧应为 B，而不是同批的 C"


def test_revision_keeps_full_adjacency(monkeypatch):
    """用户修订 B 时同样按全量算邻接 → FL2VA。"""
    agent = PromptRewriteAgent()
    seen, images_calls = {}, []
    _patch_agent(monkeypatch, agent, seen, images_calls)

    prev_items = {"seg_01_02": {"id": "seg_01_02", "rewritten_prompt": "旧正文"}}
    result = asyncio.run(agent._process_revisions(
        [{"id": "seg_01_02", "instruction": "节奏更紧凑"}],
        SEGMENTS, prev_items, {"video_generation_mode": "start_end_frame"},
    ))
    assert seen == {"seg_01_02": "FL2VA"}
    assert images_calls[0] == ["b.png", "c.png"]
    assert result["payload"]["items"][0]["id"] == "seg_01_02"
