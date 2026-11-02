"""H3 模式的共享纯函数（供 H3 改写与视频阶段共用，避免循环导入）。

包含：
- `resolve_h3_mode` / `NEXT_FRAME_MODES`：模式解析；
- `resolve_grounding_model`：VLM 模型回退链（两侧必须拿到同一字符串）；
- `authority_facts`：本分镜权威事实（与提示词注入同一匹配）；
- `fingerprint_parts`：当前输入指纹与逐维度哈希（H3 生成与视频解析同源）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.prompt_fingerprint import build_payload, dimension_hashes, sha_of

NEXT_FRAME_MODES = ("start_end_frame", "FL2VA")


def resolve_h3_mode(video_generation_mode: str, has_first_frame: bool = False,
                    has_last_frame: bool = False, has_reference_set: bool = False) -> str:
    """mode resolver（纯函数，决策文档 §5.2 契约）。

    - reference 模式 / 有参考集 → Ref2VA
    - 首尾帧 → FL2VA；仅尾帧 → L2VA；仅首帧 → I2VA
    - 无图像输入 → T2VA
    """
    mode = str(video_generation_mode or "").strip().lower()
    if has_reference_set or mode == "reference":
        return "Ref2VA"
    if has_first_frame and has_last_frame:
        return "FL2VA"
    if has_last_frame:
        return "L2VA"
    if has_first_frame:
        return "I2VA"
    return "T2VA"


def resolve_grounding_model(input_data: Optional[Dict[str, Any]],
                            session_meta: Optional[Dict[str, Any]] = None) -> str:
    from config import Config

    data = input_data if isinstance(input_data, dict) else {}
    meta = session_meta if isinstance(session_meta, dict) else {}
    return (
        str(Config.H3_REWRITE_VLM_MODEL or "").strip()
        or str(data.get("vlm_model") or meta.get("vlm_model") or "").strip()
        or str(Config.VLM_MODEL or "").strip()
    )


def resolve_style_key(input_data: Optional[Dict[str, Any]],
                      session_meta: Optional[Dict[str, Any]] = None) -> str:
    """风格**原始键**（未转英文关键词）：两侧必须同源，默认空串。"""
    data = input_data if isinstance(input_data, dict) else {}
    meta = session_meta if isinstance(session_meta, dict) else {}
    return str(data.get("style") or meta.get("style") or "").strip()


def collect_segments(storyboard_artifact: Optional[Dict[str, Any]], model: str = "",
                     session_override: Any = None) -> List[Dict[str, Any]]:
    """storyboard → H3 分镜条目（id/描述/有效时长/角色/场景），按 id 排序。

    H3 改写与视频阶段算指纹必须共用此归一化，否则 `shot_content` 取法不同会恒 stale。
    注意：视频侧执行用的原始 segment 仍应保留自己的形状，不要用它替换。
    """
    from core.video_params import resolve_effective_duration

    artifact = storyboard_artifact if isinstance(storyboard_artifact, dict) else {}
    segments: List[Dict[str, Any]] = []
    episodes = artifact.get("episodes", [])
    if not isinstance(episodes, list):
        return segments
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        ep_n = ep.get("episode_number", 0)
        for s_i, seg in enumerate(ep.get("segments", []), 1):
            if not isinstance(seg, dict):
                continue
            seg_id = seg.get("segment_id") or f"seg_{ep_n:02d}_{s_i:02d}"
            shots = seg.get("shots", [])
            shot_content = "\n".join(
                str(sh.get("content") or sh.get("plot") or "").strip()
                for sh in shots if isinstance(sh, dict)
            ).strip()
            total_duration = seg.get("total_duration") or sum(
                sh.get("duration", 0) for sh in shots if isinstance(sh, dict)
            )
            duration, duration_adjustments = resolve_effective_duration(
                total_duration, session_override, model
            )
            location = str(seg.get("location") or seg.get("setting") or "").strip()
            segments.append({
                "id": seg_id,
                "name": f"第{ep_n}集-片段{s_i}",
                "index": s_i,
                "episode": ep_n,
                "shot_content": shot_content,
                "duration": duration,
                "duration_adjustments": duration_adjustments,
                "characters": [str(c) for c in (seg.get("characters") or []) if c],
                "location": location,
            })
    segments.sort(key=lambda item: item["id"])
    return segments


def setting_entry(location: str, settings: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按稳定 ID 或规范名查找场景条目；不做模糊匹配。"""
    target = str(location or "").strip()
    if not target:
        return None
    for item in settings or []:
        if not isinstance(item, dict):
            continue
        names = {
            str(item.get("id") or "").strip(),
            str(item.get("setting_id") or "").strip(),
            str(item.get("name") or "").strip(),
        }
        names.discard("")
        if target in names:
            return item
    return None


def setting_description(location: str, settings: Sequence[Dict[str, Any]]) -> str:
    entry = setting_entry(location, settings)
    return str(entry.get("description") or "").strip() if entry else ""


def authority_facts(segment: Optional[Dict[str, Any]],
                    character_artifact: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """本分镜的权威事实（与 `_authoritative_context` 同一匹配）。"""
    segment = segment if isinstance(segment, dict) else {}
    artifact = character_artifact if isinstance(character_artifact, dict) else {}
    characters = artifact.get("characters", [])
    characters = characters if isinstance(characters, list) else []
    settings = artifact.get("settings", [])
    settings = settings if isinstance(settings, list) else []

    facts: List[Dict[str, Any]] = []
    for name in segment.get("characters", []) or []:
        entry = next(
            (c for c in characters if isinstance(c, dict)
             and str(c.get("name") or c.get("id") or "").strip() == str(name).strip()),
            None,
        )
        if entry is None:
            # 与 `_authoritative_context` 一致：未匹配到资产也会输出一行名称
            facts.append({"kind": "character", "id": "", "name": str(name), "description": ""})
            continue
        facts.append({
            "kind": "character",
            "id": entry.get("id"),
            "name": entry.get("name") or name,
            "description": entry.get("description"),
            "species": entry.get("species"),
        })
    location = str(segment.get("location") or "").strip()
    if location:
        entry = setting_entry(location, settings)
        if entry is not None:
            facts.append({
                "kind": "setting",
                "id": entry.get("id") or entry.get("setting_id"),
                "name": entry.get("name") or location,
                "description": entry.get("description"),
                "location": location,
            })
    return facts


def next_in_order(ordered: Sequence[Dict[str, Any]], seg_id: str) -> Optional[Dict[str, Any]]:
    for index, seg in enumerate(ordered or []):
        if seg.get("id") == seg_id:
            return ordered[index + 1] if index + 1 < len(ordered) else None
    return None


def media_references(segment: Dict[str, Any], mode: str,
                     ordered_segments: Sequence[Dict[str, Any]],
                     scene_map: Dict[str, Dict], artifacts: Dict[str, Any]) -> List[Tuple[str, str, Any]]:
    """当前模式的参考集（与 H3 生成时同一取法与顺序）。"""
    references: List[Tuple[str, str, Any]] = []
    if mode == "Ref2VA":
        from core.consumption import reference_assets_for_segment

        for container, key, asset in reference_assets_for_segment(artifacts or {}, segment):
            references.append((container, key, asset.get("selected")))
        return references
    references.append(("reference_generation", "scenes",
                       (scene_map.get(segment.get("id")) or {}).get("selected")))
    if mode == "FL2VA":
        nxt = next_in_order(ordered_segments, segment.get("id"))
        if nxt is not None:
            references.append(("reference_generation", "scenes",
                               (scene_map.get(nxt.get("id")) or {}).get("selected")))
    return references


def fingerprint_parts(segment: Dict[str, Any], mode: str,
                      ordered_segments: Sequence[Dict[str, Any]],
                      scene_map: Optional[Dict[str, Dict]],
                      artifacts: Optional[Dict[str, Any]],
                      style: Any = "", grounding_model: str = "") -> Tuple[str, Dict[str, str]]:
    """(整体指纹, 逐维度哈希)。H3 生成与视频解析必须调用同一实现。"""
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    payload = build_payload(
        shot_content=segment.get("shot_content"),
        duration=segment.get("duration"),
        mode=mode,
        references=media_references(segment, mode, ordered_segments, scene_map or {}, artifacts),
        style=style,
        facts=authority_facts(segment, artifacts.get("character_design")),
        grounding_model=grounding_model,
    )
    return sha_of(payload), dimension_hashes(payload)


__all__ = [
    "NEXT_FRAME_MODES",
    "authority_facts",
    "collect_segments",
    "fingerprint_parts",
    "media_references",
    "next_in_order",
    "resolve_grounding_model",
    "resolve_h3_mode",
    "resolve_style_key",
    "setting_description",
    "setting_entry",
]
