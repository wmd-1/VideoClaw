"""H3 提示词改写产物完整性判据（/continue 与前端共用同一业务语义）。"""
from __future__ import annotations

from typing import Any, Dict, List


def expected_segment_ids(storyboard: Any) -> List[str]:
    if not isinstance(storyboard, dict):
        return []
    episodes = storyboard.get("episodes")
    if not isinstance(episodes, list):
        return []
    ids: List[str] = []
    seen = set()
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        segments = episode.get("segments")
        if not isinstance(segments, list):
            continue
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("segment_id") or "").strip()
            if segment_id and segment_id not in seen:
                seen.add(segment_id)
                ids.append(segment_id)
    return ids


def assess_prompt_rewrite(
    storyboard: Any,
    rewrite: Any,
    *,
    storyboard_reconfirm: bool = False,
) -> Dict[str, Any]:
    expected = expected_segment_ids(storyboard)
    raw_items = rewrite.get("items") if isinstance(rewrite, dict) else None
    items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    by_id = {
        str(item.get("id") or "").strip(): item
        for item in items
        if str(item.get("id") or "").strip()
    }
    missing = [segment_id for segment_id in expected if segment_id not in by_id]
    incomplete = [
        segment_id for segment_id in expected
        if segment_id in by_id and by_id[segment_id].get("status") not in {"done", "failed"}
    ]
    failed = [
        segment_id for segment_id in expected
        if segment_id in by_id and by_id[segment_id].get("status") == "failed"
    ]
    meaningful = bool(expected) and any(
        segment_id in by_id and (
            by_id[segment_id].get("status") in {"done", "failed"}
            or str(by_id[segment_id].get("rewritten_prompt") or "").strip()
        )
        for segment_id in expected
    )
    can_continue = meaningful and not missing and not incomplete and not storyboard_reconfirm
    return {
        "expected_ids": expected,
        "item_ids": list(by_id),
        "hasMeaningfulArtifact": meaningful,
        "hasMissingItems": bool(missing),
        "hasIncompleteItems": bool(incomplete),
        "hasFailedItems": bool(failed),
        "missing_ids": missing,
        "incomplete_ids": incomplete,
        "failed_ids": failed,
        "storyboardReconfirm": bool(storyboard_reconfirm),
        "canContinue": can_continue,
    }
