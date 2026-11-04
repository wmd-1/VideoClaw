"""C2 第 2 步：asset_kind 枚举、默认值与生成/评估选路。"""
from __future__ import annotations

from core.agents.character_agent import CharacterDesignerAgent
from core.asset_kind import (
    ASSET_KIND_ANIMAL,
    ASSET_KIND_BUILDING,
    ASSET_KIND_HUMAN,
    ASSET_KIND_LOGO,
    ASSET_KIND_PLACE,
    ASSET_KIND_PRODUCT,
    asset_kind_for,
    item_asset_kind,
    normalize_asset_kind,
)


def test_normalize_and_container_defaults():
    assert normalize_asset_kind("human") == ASSET_KIND_HUMAN
    assert normalize_asset_kind("动物") == ASSET_KIND_ANIMAL
    assert normalize_asset_kind("产品") == ASSET_KIND_PRODUCT
    assert normalize_asset_kind("标识") == ASSET_KIND_LOGO
    assert normalize_asset_kind("建筑") == ASSET_KIND_BUILDING
    assert normalize_asset_kind("unknown-type") is None
    # 旧资产无字段：characters→human，settings→place
    assert asset_kind_for("", container="characters") == ASSET_KIND_HUMAN
    assert asset_kind_for("", container="settings") == ASSET_KIND_PLACE
    # 映射不到枚举归 other，不猜
    assert asset_kind_for("猫", container="characters") == "other"


def test_human_keeps_four_view_and_non_human_uses_asset_template():
    human = CharacterDesignerAgent._char_prompt("小陈", "银行职员", "风格", "", "human")
    assert "4 个视角" in human and "正面肖像" in human

    product = CharacterDesignerAgent._char_prompt("产品A", "金属机身", "风格", "", "product")
    assert "资产类型：产品" in product and "结构、材质与配色" in product
    assert "正面肖像" not in product

    animal = CharacterDesignerAgent._char_prompt("猫", "橘色短毛", "风格", "animal")
    assert "资产类型：动物" in animal and "体态、毛色" in animal

    logo = CharacterDesignerAgent._char_prompt("Logo", "圆形标识", "风格", "logo")
    assert "轮廓、比例与文字" in logo

    building = CharacterDesignerAgent._setting_prompt("大楼", "玻璃幕墙", "风格", "building")
    assert "资产类型：建筑" in building and "立面、结构与空间" in building
    place = CharacterDesignerAgent._setting_prompt("办公室", "会议室", "风格", "")
    assert "资产类型" not in place and "场景" not in place.splitlines()[0]


def test_eval_routes_human_vs_asset():
    human = CharacterDesignerAgent._eval_prompt_for("银行职员", "characters", "", "human")
    assert "4个角色视图" in human
    product = CharacterDesignerAgent._eval_prompt_for("金属机身", "characters", "", "product")
    assert "资产类型：产品" in product and "人体四视图" in product
    assert "4 个角色视图" not in product
    building = CharacterDesignerAgent._eval_prompt_for("玻璃幕墙", "settings", "", "building")
    assert "资产类型：建筑" in building


def test_payload_and_preview_include_asset_kind(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: [])
    chars = {
        "c1": {"name": "小陈", "description": "银行职员"},
        "c2": {"name": "产品A", "description": "金属机身", "asset_kind": "product"},
    }
    sets = {
        "s1": {"name": "会议室", "description": "会议室"},
        "s2": {"name": "大楼", "description": "玻璃幕墙", "asset_kind": "building"},
    }
    payload = agent._build_payload("sid", chars, sets)["payload"]
    by_id = {item["id"]: item for item in payload["characters"] + payload["settings"]}
    assert by_id["c1"]["asset_kind"] == "human"       # 旧资产默认
    assert by_id["c2"]["asset_kind"] == "product"
    assert by_id["s1"]["asset_kind"] == "place"
    assert by_id["s2"]["asset_kind"] == "building"

    preview = agent._build_preview("sid", chars, sets)
    preview_by_id = {item["id"]: item for item in preview["characters"] + preview["settings"]}
    assert preview_by_id["c1"]["asset_kind"] == "human"
    assert preview_by_id["s2"]["asset_kind"] == "building"


def test_item_asset_kind_compat_species():
    assert item_asset_kind({"asset_kind": "logo", "species": "产品"}, "characters") == "logo"
    assert item_asset_kind({"species": "产品"}, "characters") == "product"
    assert item_asset_kind({}, "settings") == "place"
