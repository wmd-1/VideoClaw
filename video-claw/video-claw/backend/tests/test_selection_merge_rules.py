"""选优合并规则回归：后台重生成与事件合并时的 `selected` 归属。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

from core.orchestrator import WorkflowEngine


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
        {"c1": "v1.png"},
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
        baseline_selected={"c1": "v1.png"},
    )
    item = merged["characters"][0]
    assert item["selected"] == "v2.png"
    assert "v3.png" in item["versions"]


def test_selected_snapshot_reads_item_containers():
    artifact = {
        "characters": [{"id": "c1", "selected": "v1.png"}, {"id": "c2"}],
        "settings": [{"id": "s1", "selected": "s1.png"}],
        "meta": {"k": 1},
    }
    assert WorkflowEngine._selected_snapshot(artifact) == {"c1": "v1.png", "c2": None, "s1": "s1.png"}
    assert WorkflowEngine._selected_snapshot(None) == {}
