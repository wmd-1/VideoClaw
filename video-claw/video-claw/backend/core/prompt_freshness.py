"""读时派生的“需重生成”标注（WF-04 / Q1 收尾）。

不落库、不打标：每次返回状态快照时，用**当前** artifacts + meta 重算指纹，
与条目里存的指纹/维度比对，给 H3 条目与视频片段注入：

    needs_regeneration: bool
    regeneration_reasons: list[str]

这样配置（style / vlm）变化、导入重跑、素材替换都能自动覆盖，也没有
“重跑后忘了清除标记”的粘滞问题。旧产物缺指纹 → 标 unknown（needs=True），
不当作 fresh。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.h3_modes import (
    collect_segments,
    current_fingerprint,
    resolve_grounding_model,
    resolve_style_key,
)
from core.prompt_source import changed_reasons

logger = logging.getLogger(__name__)


def _baseline(artifacts: Dict[str, Any], meta: Dict[str, Any]):
    """返回 (h3_segments, scene_map, style, grounding_model)；缺模型配置时返回 None。"""
    from core.agents.video_agent import VideoDirectorAgent

    try:
        _mode, video_model = VideoDirectorAgent._select_video_model({}, meta)
    except Exception as exc:  # noqa: BLE001 - 无法确定模型时不派生，避免误报
        logger.debug("needs_regeneration 跳过：会话缺少视频模型配置（%s）", exc)
        return None
    segments = collect_segments(
        artifacts.get("storyboard", {}), model=video_model,
        session_override=meta.get("video_duration"),
    )
    scene_map = {
        item.get("id"): item
        for item in (artifacts.get("reference_generation", {}) or {}).get("scenes", [])
        if isinstance(item, dict) and item.get("id")
    }
    return (
        segments,
        scene_map,
        resolve_style_key({}, meta),
        resolve_grounding_model({}, meta),
    )


def _annotate_item(item: Dict[str, Any], stored: str, dimensions: Any,
                   current: str, current_dims: Dict[str, str],
                   not_ready_reason: Optional[str] = None) -> None:
    if not_ready_reason:
        item["needs_regeneration"] = True
        item["regeneration_reasons"] = [not_ready_reason]
        return
    if not stored:
        item["needs_regeneration"] = True
        item["regeneration_reasons"] = ["fingerprint_missing"]
        return
    if stored == current:
        item["needs_regeneration"] = False
        item["regeneration_reasons"] = []
        return
    item["needs_regeneration"] = True
    item["regeneration_reasons"] = changed_reasons(dimensions, current_dims)


def annotate_regeneration(artifacts: Dict[str, Any], meta: Optional[Dict[str, Any]]) -> None:
    """就地给快照副本注入 needs_regeneration（仅处理 prompt_rewrite / video_generation）。"""
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    baseline = _baseline(artifacts, meta)
    if baseline is None:
        return
    segments, scene_map, style, grounding_model = baseline
    by_id = {item.get("id"): item for item in segments if item.get("id")}

    def current_for(seg_id: str):
        segment = by_id.get(seg_id) or {
            "id": seg_id, "shot_content": "", "duration": 0, "characters": [], "location": "",
        }
        _mode, fingerprint, dimensions = current_fingerprint(
            seg_id, meta.get("video_generation_mode") or "first_frame",
            segments, scene_map, artifacts,
            style=style, grounding_model=grounding_model,
        )
        return segment, fingerprint, dimensions

    items = (artifacts.get("prompt_rewrite", {}) or {}).get("items") or []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        _segment, fingerprint, dimensions = current_for(item["id"])
        not_ready = None
        if str(item.get("status") or "") != "done" or not str(item.get("rewritten_prompt") or "").strip():
            not_ready = "h3_not_ready"
        _annotate_item(item, str(item.get("input_fingerprint") or ""), item.get("input_dimensions"),
                       fingerprint, dimensions, not_ready)

    clips = (artifacts.get("video_generation", {}) or {}).get("clips") or []
    for clip in clips:
        if not isinstance(clip, dict) or not clip.get("id"):
            continue
        _segment, fingerprint, dimensions = current_for(clip["id"])
        # 语义（WF-04-A）：clip 的 needs_regeneration = “视频是否用了当前正文”；
        # 只看生成时指纹。人工正文的新鲜度由 prompt_source 单独表达（徽标）。
        stored = str(clip.get("resolved_fingerprint") or "")
        source_meta = clip.get("prompt_source") if isinstance(clip.get("prompt_source"), dict) else {}
        if not stored and str(source_meta.get("freshness") or "") in ("stale", "unknown") \
                and source_meta.get("stale_reasons"):
            # 旧产物无 resolved_fingerprint：沿用已算出的解析链原因，避免与徽标矛盾
            clip["needs_regeneration"] = True
            clip["regeneration_reasons"] = list(source_meta.get("stale_reasons") or [])
            continue
        _annotate_item(clip, stored, None, fingerprint, dimensions)


__all__ = ["annotate_regeneration"]
