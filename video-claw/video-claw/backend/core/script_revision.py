# -*- coding: utf-8 -*-
"""剧本修订的纯函数集合。

提供内容指纹、集变更比对、下游过期计算、角色/场景差异与预览合并等无副作用
工具，供 ScriptWriterAgent 与验证脚本共用。
"""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.script_errors import ScriptFieldNotWritableError

REVISION_HISTORY_LIMIT = 20

SCRIPT_WRITABLE_FIELDS = ("episodes", "characters", "settings", "title", "logline")
SCRIPT_PREVIEW_FIELDS = ("pending_revision", "pending_review")
_EPISODE_WRITABLE_FIELDS = ("episode_number", "act_title", "content")
_CHARACTER_WRITABLE_FIELDS = ("name", "description", "species")
_SETTING_WRITABLE_FIELDS = ("name", "description", "mood")


def _normalize_name(name: str) -> str:
    """名称归一化：去空格、去括号注释、转小写；用于跨阶段匹配。"""
    import re

    text = str(name or "").strip().lower()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"\s+", "", text)
    return text


# 公开别名：跨阶段重建需要同一套名称归一化规则
normalize_name = _normalize_name


def _item_match_key(item: Any, id_key: str) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get(id_key) or item.get("id") or "").strip()


def _find_script_item(
    current_items: Any,
    incoming: Dict[str, Any],
    *,
    id_key: str,
) -> Optional[dict]:
    """按服务端主键优先、名称其次匹配；客户端主键只作匹配线索。"""
    items = [item for item in (current_items or []) if isinstance(item, dict)]
    incoming_id = _item_match_key(incoming, id_key)
    if incoming_id:
        for item in items:
            if _item_match_key(item, id_key) == incoming_id:
                return item
    name_key = _normalize_name(incoming.get("name"))
    if name_key:
        for item in items:
            if _normalize_name(item.get("name")) == name_key:
                return item
    return None


def resolve_description_owner(item: Any, *, default: str = "model") -> str:
    """统一 provenance 解析：显式 owner 优先，其次导入来源，缺省按 model。"""
    if isinstance(item, dict):
        owner = item.get("description_owner")
        if owner in ("user", "model"):
            return owner
        if item.get("description_source") == "uploaded":
            return "user"
    return default if default in ("user", "model") else "model"


def normalize_cast_owners(cast_payload: Dict[str, Any], *, default_owner: str = "model") -> Dict[str, Any]:
    """就地补齐 cast provenance 的缺省值，不覆盖显式 user/model。"""
    normalized = copy.deepcopy(cast_payload) if isinstance(cast_payload, dict) else {}
    for container in ("characters", "settings"):
        items = normalized.get(container)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                item["description_owner"] = resolve_description_owner(item, default=default_owner)
    return normalized


def merge_model_cast_provenance(
    current_items: Any,
    incoming_items: Any,
    *,
    container: str,
    id_key: str,
    prefix: str,
) -> List[dict]:
    """AI cast 替换：保留主键/资产字段，描述变化且经用户确认后归 model。"""
    current = [item for item in (current_items or []) if isinstance(item, dict)]
    incoming = incoming_items if isinstance(incoming_items, list) else []
    allowed = _MODEL_CAST_FIELDS[container]
    merged: List[dict] = []
    for candidate in incoming:
        if not isinstance(candidate, dict):
            continue
        existing = _find_script_item(current, candidate, id_key=id_key)
        if existing is None:
            item = {key: copy.deepcopy(candidate[key]) for key in allowed if key in candidate}
            item["name"] = str(candidate.get("name") or "").strip()
            item["description_owner"] = "model"
        else:
            item = copy.deepcopy(existing)
            for key in allowed:
                if key in candidate:
                    item[key] = copy.deepcopy(candidate[key])
            if str(item.get("description") or "") != str(existing.get("description") or ""):
                item["description_owner"] = "model"
            else:
                item["description_owner"] = resolve_description_owner(existing)
        merged.append(item)
    return ensure_item_ids(current, merged, id_key, prefix)


def _stamp_description_owner(existing: Optional[dict], cleaned: dict) -> None:
    """描述所有权由后端 diff 计算，客户端值绝不作为权威。"""
    if existing is None:
        cleaned["description_owner"] = "user"
        return
    old_description = str(existing.get("description") or "")
    new_description = str(cleaned.get("description") or "")
    if new_description != old_description:
        cleaned["description_owner"] = "user"
        return
    cleaned["description_owner"] = resolve_description_owner(existing)


def sanitize_script_patch(
    current: Dict[str, Any],
    body: Dict[str, Any],
    *,
    reject_unknown: bool = True,
) -> Tuple[Dict[str, Any], List[str]]:
    """统一清洗剧本 PATCH/完整替换载荷，返回 (可写载荷, 被忽略字段)。

    PATCH 默认拒绝未知顶层字段；``modified_script`` 传 ``reject_unknown=False``，
    只提取白名单并回报 ``ignored_fields``。嵌套未知字段始终丢弃，已有条目的
    服务端派生字段保留，新条目主键由 ``ensure_item_ids`` 同源派生。
    """
    if not isinstance(body, dict):
        raise ValueError("剧本写入载荷必须是对象")
    current = current if isinstance(current, dict) else {}
    ignored: List[str] = []
    rejected = set(body) - set(SCRIPT_WRITABLE_FIELDS) - set(SCRIPT_PREVIEW_FIELDS)
    for key in SCRIPT_PREVIEW_FIELDS:
        if key in body and body[key] is not None:
            rejected.add(key)
    if reject_unknown and rejected:
        raise ScriptFieldNotWritableError(rejected)
    for key in sorted(rejected):
        ignored.append(key)

    cleaned: Dict[str, Any] = {}
    for key in ("title", "logline"):
        if key in body:
            cleaned[key] = copy.deepcopy(body[key])
    for key in SCRIPT_PREVIEW_FIELDS:
        if key in body and body[key] is None:
            cleaned[key] = None

    if "episodes" in body:
        incoming_episodes = body.get("episodes")
        if incoming_episodes is None:
            incoming_episodes = []
        if not isinstance(incoming_episodes, list):
            raise ValueError("episodes 必须是数组")
        current_episodes = current.get("episodes") or []
        current_by_number = _episode_map(current_episodes)
        episodes: List[dict] = []
        for index, incoming in enumerate(incoming_episodes, 1):
            if not isinstance(incoming, dict):
                ignored.append(f"episodes[{index - 1}]")
                continue
            raw_number = incoming.get("episode_number", index)
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                number = index
            existing = current_by_number.get(number)
            episode = copy.deepcopy(existing) if existing is not None else {}
            for key, value in incoming.items():
                if key in _EPISODE_WRITABLE_FIELDS:
                    episode[key] = copy.deepcopy(value)
                else:
                    ignored.append(f"episodes[{index - 1}].{key}")
            episodes.append(episode)
        cleaned["episodes"] = episodes

    for container, id_key, prefix, writable in (
        ("characters", "character_id", "char", _CHARACTER_WRITABLE_FIELDS),
        ("settings", "setting_id", "set", _SETTING_WRITABLE_FIELDS),
    ):
        if container not in body:
            continue
        incoming_items = body.get(container)
        if incoming_items is None:
            incoming_items = []
        if not isinstance(incoming_items, list):
            raise ValueError(f"{container} 必须是数组")
        current_items = current.get(container) or []
        items: List[dict] = []
        for index, incoming in enumerate(incoming_items):
            if not isinstance(incoming, dict):
                ignored.append(f"{container}[{index}]")
                continue
            existing = _find_script_item(current_items, incoming, id_key=id_key)
            item = copy.deepcopy(existing) if existing is not None else {}
            if existing is None:
                item.pop(id_key, None)
                item.pop("id", None)
            for key, value in incoming.items():
                if key in writable:
                    item[key] = copy.deepcopy(value)
                else:
                    ignored.append(f"{container}[{index}].{key}")
            _stamp_description_owner(existing, item)
            items.append(item)
        cleaned[container] = ensure_item_ids(current_items, items, id_key, prefix)

    return cleaned, ignored


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
        "characters": [
            {key: value for key, value in item.items() if key != "description_owner"}
            for item in (artifact.get("characters") or [])
            if isinstance(item, dict)
        ],
        "settings": [
            {key: value for key, value in item.items() if key != "description_owner"}
            for item in (artifact.get("settings") or [])
            if isinstance(item, dict)
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def structure_fingerprint(artifact: Dict[str, Any], *, scope: str = "all") -> str:
    """结构指纹：只覆盖用户可编辑的内容，剔除服务端派生字段。

    `content_fingerprint` 的语义保持不变（修订预览与 `pending_revision` 失效的口径），
    但它把 `episodes`/`characters`/`settings` 整份序列化：服务端会往这些条目里回写
    `character_id`/`setting_id`/`asset_kind` 与 `line_start`/`line_end`，于是“回填主键后
    原样再保存”也会被判定为内容变化。下游同步与待重新确认标记只关心“用户改了什么”，
    因此另用本函数。

    scope: `all`=三者合计；`cast`=角色/场景集合；`body`=分集正文与集号集合。
    传其他值直接报错，避免“静默哈希空内容”让所有保存都被判为变更或都不变更。
    """
    if scope not in ("all", "cast", "body"):
        raise ValueError(f"unknown structure_fingerprint scope: {scope!r}")
    item_dropped = {"character_id", "setting_id", "asset_kind", "description_owner"}
    episode_dropped = {"line_start", "line_end"}

    def _clean(items: Any, dropped: Iterable[str]) -> List[dict]:
        cleaned: List[dict] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            cleaned.append({key: value for key, value in item.items() if key not in dropped})
        return cleaned

    scopes: Dict[str, Any] = {}
    if scope in ("all", "body"):
        scopes["episodes"] = _clean(artifact.get("episodes"), episode_dropped)
    if scope in ("all", "cast"):
        scopes["characters"] = _clean(artifact.get("characters"), item_dropped)
        scopes["settings"] = _clean(artifact.get("settings"), item_dropped)
    encoded = json.dumps(scopes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def structure_snapshot(artifact: Any) -> Dict[str, Any]:
    """变更前快照：只留结构指纹覆盖的字段，供事后比对与集级差异计算。

    调用方必须在产物被就地改写**之前**取本快照（`_apply_artifact_update` 是就地合并）。
    """
    if not isinstance(artifact, dict):
        return {}
    return copy.deepcopy(
        {
            "episodes": artifact.get("episodes") or [],
            "characters": artifact.get("characters") or [],
            "settings": artifact.get("settings") or [],
        }
    )


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


def item_id(item: Any, id_key: str, prefix: str) -> str:
    """条目主键：产物自带 ID 优先，缺失时按名称派生稳定 ID。

    剧本产物丢 ID（旧版全剧修订直接采用模型返回的角色/场景对象）时，下游“按 ID 取数”
    会整段丢条目，阶段 2 报“未能从剧本中读取到角色或场景描述数据”。派生口径与导入路径
    `script_import.stable_id` 一致：导入剧本可原地找回原 ID 与已生成的资产。
    """
    if not isinstance(item, dict):
        return ""
    existing = item.get(id_key) or item.get("id")
    if existing:
        return str(existing)
    name = str(item.get("name") or "").strip()
    if not name:
        return ""
    from core import script_import

    return script_import.stable_id(prefix, name)


def ensure_item_ids(old_items: Any, new_items: Any, id_key: str, prefix: str) -> List[dict]:
    """就地为缺 ID 的条目补主键：同名先复用修订前条目的 ID，再按名称派生。

    必须在预览计算之前调用：复用旧 ID 才能保住阶段 2 的选中/版本/资产，
    而新 ID 一旦落入产物就要与下游（种子化、取数）的派生口径一致。
    模型偶尔给多个条目返回同一主键：保留首个，其余按名称重新分配。
    """
    from core import script_import

    items = [item for item in (new_items or []) if isinstance(item, dict)]
    incoming_ids: set = set()
    for item in items:
        current = item.get(id_key) or item.get("id")
        if not current:
            continue
        if str(current) in incoming_ids:
            item.pop(id_key, None)
            item.pop("id", None)
            continue
        incoming_ids.add(str(current))
    taken = {
        str(item.get(id_key) or item.get("id"))
        for item in items
        if item.get(id_key) or item.get("id")
    }
    reusable: Dict[str, List[str]] = {}
    for old in old_items or []:
        if not isinstance(old, dict):
            continue
        old_id = old.get(id_key) or old.get("id")
        key = _normalize_name(old.get("name"))
        if old_id and key:
            reusable.setdefault(key, []).append(str(old_id))

    for item in items:
        if item.get(id_key) or item.get("id"):
            continue
        name = str(item.get("name") or "").strip()
        candidate = next(
            (cid for cid in reusable.get(_normalize_name(name), []) if cid not in taken), ""
        )
        if not candidate:
            candidate = (
                script_import.stable_id(prefix, name)
                if name
                else f"{prefix}_{uuid.uuid4().hex[:6]}"
            )
            if candidate in taken:
                # 同名多条目：派生 ID 会碰撞，退回随机 ID 保证主键唯一
                candidate = f"{prefix}_{uuid.uuid4().hex[:6]}"
        taken.add(candidate)
        item[id_key] = candidate
    return items


_INTERNAL_CAST_FIELDS = {
    "characters": {
        "character_id", "id", "name", "description", "species", "description_source",
        "description_owner", "asset_kind", "selected", "versions", "status",
    },
    "settings": {
        "setting_id", "id", "name", "description", "mood", "description_source",
        "description_owner", "asset_kind", "aliases", "selected", "versions", "status",
    },
}
_MODEL_CAST_FIELDS = {
    "characters": {"name", "description", "species", "asset_kind"},
    "settings": {"name", "description", "mood", "asset_kind", "aliases"},
}


def sanitize_internal_cast_payload(cast_payload: Dict[str, Any]) -> Dict[str, Any]:
    """内部 cast 写回的防御性白名单；拒绝未知嵌套键和非法 owner。"""
    if not isinstance(cast_payload, dict):
        raise ValueError("cast 写回载荷必须是对象")
    rejected = set(cast_payload) - set(_INTERNAL_CAST_FIELDS)
    if rejected:
        raise ScriptFieldNotWritableError(rejected)
    cleaned: Dict[str, Any] = {}
    for container, allowed in _INTERNAL_CAST_FIELDS.items():
        if container not in cast_payload:
            continue
        items = cast_payload[container]
        if not isinstance(items, list):
            raise ValueError(f"{container} 必须是数组")
        output = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{container}[{index}] 必须是对象")
            unknown = set(item) - allowed
            if unknown:
                raise ScriptFieldNotWritableError(
                    f"{container}[{index}].{key}" for key in unknown
                )
            owner = item.get("description_owner")
            if owner is not None and owner not in ("user", "model"):
                raise ValueError(f"{container}[{index}].description_owner 非法")
            output.append(copy.deepcopy(item))
        cleaned[container] = output
    return cleaned


def normalize_cast_candidates(cast_payload: Dict[str, Any]) -> Dict[str, Any]:
    """模型候选只保留业务字段，主键/资产/状态由服务端合并时产生。"""
    if not isinstance(cast_payload, dict):
        raise ValueError("cast 候选必须是对象")
    cleaned: Dict[str, Any] = {}
    for container, allowed in _MODEL_CAST_FIELDS.items():
        items = cast_payload.get(container) or []
        if not isinstance(items, list):
            raise ValueError(f"{container} 候选必须是数组")
        output = []
        seen_names = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{container}[{index}] 候选必须是对象")
            name = str(item.get("name") or "").strip()
            if not name:
                raise ValueError(f"{container}[{index}] 缺少 name")
            name_key = _normalize_name(name)
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
            record = {key: copy.deepcopy(value) for key, value in item.items() if key in allowed}
            record["name"] = name
            output.append(record)
        cleaned[container] = output
    return cleaned


def cast_suggestion_id(item: Dict[str, Any]) -> str:
    """为建议生成稳定 ID；目标资产主键单独放 item_id，避免与建议 ID 混淆。"""
    basis = {
        "kind": item.get("kind"),
        "container": item.get("container") or item.get("kind"),
        "item_id": item.get("item_id"),
        "name": item.get("name"),
        "from": item.get("from"),
        "to": item.get("to"),
        "reason": item.get("reason"),
    }
    encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cast_sug_" + hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:12]


def merge_cast_reparse_candidates(
    current_script: Dict[str, Any],
    candidates: Dict[str, Any],
) -> Dict[str, Any]:
    """安全合并重解析候选：保护 user-owner，新增自动追加，删除/改名只建议。"""
    candidates = normalize_cast_candidates(candidates)
    script = copy.deepcopy(current_script) if isinstance(current_script, dict) else {}
    full_text = "\n".join(
        str(episode.get("content") or "")
        for episode in script.get("episodes") or []
        if isinstance(episode, dict)
    )
    full_text_key = _normalize_name(full_text)
    suggestions: List[dict] = []
    remove_candidates: List[dict] = []
    summary = {"added": 0, "updated": 0, "protected": 0, "remove_candidates": 0}

    for container, id_key, prefix in (
        ("characters", "character_id", "char"),
        ("settings", "setting_id", "set"),
    ):
        existing_items = [item for item in (script.get(container) or []) if isinstance(item, dict)]
        existing_by_name = _items_by_name(existing_items)
        matched_existing: Set[int] = set()
        added_names: List[dict] = []
        removed_names: List[dict] = []
        merged: List[dict] = []
        for candidate in candidates.get(container) or []:
            name_key = _normalize_name(candidate.get("name"))
            existing = existing_by_name.get(name_key)
            if existing is None:
                added_names.append({"kind": container, **copy.deepcopy(candidate)})
                continue
            matched_existing.add(id(existing))
            item = copy.deepcopy(existing)
            description_provided = "description" in candidate
            description = str(candidate.get("description") or "") if description_provided else str(existing.get("description") or "")
            old_description = str(existing.get("description") or "")
            owner = resolve_description_owner(existing)
            item["description_owner"] = owner
            if owner == "user" and description_provided and description != old_description:
                suggestion = {
                    "kind": container,
                    "container": container,
                    "item_id": existing.get(id_key) or existing.get("id") or "",
                    "name": candidate.get("name"),
                    "description": description,
                    "reason": "user_owned",
                }
                suggestion["id"] = cast_suggestion_id(suggestion)
                suggestions.append(suggestion)
                summary["protected"] += 1
                merged.append(item)
                continue
            changed = False
            for key in _MODEL_CAST_FIELDS[container]:
                if key in candidate and item.get(key) != candidate[key]:
                    item[key] = copy.deepcopy(candidate[key])
                    changed = True
            if description_provided and description != old_description:
                item["description_owner"] = "model"
            else:
                item["description_owner"] = owner
            if changed or (description_provided and description != old_description):
                summary["updated"] += 1
            merged.append(item)

        for existing in existing_items:
            name_key = _normalize_name(existing.get("name"))
            if id(existing) in matched_existing:
                continue
            kept = copy.deepcopy(existing)
            kept["description_owner"] = resolve_description_owner(existing)
            merged.append(kept)
            if name_key and name_key not in full_text_key:
                removed = {
                    "kind": container,
                    "container": container,
                    "item_id": existing.get(id_key) or existing.get("id") or "",
                    "name": existing.get("name") or "",
                    "reason": "removed_from_body",
                }
                removed["id"] = cast_suggestion_id(removed)
                remove_candidates.append(removed)
                removed_names.append(removed)

        # 一删一新增且旧名已从全文消失时，只提示疑似改名，不自动建立新主键。
        if len(added_names) == 1 and len(removed_names) == 1:
            added = added_names[0]
            removed = removed_names[0]
            suggestion = {
                "kind": "rename",
                "container": container,
                "item_id": removed.get("item_id") or "",
                "from": removed.get("name"),
                "to": added.get("name"),
                "reason": "rename",
            }
            suggestion["id"] = cast_suggestion_id(suggestion)
            suggestions.append(suggestion)
            # 改名候选已解释这次“消失+新增”，不再同时建议删除旧主键。
            remove_candidates = [item for item in remove_candidates if item is not removed]
            added_names = []

        merged = ensure_item_ids(existing_items, merged, id_key, prefix)
        script[container] = merged
        for added in added_names:
            item = {key: copy.deepcopy(value) for key, value in added.items() if key != "kind"}
            item["description_owner"] = "model"
            item["selected"] = ""
            item["versions"] = []
            item["status"] = "pending"
            script[container] = ensure_item_ids(
                script.get(container) or [], [*script.get(container), item], id_key, prefix
            )
            summary["added"] += 1

    summary["remove_candidates"] = len(remove_candidates)
    summary["suggested"] = len(suggestions)
    return {
        "characters": script.get("characters") or [],
        "settings": script.get("settings") or [],
        "suggestions": suggestions,
        "remove_candidates": remove_candidates,
        "summary": summary,
    }


def storyboard_coverage(
    script_artifact: Dict[str, Any], storyboard_artifact: Dict[str, Any]
) -> Dict[str, Any]:
    """分镜确认的统一纯判据：覆盖剧本全集且每集 segments 非空。"""
    script_number_set = set()
    for episode in (script_artifact.get("episodes") or []):
        if not isinstance(episode, dict):
            continue
        raw = episode.get("episode_number", episode.get("act_number"))
        if str(raw).isdigit():
            script_number_set.add(int(raw))
    script_numbers = sorted(script_number_set)
    storyboard_by_number: Dict[int, dict] = {}
    for episode in (storyboard_artifact.get("episodes") or []):
        if not isinstance(episode, dict):
            continue
        raw = episode.get("episode_number", episode.get("act_number"))
        if not str(raw).isdigit():
            continue
        storyboard_by_number[int(raw)] = episode
    missing = [number for number in script_numbers if number not in storyboard_by_number]
    empty = [
        number for number in script_numbers
        if number in storyboard_by_number and not storyboard_by_number[number].get("segments")
    ]
    return {
        "complete": bool(script_numbers) and not missing and not empty,
        "script_episodes": script_numbers,
        "storyboard_episodes": sorted(storyboard_by_number),
        "missing_episodes": missing,
        "empty_episodes": sorted(set(empty)),
    }


def cast_diff_summary(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """生成 cast 快照与当前清单的稳定差异摘要，供质检撤销回报。"""
    result: Dict[str, Any] = {}
    for container, id_key in (("characters", "character_id"), ("settings", "setting_id")):
        old_by_name = _items_by_name(before.get(container))
        new_by_name = _items_by_name(after.get(container))
        added = [
            {"id": item.get(id_key) or item.get("id") or "", "name": item.get("name") or ""}
            for key, item in new_by_name.items() if key not in old_by_name
        ]
        removed = [
            {"id": item.get(id_key) or item.get("id") or "", "name": item.get("name") or ""}
            for key, item in old_by_name.items() if key not in new_by_name
        ]
        compare_fields = ("name", "description", "species") if container == "characters" else (
            "name", "description", "mood"
        )
        updated = []
        for key in sorted(set(old_by_name) & set(new_by_name)):
            old_item = old_by_name[key]
            new_item = new_by_name[key]
            if not any(str(old_item.get(field) or "") != str(new_item.get(field) or "") for field in compare_fields):
                continue
            updated.append({
                "id": new_item.get(id_key) or new_item.get("id") or "",
                "name": new_item.get("name") or "",
                "before": old_item.get("description") or "",
                "after": new_item.get("description") or "",
                "before_name": old_item.get("name") or "",
                "after_name": new_item.get("name") or "",
                "before_species": old_item.get("species") or "",
                "after_species": new_item.get("species") or "",
                "before_mood": old_item.get("mood") or "",
                "after_mood": new_item.get("mood") or "",
                "reason": "cast_changed",
            })
        result[container] = {"added": added, "removed": removed, "updated": updated}
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
    kind: str = "",
) -> List[dict]:
    """阶段 2 描述与旧剧本不同（视为用户手改），且新剧本仍保留的同名条目。

    ``kind`` 区分角色（character）与场景（setting）：两者可能同名，
    冲突合并与确认签名都以 (kind, name) 为键，不能只按 name。
    """
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
                    "kind": kind,
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
        updated["characters"] = merge_model_cast_provenance(
            updated.get("characters") or [], preview["characters"],
            container="characters", id_key="character_id", prefix="char",
        )
    if isinstance(preview.get("settings"), list):
        updated["settings"] = merge_model_cast_provenance(
            updated.get("settings") or [], preview["settings"],
            container="settings", id_key="setting_id", prefix="set",
        )

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
    # AI 修订与手工编辑共用同一失效语义：旧质检结论与建议总时长不可再消费
    from core import script_import

    script_import.mark_import_review_stale(updated)
    return updated
