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
    asset_eval_view_rule,
    asset_kind_for,
    asset_view_requirement,
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
    assert "单张正面/平铺" in logo and "至少 3 个稳定视角" not in logo

    product_views = CharacterDesignerAgent._char_prompt("产品A", "金属机身", "风格", "", "product")
    assert "至少 3 个稳定视角" in product_views

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
    logo = CharacterDesignerAgent._eval_prompt_for("圆形标识", "characters", "", "logo")
    assert "单张正面/平铺" in logo
    assert "缺少正面/侧面/背面" not in logo
    assert asset_view_requirement("logo") != asset_view_requirement("product")
    assert "平铺标识" in asset_eval_view_rule("logo")


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


def test_import_artifact_passes_asset_kind():
    from core.script_import import build_import_artifact, merge_characters, merge_settings

    merged_chars = merge_characters(
        [], [{"name": "小猫", "species": "猫", "asset_kind": "animal"}],
    )
    assert merged_chars[0]["asset_kind"] == "animal"
    merged_sets = merge_settings([{"name": "仓库", "asset_kind": "building"}])
    assert merged_sets[0]["asset_kind"] == "building"

    raw = "第1集\n第1集-第1场 日 内 仓库\n人物：小猫\n<action>小猫走进仓库。</action>"
    artifact = build_import_artifact(
        raw, session_id="s1",
        inferred_characters=[{"name": "小猫", "species": "猫", "asset_kind": "animal"}],
        inferred_settings=[{"name": "仓库", "asset_kind": "building"}],
    )
    assert artifact["characters"][0]["asset_kind"] == "animal"
    assert artifact["settings"][0]["asset_kind"] == "building"


def test_item_asset_kind_compat_species():
    assert item_asset_kind({"asset_kind": "logo", "species": "产品"}, "characters") == "logo"
    assert item_asset_kind({"species": "产品"}, "characters") == "product"
    assert item_asset_kind({}, "settings") == "place"


def test_five_kind_generation_and_eval_matrix():
    cases = {
        "human": ("4 个视角", "4 个视角", "4个角色视图"),
        "animal": ("体态、毛色", "至少 3 个稳定视角", "资产类型：动物"),
        "product": ("结构、材质与配色", "至少 3 个稳定视角", "资产类型：产品"),
        "logo": ("轮廓、比例与文字", "单张正面/平铺", "资产类型：标识"),
        "building": ("立面、结构与空间关系", "至少 3 个稳定视角", "资产类型：建筑"),
    }
    for kind, (focus, view, eval_label) in cases.items():
        if kind == "building":
            prompt = CharacterDesignerAgent._setting_prompt("楼", "玻璃幕墙", "风格", kind)
        else:
            prompt = CharacterDesignerAgent._char_prompt("主体", "描述", "风格", "", kind)
        eval_prompt = CharacterDesignerAgent._eval_prompt_for("描述", "characters", "", kind)
        assert focus in prompt
        assert view in prompt
        assert eval_label in eval_prompt


def test_import_and_sequel_contract_cover_all_kinds():
    from core.script_import import build_import_artifact
    from prompts.loader import load_prompt

    artifact = build_import_artifact(
        "第1集\n这是一段用于导入契约的正文。",
        session_id="s-kinds",
        inferred_characters=[
            {"name": "小陈", "description": "银行职员", "asset_kind": "human"},
            {"name": "小猫", "description": "橘色短毛", "asset_kind": "animal"},
            {"name": "产品A", "description": "金属机身", "asset_kind": "product"},
            {"name": "Logo", "description": "圆形标识", "asset_kind": "logo"},
        ],
        inferred_settings=[
            {"name": "大楼", "description": "玻璃幕墙", "asset_kind": "building"},
            {"name": "会议室", "description": "会议室", "asset_kind": "place"},
        ],
    )
    assert {item["asset_kind"] for item in artifact["characters"]} == {
        "human", "animal", "product", "logo",
    }
    assert {item["asset_kind"] for item in artifact["settings"]} == {"building", "place"}

    for lang in ("zh", "en"):
        sequel = load_prompt("script", "meta_extract_sequel", lang)
        meta = load_prompt("script", "meta_extract", lang)
        assert "asset_kind" in sequel and "asset_kind" in meta
