"""选优合并规则回归：后台重生成与事件合并时的 `selected` 归属。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

from core.orchestrator import _BACKGROUND_PROTECTED_FIELDS, WorkflowEngine


def _artifact():
    return {
        "characters": [
            {"id": "c1", "status": "done", "selected": "v1.png", "versions": ["v1.png"]},
            {"id": "c2", "status": "done", "selected": "x1.png", "versions": ["x1.png"]},
        ]
    }


def test_asset_update_prefers_new_selection():
    """后台事件带来新选中时必须生效，否则界面显示新图而落库仍是旧图。"""
    artifact = _artifact()
    WorkflowEngine._merge_asset_update(
        artifact,
        {"type": "characters", "id": "c1", "status": "done",
         "selected": "v3.png", "versions": ["v1.png", "v3.png"]},
    )
    assert artifact["characters"][0]["selected"] == "v3.png"


def test_asset_update_keeps_old_selection_when_new_is_empty():
    artifact = _artifact()
    WorkflowEngine._merge_asset_update(
        artifact,
        {"type": "characters", "id": "c1", "status": "failed", "selected": "", "versions": ["v1.png"]},
    )
    assert artifact["characters"][0]["selected"] == "v1.png"


def test_regeneration_merge_updates_only_target_items():
    merged = WorkflowEngine._merge_item_regeneration_payload(
        _artifact(),
        {
            "characters": [
                {"id": "c1", "status": "done", "selected": "v3.png", "versions": ["v1.png", "v3.png"]},
                {"id": "c2", "status": "done", "selected": "x9.png", "versions": ["x1.png", "x9.png"]},
            ]
        },
        ["characters"],
        {"characters": {"c1"}},
    )
    by_id = {item["id"]: item for item in merged["characters"]}
    assert by_id["c1"]["selected"] == "v3.png", "目标条目的新选中未生效"
    assert by_id["c2"]["selected"] == "x1.png", "非目标条目被本轮结果覆盖"


def test_selected_after_asset_update_prefers_new():
    assert WorkflowEngine._selected_after_asset_update("v1.png", "v3.png") == "v3.png"
    assert WorkflowEngine._selected_after_asset_update("v1.png", "") == "v1.png"
    assert WorkflowEngine._selected_after_asset_update("", "v3.png") == "v3.png"
    assert WorkflowEngine._selected_after_asset_update(None, None) == ""


def test_background_protection_declares_description_and_rewritten_prompt_only():
    assert _BACKGROUND_PROTECTED_FIELDS == {
        "character_design": ("description",),
        "prompt_rewrite": ("rewritten_prompt",),
    }


def test_background_prompt_rewrite_keeps_user_text_but_uses_new_inputs():
    existing = {"items": [{
        "id": "i1", "status": "done", "rewritten_prompt": "用户改后的正文",
        "input_fingerprint": "old", "input_dimensions": {"duration": "d1"},
    }]}
    payload = {"items": [{
        "id": "i1", "status": "done", "rewritten_prompt": "模型新正文",
        "input_fingerprint": "new", "input_dimensions": {"duration": "d2"},
    }]}
    merged = WorkflowEngine._merge_item_regeneration_payload(
        existing, payload, ["items"], {"items": {"i1"}},
        baseline_fields={"items": {"i1": {"rewritten_prompt": "旧正文"}}},
        protected_fields=_BACKGROUND_PROTECTED_FIELDS["prompt_rewrite"],
    )
    item = merged["items"][0]
    assert item["rewritten_prompt"] == "用户改后的正文", "用户正文被后台覆盖"
    assert item["input_fingerprint"] == "new", "新输入的指纹必须生效"
    assert item["input_dimensions"] == {"duration": "d2"}


def test_background_prompt_rewrite_updates_when_not_edited():
    existing = {"items": [{
        "id": "i1", "status": "done", "rewritten_prompt": "旧正文",
        "input_fingerprint": "old",
    }]}
    payload = {"items": [{
        "id": "i1", "status": "done", "rewritten_prompt": "模型新正文",
        "input_fingerprint": "new",
    }]}
    merged = WorkflowEngine._merge_item_regeneration_payload(
        existing, payload, ["items"], {"items": {"i1"}},
        baseline_fields={"items": {"i1": {"rewritten_prompt": "旧正文"}}},
        protected_fields=_BACKGROUND_PROTECTED_FIELDS["prompt_rewrite"],
    )
    assert merged["items"][0]["rewritten_prompt"] == "模型新正文"


def test_background_character_protects_description_but_not_name():
    existing = {"characters": [{
        "id": "c1", "name": "用户改名", "description": "用户改描述",
        "selected": "v1.png", "versions": ["v1.png"], "status": "done",
    }]}
    payload = {"characters": [{
        "id": "c1", "name": "模型新名", "description": "模型新描述",
        "selected": "v1.png", "versions": ["v1.png"], "status": "done",
    }]}
    merged = WorkflowEngine._merge_item_regeneration_payload(
        existing, payload, ["characters"], {"characters": {"c1"}},
        baseline_fields={"characters": {"c1": {
            "name": "旧名", "description": "旧描述", "selected": "v1.png",
        }}},
        protected_fields=_BACKGROUND_PROTECTED_FIELDS["character_design"],
    )
    item = merged["characters"][0]
    assert item["description"] == "用户改描述", "description 应保护"
    assert item["name"] == "模型新名", "name 是下游匹配键，不应保护"


# ─────────────── Q8 安全阀：执行期间手选不被晚到结果覆盖 ───────────────


def test_selected_after_asset_update_keeps_in_run_user_edit():
    """基线是 v1、当前已是 v2（执行期间手改）→ 晚到的 v3 不得覆盖。"""
    assert WorkflowEngine._selected_after_asset_update("v2.png", "v3.png", "v1.png") == "v2.png"
    # 没有手改（当前 == 基线）→ 本轮结果生效
    assert WorkflowEngine._selected_after_asset_update("v1.png", "v3.png", "v1.png") == "v3.png"
    # 基线为空、当前仍为空 → 本轮结果生效
    assert WorkflowEngine._selected_after_asset_update("", "v3.png", "") == "v3.png"


def test_asset_update_keeps_in_run_user_edit_with_baseline():
    artifact = {"characters": [{"id": "c1", "status": "done", "selected": "v2.png",
                                "versions": ["v1.png", "v2.png"]}]}
    WorkflowEngine._merge_asset_update(
        artifact,
        {"type": "characters", "id": "c1", "status": "done",
         "selected": "v3.png", "versions": ["v1.png", "v2.png", "v3.png"]},
        {"characters": {"c1": {"selected": "v1.png"}}},
    )
    assert artifact["characters"][0]["selected"] == "v2.png"
    # 版本仍合并，不因保留选中而丢掉新版本
    assert "v3.png" in artifact["characters"][0]["versions"]


def test_regeneration_merge_keeps_in_run_user_edit():
    """全链：起点 v1 → 执行中改 v2 → 本轮产出 v3 → 落库保持 v2。"""
    existing = {"characters": [{"id": "c1", "status": "done", "selected": "v2.png",
                                 "versions": ["v1.png", "v2.png"]}]}
    payload = {"characters": [{"id": "c1", "status": "done", "selected": "v3.png",
                               "versions": ["v1.png", "v2.png", "v3.png"]}]}
    merged = WorkflowEngine._merge_item_regeneration_payload(
        existing, payload, ["characters"], {"characters": {"c1"}},
        baseline_fields={"characters": {"c1": {"selected": "v1.png"}}},
    )
    item = merged["characters"][0]
    assert item["selected"] == "v2.png"
    assert "v3.png" in item["versions"]


def test_user_edit_snapshot_is_container_keyed_with_fields():
    artifact = {
        "characters": [{"id": "c1", "selected": "v1.png", "description": "旧描述",
                        "internal": "不应进入快照"}],
        "settings": [{"id": "s1", "selected": "s1.png"}],
        "meta": {"k": 1},
    }
    snapshot = WorkflowEngine._user_edit_snapshot(artifact)
    assert snapshot["characters"]["c1"] == {"selected": "v1.png", "description": "旧描述"}
    assert snapshot["settings"]["s1"] == {"selected": "s1.png"}
    assert "internal" not in snapshot["characters"]["c1"]
    assert WorkflowEngine._user_edit_snapshot(None) == {}


# ─────────────── A1b：后台按阶段保护 description ───────────────

_PROTECTED = ("description",)
_BASE = {"characters": {"c1": {"selected": "v1.png", "description": "旧描述"}}}


def _regenerate(current: dict, payload_item: dict, baseline=_BASE):
    existing = {"characters": [dict({"id": "c1", "status": "done",
                                      "versions": ["v1.png"]}, **current)]}
    payload = {"characters": [dict({"id": "c1", "status": "done"}, **payload_item)]}
    merged = WorkflowEngine._merge_item_regeneration_payload(
        existing, payload, ["characters"], {"characters": {"c1"}},
        baseline_fields=baseline, protected_fields=_PROTECTED,
    )
    return merged["characters"][0]


def test_protected_description_keeps_in_run_user_edit():
    """手改描述 + 本轮生成成功 → 保留用户描述，但新图与新版本仍生效。"""
    item = _regenerate(
        {"selected": "v1.png", "description": "用户新描述", "versions": ["v1.png"]},
        {"selected": "v2.png", "description": "旧描述", "versions": ["v1.png", "v2.png"]},
    )
    assert item["description"] == "用户新描述"
    assert item["selected"] == "v2.png"
    assert item["versions"] == ["v1.png", "v2.png"]


def test_protected_description_overwritten_when_not_edited():
    """反向：未手改时本轮描述必须生效，防止“全保留”的过度保护。"""
    item = _regenerate(
        {"selected": "v1.png", "description": "旧描述", "versions": ["v1.png"]},
        {"selected": "v2.png", "description": "本轮新描述", "versions": ["v1.png", "v2.png"]},
    )
    assert item["description"] == "本轮新描述"
    assert item["selected"] == "v2.png"


def test_unprotected_field_still_takes_payload_value():
    """name 不在 A1b 保护范围内：即使执行期间手改，也以本轮 payload 为准。"""
    base = {"characters": {"c1": {"selected": "v1.png", "name": "旧名", "description": "旧描述"}}}
    item = _regenerate(
        {"selected": "v1.png", "name": "用户新名", "description": "旧描述", "versions": ["v1.png"]},
        {"selected": "v2.png", "name": "本轮旧名", "description": "旧描述",
         "versions": ["v1.png", "v2.png"]},
        baseline=base,
    )
    assert item["name"] == "本轮旧名"


def test_protected_description_empty_payload_falls_back():
    """payload 送空字符串视为“本轮没有该值”，回退当前值。"""
    item = _regenerate(
        {"selected": "v1.png", "description": "用户新描述", "versions": ["v1.png"]},
        {"selected": "v2.png", "description": "", "versions": ["v1.png", "v2.png"]},
    )
    assert item["description"] == "用户新描述"


# ─────────────── 全量路径：重构后与旧语义逐字等价 ───────────────


def _prefer(base_item: dict, latest_item: dict):
    base = {"characters": [dict({"id": "c1"}, **base_item)]}
    latest = {"characters": [dict({"id": "c1"}, **latest_item)]}
    payload = {"characters": [{"id": "c1"}]}
    merged = WorkflowEngine._prefer_user_edits("character_design", base, latest, payload)
    return merged["characters"][0]


def test_prefer_user_edits_does_not_write_null_for_new_field():
    """基线缺字段、当前值为 None：旧实现不写键，重构后必须仍然不写。"""
    item = _prefer({}, {"species": None})
    assert "species" not in item


def test_prefer_user_edits_writes_new_field_when_value_present():
    item = _prefer({}, {"species": "cat"})
    assert item["species"] == "cat"

