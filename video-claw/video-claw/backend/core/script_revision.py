# -*- coding: utf-8 -*-
"""剧本修订的纯函数集合。

提供内容指纹、集变更比对、下游过期计算、角色/场景差异与预览合并等无副作用
工具，供 ScriptWriterAgent 与验证脚本共用。
"""

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

REVISION_HISTORY_LIMIT = 20


def _normalize_name(name: str) -> str:
    """名称归一化：去空格、去括号注释、转小写；用于跨阶段匹配。"""
    import re

    text = str(name or "").strip().lower()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


# 公开别名：跨阶段重建需要同一套名称归一化规则
normalize_name = _normalize_name


def _episode_map(episodes: Any) -> Dict[int, dict]:
    result: Dict[int, dict] = {}
    if not isinstance(episodes, list):
        return result
    for index, episode in enumerate(episodes, 1):
        if not isinstance(episode, dict):
            continue
        raw = episode.get("episode_number", episode.get("act_number", index))
        try:
            number = int(raw)
        except (TypeError, ValueError):
            number = index
        result[number] = episode
    return result


def content_fingerprint(artifact: Dict[str, Any]) -> str:
    """对 episodes/characters/settings 做确定性指纹；忽略预览与历史字段。"""
    payload = {
        "episodes": artifact.get("episodes") or [],
        "characters": artifact.get("characters") or [],
        "settings": artifact.get("settings") or [],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def diff_episode_numbers(old_episodes: Any, new_episodes: Any) -> List[int]:
    """返回正文发生变化的集号（含新增/缺失集）。"""
    old_map = _episode_map(old_episodes)
    new_map = _episode_map(new_episodes)
    changed = set(old_map) ^ set(new_map)
    for number in set(old_map) & set(new_map):
        if str(old_map[number].get("content", "")) != str(
            new_map[number].get("content", "")
        ):
            changed.add(number)
    return sorted(changed)


def stale_storyboards(diff_numbers: Iterable[int], storyboard_artifact: Any) -> List[int]:
    """已有分镜（episodes[].segments 非空）且正文被改动的集号。"""
    if not isinstance(storyboard_artifact, dict):
        return []
    ready = set()
    for episode in storyboard_artifact.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        if episode.get("segments"):
            try:
                ready.add(int(episode.get("episode_number")))
            except (TypeError, ValueError):
                continue
    return sorted(set(diff_numbers) & ready)


def _items_by_name(items: Any, name_key: str = "name") -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _normalize_name(item.get(name_key))
        if key and key not in result:
            result[key] = item
    return result


def removed_items(
    old_items: Any,
    new_items: Any,
    stage_items: Any = None,
    id_key: str = "id",
) -> List[dict]:
    """新集合中已不存在的旧条目，附带阶段 2 已生成资产数量。"""
    new_names = set(_items_by_name(new_items))
    stage_list = stage_items if isinstance(stage_items, list) else []
    stage_by_name = _items_by_name(stage_list)
    removed: List[dict] = []
    for item in old_items or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        key = _normalize_name(name)
        if not key or key in new_names:
            continue
        stage_item = stage_by_name.get(key) or {}
        item_id = (
            item.get(id_key)
            or item.get("character_id")
            or item.get("setting_id")
            or stage_item.get("id")
            or ""
        )
        versions = stage_item.get("versions") if isinstance(stage_item, dict) else []
        removed.append(
            {
                "name": name,
                "id": item_id,
                "asset_count": len(versions) if isinstance(versions, list) else 0,
            }
        )
    return removed


def user_edited_items(
    old_items: Any,
    new_items: Any,
    stage_items: Any = None,
) -> List[dict]:
    """阶段 2 描述与旧剧本不同（视为用户手改），且新剧本仍保留的同名条目。"""
    old_by_name = _items_by_name(old_items)
    new_by_name = _items_by_name(new_items)
    stage_by_name = _items_by_name(stage_items)
    edited: List[dict] = []
    for key, stage_item in stage_by_name.items():
        if key not in old_by_name or key not in new_by_name:
            continue
        old_desc = str(old_by_name[key].get("description", "")).strip()
        stage_desc = str(stage_item.get("description", "")).strip()
        if stage_desc and stage_desc != old_desc:
            edited.append(
                {
                    "name": stage_item.get("name", ""),
                    "id": stage_item.get("id", ""),
                    "current_description": stage_desc,
                    "incoming_description": str(
                        new_by_name[key].get("description", "")
                    ),
                }
            )
    return edited


def duplicate_names(items: Any) -> List[str]:
    """同一集合内归一化名称重复的原始名称。"""
    seen: Dict[str, int] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = _normalize_name(item.get("name"))
        if key:
            seen[key] = seen.get(key, 0) + 1
    return sorted(
        {
            item.get("name", "")
            for item in items or []
            if isinstance(item, dict)
            and seen.get(_normalize_name(item.get("name")), 0) > 1
        }
    )


def build_episode_brief(episodes: Any, exclude_number: int, limit: int = 120) -> str:
    """其他集概要：只传集号、标题与正文前若干字，避免暴露全文。"""
    lines: List[str] = []
    for episode in episodes or []:
        if not isinstance(episode, dict):
            continue
        number = episode.get("episode_number", episode.get("act_number"))
        try:
            if int(number) == int(exclude_number):
                continue
        except (TypeError, ValueError):
            pass
        content = str(episode.get("content", "")).replace("\n", " ").strip()
        lines.append(
            f"第{number}集《{episode.get('act_title', '')}》：{content[:limit]}"
        )
    return "\n".join(lines) or "（无其他集）"


def build_target_episode_block(episode: Dict[str, Any]) -> str:
    number = episode.get("episode_number", episode.get("act_number", ""))
    return (
        f"集号：{number}\n"
        f"标题：{episode.get('act_title', '')}\n"
        f"正文：\n{episode.get('content', '')}"
    )


def validate_episode_numbers(
    old_episodes: Any, new_episodes: Any
) -> List[dict]:
    """校验全剧修订集号集合一致，返回按集号排序的新集列表。"""
    old_map = _episode_map(old_episodes)
    new_map = _episode_map(new_episodes)
    if not new_map:
        raise ValueError("修订结果不包含任何剧集")
    if set(old_map) != set(new_map):
        raise ValueError(
            "修订结果集号与原文不一致："
            f"期望 {sorted(old_map)}，实际 {sorted(new_map)}"
        )
    return [new_map[number] for number in sorted(new_map)]


def merge_single_episode(old_episodes: Any, new_episode: Any) -> List[dict]:
    """把单集修订结果合并回完整集列表；未选中集保持逐字不变。"""
    if not isinstance(new_episode, dict):
        raise ValueError("单集修订结果为空")
    target_raw = new_episode.get("episode_number", new_episode.get("act_number"))
    try:
        target = int(target_raw)
    except (TypeError, ValueError):
        raise ValueError("单集修订结果缺少合法集号")
    merged: List[dict] = []
    replaced = False
    for episode in old_episodes or []:
        if not isinstance(episode, dict):
            continue
        current = copy.deepcopy(episode)
        number = episode.get("episode_number", episode.get("act_number"))
        try:
            is_target = int(number) == target
        except (TypeError, ValueError):
            is_target = False
        if is_target:
            current["episode_number"] = target
            if "act_title" in new_episode or "act_title" in current:
                current["act_title"] = new_episode.get(
                    "act_title", current.get("act_title", "")
                )
            current["content"] = str(new_episode.get("content", ""))
            replaced = True
        merged.append(current)
    if not replaced:
        raise ValueError(f"单集修订目标集号不存在：{target}")
    return merged


def assert_untouched_episodes(
    old_episodes: Any, merged_episodes: Any, target_number: int
) -> None:
    """单集修订后，未选中集必须与修订前逐字一致。"""
    old_map = _episode_map(old_episodes)
    new_map = _episode_map(merged_episodes)
    for number, old in old_map.items():
        if number == int(target_number):
            continue
        new = new_map.get(number)
        if new is None or str(new.get("content", "")) != str(old.get("content", "")):
            raise ValueError(f"单集修订越界改动了第 {number} 集")


def apply_preview_to_artifact(
    current_artifact: Dict[str, Any],
    preview: Dict[str, Any],
    instruction: str,
    scope: str,
    episode_number: Optional[int],
) -> Dict[str, Any]:
    """把预览写入产物：更新正文/角色/场景，追加历史并清除派生标注。"""
    updated = copy.deepcopy(current_artifact)

    old_episodes = updated.get("episodes") or []
    if scope == "episode":
        updated["episodes"] = merge_single_episode(
            old_episodes, preview.get("episode") or {}
        )
    else:
        updated["episodes"] = validate_episode_numbers(
            old_episodes, preview.get("episodes") or []
        )

    if isinstance(preview.get("characters"), list):
        updated["characters"] = copy.deepcopy(preview["characters"])
    if isinstance(preview.get("settings"), list):
        updated["settings"] = copy.deepcopy(preview["settings"])

    history = updated.get("revision_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "instruction": instruction,
            "scope": scope,
            "episode_number": episode_number,
            "summary": preview.get("summary", ""),
            "stale_storyboards": list(preview.get("stale_storyboards") or []),
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "base_version": preview.get("base_version", ""),
        }
    )
    updated["revision_history"] = history[-REVISION_HISTORY_LIMIT:]

    updated.pop("pending_revision", None)
    # 导入路径产生的派生标注与全局行号绑定，正文变化后必然失效
    updated.pop("line_annotations", None)
    updated.pop("scene_spans", None)
    for episode in updated.get("episodes") or []:
        if isinstance(episode, dict):
            episode.pop("line_start", None)
            episode.pop("line_end", None)
    return updated
