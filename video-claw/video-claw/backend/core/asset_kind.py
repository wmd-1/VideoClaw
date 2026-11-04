"""资产类型枚举与默认值（C2/WF-10）。

`asset_kind` 是枚举（human/animal/product/logo/building/place/other），
`species` 保留为自由文本展示/兼容字段。映射不到枚举时归 `other`，不猜关键词。
旧资产缺字段时按容器取默认值：characters → human，settings → place。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

ASSET_KIND_HUMAN = "human"
ASSET_KIND_ANIMAL = "animal"
ASSET_KIND_PRODUCT = "product"
ASSET_KIND_LOGO = "logo"
ASSET_KIND_BUILDING = "building"
ASSET_KIND_PLACE = "place"
ASSET_KIND_OTHER = "other"

ASSET_KIND_VALUES = (
    ASSET_KIND_HUMAN, ASSET_KIND_ANIMAL, ASSET_KIND_PRODUCT,
    ASSET_KIND_LOGO, ASSET_KIND_BUILDING, ASSET_KIND_PLACE, ASSET_KIND_OTHER,
)

_ALIASES = {
    "human": ASSET_KIND_HUMAN, "person": ASSET_KIND_HUMAN, "people": ASSET_KIND_HUMAN,
    "人类": ASSET_KIND_HUMAN, "人物": ASSET_KIND_HUMAN, "角色": ASSET_KIND_HUMAN,
    "animal": ASSET_KIND_ANIMAL, "动物": ASSET_KIND_ANIMAL,
    "product": ASSET_KIND_PRODUCT, "产品": ASSET_KIND_PRODUCT, "商品": ASSET_KIND_PRODUCT,
    "logo": ASSET_KIND_LOGO, "标识": ASSET_KIND_LOGO, "标志": ASSET_KIND_LOGO,
    "building": ASSET_KIND_BUILDING, "建筑": ASSET_KIND_BUILDING, "楼宇": ASSET_KIND_BUILDING,
    "place": ASSET_KIND_PLACE, "场景": ASSET_KIND_PLACE, "地点": ASSET_KIND_PLACE, "场所": ASSET_KIND_PLACE,
    "other": ASSET_KIND_OTHER, "其他": ASSET_KIND_OTHER,
}

_LABELS = {
    ASSET_KIND_HUMAN: "人物", ASSET_KIND_ANIMAL: "动物", ASSET_KIND_PRODUCT: "产品",
    ASSET_KIND_LOGO: "标识", ASSET_KIND_BUILDING: "建筑", ASSET_KIND_PLACE: "场景",
    ASSET_KIND_OTHER: "其他",
}

_FOCUS = {
    ASSET_KIND_HUMAN: "身份、服饰、发型与体态",
    ASSET_KIND_ANIMAL: "体态、毛色与物种特征",
    ASSET_KIND_PRODUCT: "结构、材质与配色",
    ASSET_KIND_LOGO: "轮廓、比例与文字",
    ASSET_KIND_BUILDING: "立面、结构与空间关系",
    ASSET_KIND_PLACE: "空间布局、陈设与光线",
    ASSET_KIND_OTHER: "主体结构与关键特征",
}


def normalize_asset_kind(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in ASSET_KIND_VALUES:
        return text
    return _ALIASES.get(text)


def default_asset_kind(container: str) -> str:
    """旧资产缺字段时的容器默认：characters→human，settings→place。"""
    return ASSET_KIND_HUMAN if str(container) == "characters" else ASSET_KIND_PLACE


def asset_kind_for(value: Any, *, container: str) -> str:
    """空值按容器默认；显式但无法识别时归 other（不猜）。"""
    if not str(value or "").strip():
        return default_asset_kind(container)
    return normalize_asset_kind(value) or ASSET_KIND_OTHER


def asset_kind_label(kind: str) -> str:
    return _LABELS.get(str(kind), "其他")


def asset_kind_focus(kind: str) -> str:
    return _FOCUS.get(str(kind), _FOCUS[ASSET_KIND_OTHER])


def item_asset_kind(item: Dict[str, Any], container: str) -> str:
    """产物条目 → kind：显式 asset_kind 优先，其次兼容 species，再按容器默认。"""
    if not isinstance(item, dict):
        return default_asset_kind(container)
    return asset_kind_for(item.get("asset_kind") or item.get("species"), container=container)


__all__ = [
    "ASSET_KIND_VALUES",
    "ASSET_KIND_HUMAN", "ASSET_KIND_ANIMAL", "ASSET_KIND_PRODUCT",
    "ASSET_KIND_LOGO", "ASSET_KIND_BUILDING", "ASSET_KIND_PLACE", "ASSET_KIND_OTHER",
    "normalize_asset_kind", "default_asset_kind", "asset_kind_for",
    "asset_kind_label", "asset_kind_focus", "item_asset_kind",
]
