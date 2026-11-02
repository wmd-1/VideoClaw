"""有效提示词解析单点（Q1）。

纯函数：不读存储、不调模型，调用方用当前 artifacts 算好 `current_fingerprint`
与 `current_dimensions` 传入。优先级：人工正文 > 新鲜 H3 > description。

两类 stale 分流：
- 人工正文 stale 仍提交（用户明确意图），只标注原因；
- H3 正文 stale/unknown 不静默使用，回退 description 并给出结构化原因。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SOURCE_MANUAL = "manual"
SOURCE_H3 = "h3"
SOURCE_DESCRIPTION = "description"

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

STALE_REASONS = (
    "shot_changed",
    "duration_changed",
    "mode_changed",
    "references_changed",
    "style_changed",
    "facts_changed",
    "grounding_model_changed",
    "fingerprint_missing",
    "fingerprint_changed",
)

_DIMENSION_REASONS = {
    "shot_content": "shot_changed",
    "duration": "duration_changed",
    "mode": "mode_changed",
    "references": "references_changed",
    "style": "style_changed",
    "facts": "facts_changed",
    "grounding_model": "grounding_model_changed",
}


def _changed_reasons(stored: Optional[Dict[str, str]], current: Dict[str, str]) -> List[str]:
    if not isinstance(stored, dict) or not stored:
        return ["fingerprint_missing"]
    reasons = [
        reason for dimension, reason in _DIMENSION_REASONS.items()
        if str(stored.get(dimension) or "") != str(current.get(dimension) or "")
    ]
    return reasons or ["fingerprint_changed"]


def resolve_effective_prompt(
    segment: Optional[Dict[str, Any]],
    h3_item: Optional[Dict[str, Any]],
    video_item: Optional[Dict[str, Any]],
    *,
    current_fingerprint: str,
    current_dimensions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """返回 {text, source, freshness, stale_reasons}。"""
    segment = segment if isinstance(segment, dict) else {}
    h3_item = h3_item if isinstance(h3_item, dict) else {}
    video_item = video_item if isinstance(video_item, dict) else {}
    dimensions = current_dimensions if isinstance(current_dimensions, dict) else {}
    description = str(video_item.get("description") or segment.get("shot_content") or "").strip()

    manual = str(video_item.get("manual_prompt") or "").strip()
    if manual:
        stored = str(video_item.get("manual_prompt_fingerprint") or "").strip()
        if not stored:
            freshness, reasons = FRESHNESS_UNKNOWN, ["fingerprint_missing"]
        elif stored == current_fingerprint:
            freshness, reasons = FRESHNESS_FRESH, []
        else:
            freshness, reasons = FRESHNESS_STALE, ["fingerprint_changed"]
        return {"text": manual, "source": SOURCE_MANUAL, "freshness": freshness,
                "stale_reasons": reasons}

    h3_text = str(h3_item.get("rewritten_prompt") or "").strip()
    if h3_text and str(h3_item.get("status") or "") == "done":
        stored_fp = str(h3_item.get("input_fingerprint") or "").strip()
        if stored_fp and stored_fp == current_fingerprint:
            return {"text": h3_text, "source": SOURCE_H3, "freshness": FRESHNESS_FRESH,
                    "stale_reasons": []}
        reasons = _changed_reasons(h3_item.get("input_dimensions"), dimensions)
        freshness = FRESHNESS_UNKNOWN if not stored_fp else FRESHNESS_STALE
        return {"text": description, "source": SOURCE_DESCRIPTION, "freshness": freshness,
                "stale_reasons": reasons}

    return {"text": description, "source": SOURCE_DESCRIPTION, "freshness": FRESHNESS_UNKNOWN,
            "stale_reasons": ["fingerprint_missing"]}


__all__ = [
    "FRESHNESS_FRESH",
    "FRESHNESS_STALE",
    "FRESHNESS_UNKNOWN",
    "SOURCE_DESCRIPTION",
    "SOURCE_H3",
    "SOURCE_MANUAL",
    "STALE_REASONS",
    "resolve_effective_prompt",
]
