"""B1 回归：H3 与视频共用同一有序参考集（场景→角色）。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

from config import Config
from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.agents.video_agent import VideoDirectorAgent
from core.consumption import plan_targets, reference_assets_for_segment

SEGMENT = {"id": "seg_01_01", "location": "支行办公室", "characters": ["小陈", "小周"]}

ARTIFACTS = {
    "character_design": {
        "characters": [
            {"id": "c1", "name": "小陈", "selected": "/m/c1.png"},
            {"id": "c2", "name": "小周", "selected": "/m/c2.png"},
        ],
        "settings": [{"id": "s1", "name": "支行办公室", "selected": "/m/s1.png"}],
    },
    "reference_generation": {"scenes": [{"id": "seg_01_01", "selected": "/m/f1.png"}]},
    "storyboard": {"episodes": [{"episode_number": 1, "segments": [
        {"segment_id": "seg_01_01", "location": "支行办公室", "characters": ["小陈", "小周"],
         "total_duration": 5, "shots": [{"content": "镜头", "duration": 5}]},
    ]}]},
}


def test_reference_assets_order_is_scene_first_then_characters():
    ordered = reference_assets_for_segment(ARTIFACTS, SEGMENT)
    assert [(container, key) for container, key, _ in ordered] == [
        ("settings", "s1"),
        ("characters", "c1"),
        ("characters", "c2"),
    ]


def test_reference_assets_dedupes_by_selected_path_keeping_order():
    artifacts = {
        "character_design": {
            "characters": [
                {"id": "c1", "name": "小陈", "selected": "/m/same.png"},
                {"id": "c2", "name": "小周", "selected": "/m/same.png"},
            ],
            "settings": [{"id": "s1", "name": "支行办公室", "selected": "/m/s1.png"}],
        }
    }
    ordered = reference_assets_for_segment(artifacts, SEGMENT)
    # 场景在前；两个角色同图时只保留第一个
    assert [(container, key) for container, key, _ in ordered] == [
        ("settings", "s1"),
        ("characters", "c1"),
    ]


def test_prompt_rewrite_reference_mode_plans_same_ordered_assets():
    targets = plan_targets("prompt_rewrite", ARTIFACTS, mode="reference")
    assert [(t.container, t.item_id) for t in targets] == [
        ("settings", "s1"),
        ("characters", "c1"),
        ("characters", "c2"),
    ]
    assert all(t.label == "grounding" for t in targets)
    assert all(t.consumers == ("seg_01_01",) for t in targets)
    # 参考集模式不再把“片段首帧”当参考集
    assert all(t.source_stage == "character_design" for t in targets)


def test_prompt_rewrite_frame_mode_still_uses_scene_frames():
    targets = plan_targets("prompt_rewrite", ARTIFACTS, mode="first_frame")
    assert [(t.source_stage, t.container, t.item_id) for t in targets] == [
        ("reference_generation", "scenes", "seg_01_01"),
    ]


def test_agent_ref2va_grounding_matches_planned_order():
    images = PromptRewriteAgent()._grounding_images(
        SEGMENT, [SEGMENT], {}, "Ref2VA", artifacts=ARTIFACTS
    )
    assert images == ["/m/s1.png", "/m/c1.png", "/m/c2.png"]


def test_video_agent_reference_order_matches_plan(tmp_path):
    """锁死两侧同序：视频侧拼装也必须是场景→角色（需真实文件，它按存在性过滤）。"""
    files = {}
    for name in ("s1.png", "c1.png", "c2.png"):
        path = tmp_path / name
        path.write_bytes(b"img")
        files[name] = str(path)

    character_artifact = {
        "characters": [
            {"id": "c1", "name": "小陈", "selected": files["c1.png"]},
            {"id": "c2", "name": "小周", "selected": files["c2.png"]},
        ],
        "settings": [{"id": "s1", "name": "支行办公室", "selected": files["s1.png"]}],
    }
    paths = VideoDirectorAgent()._get_segment_reference_assets(SEGMENT, character_artifact)
    assert paths == [files["s1.png"], files["c1.png"], files["c2.png"]]


# ─────────────── 编号映射：<Picture N> 与入参同序且不位移 ───────────────


def _patch_vlm(monkeypatch, answer="画面描述"):
    class FakeVLM:
        def __init__(self):
            pass

        def query(self, prompt, image_paths=None, model=None):
            return answer

    monkeypatch.setattr("models.vlm_client.VLM", FakeVLM)
    monkeypatch.setattr(Config, "H3_REWRITE_GROUNDING_ENABLED", True)


def test_grounding_describe_numbers_images_in_order(monkeypatch, tmp_path):
    _patch_vlm(monkeypatch)
    paths = []
    for index in (1, 2):
        path = tmp_path / f"p{index}.png"
        path.write_bytes(b"img")
        paths.append(str(path))

    text, hashes = PromptRewriteAgent()._grounding_describe(paths, "vlm")
    assert text == "<Picture 1>：画面描述\n<Picture 2>：画面描述"
    assert len(hashes) == 2


def test_grounding_describe_keeps_numbering_when_middle_missing(monkeypatch, tmp_path):
    """中间一张不可读时写占位，后续编号不得前移。"""
    _patch_vlm(monkeypatch)
    first = tmp_path / "p1.png"
    first.write_bytes(b"img")
    third = tmp_path / "p3.png"
    third.write_bytes(b"img")

    paths = [str(first), str(tmp_path / "missing.png"), str(third)]
    text, hashes = PromptRewriteAgent()._grounding_describe(paths, "vlm")
    lines = text.split("\n")
    assert lines[0] == "<Picture 1>：画面描述"
    assert lines[1] == "<Picture 2>：（本图无可读描述）"
    assert lines[2] == "<Picture 3>：画面描述"
    assert len(hashes) == 2


def test_grounding_describe_returns_empty_when_all_unreadable(monkeypatch, tmp_path):
    _patch_vlm(monkeypatch)
    text, hashes = PromptRewriteAgent()._grounding_describe(
        [str(tmp_path / "a.png"), str(tmp_path / "b.png")], "vlm"
    )
    assert (text, hashes) == ("", [])
