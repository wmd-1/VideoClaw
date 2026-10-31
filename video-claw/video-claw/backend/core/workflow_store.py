"""Workflow read model and commit builder over the DB snapshot (design D12/D13/D15).

The engine works on an in-memory `WorkflowState` copy; this module translates between
that shape and the persisted layers:

- effective artifact of a stage = `working_json` if present else the last completed
  checkpoint, with `preview_json` keys overlaid (previews never replace the body);
- a changed stage body is promoted to a new checkpoint (version+1) only when its content
  is complete, otherwise saved as working layer; preview keys go to `preview_json`;
- media leaves are limited to the D14 fields below. Before commit every leaf must be
  canonical: values already present in the committed view are carried over, everything
  else is published (new work/temp files) or validated for reuse (indexed objects).
"""
from __future__ import annotations

import copy
import os
import threading
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from core.media_refs import PUBLIC_PREFIX, ResolvedMedia, is_remote, owner_key, to_public

PREVIEW_KEYS = ("pending_revision", "new_episodes", "new_characters", "new_settings", "sequel_idea")

ITEM_CONTAINERS: Dict[str, Tuple[str, ...]] = {
    "character_design": ("characters", "settings"),
    "reference_generation": ("scenes",),
    "prompt_rewrite": ("items",),
    "video_generation": ("clips",),
}

# D14 发布/恢复字段：只有这些位置被当作媒体，文本 versions（prompt_rewrite）不在其中
STAGE_MEDIA: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "character_design": {"containers": ("characters", "settings"), "fields": ("selected", "versions")},
    "reference_generation": {"containers": ("scenes",), "fields": ("selected", "versions")},
    "video_generation": {"containers": ("clips",), "fields": ("selected", "versions")},
    "post_production": {"containers": ("final_videos",), "fields": ("path",), "top": ("final_video",)},
}
_VERSION_PATH_KEYS = ("path", "url")


# ── 媒体字段遍历 ─────────────────────────────────────────────
def _map_value(value: Any, fn: Callable[[Any], Any]) -> Any:
    if isinstance(value, str):
        return fn(value) if value.strip() else value
    return value


def _map_version(entry: Any, fn: Callable[[Any], Any]) -> Any:
    if isinstance(entry, str):
        return _map_value(entry, fn)
    if isinstance(entry, dict):
        mapped = dict(entry)
        for key in _VERSION_PATH_KEYS:
            if isinstance(mapped.get(key), str) and mapped[key].strip():
                mapped[key] = fn(mapped[key])
        return mapped
    return entry


def map_item_media(stage: str, item: Any, fn: Callable[[Any], Any]) -> Any:
    spec = STAGE_MEDIA.get(stage)
    if not spec or not isinstance(item, dict):
        return item
    mapped = dict(item)
    for field in spec["fields"]:
        value = mapped.get(field)
        if isinstance(value, list):
            mapped[field] = [_map_version(entry, fn) for entry in value]
        else:
            mapped[field] = _map_value(value, fn)
    return mapped


def map_media(stage: str, payload: Any, fn: Callable[[Any], Any]) -> Any:
    """Return a copy of the stage payload with every D14 media leaf passed through fn."""
    spec = STAGE_MEDIA.get(stage)
    if not spec or not isinstance(payload, dict):
        return payload
    mapped = dict(payload)
    for key in spec.get("top", ()):
        mapped[key] = _map_value(mapped.get(key), fn)
    for container in spec["containers"]:
        items = mapped.get(container)
        if isinstance(items, list):
            mapped[container] = [map_item_media(stage, item, fn) for item in items]
    return mapped


def iter_media(stage: str, payload: Any) -> Iterator[Any]:
    found: List[Any] = []

    def collect(value: Any) -> Any:
        found.append(value)
        return value

    map_media(stage, payload, collect)
    return iter(found)


def iter_item_media(stage: str, item: Any) -> Iterator[Any]:
    found: List[Any] = []

    def collect(value: Any) -> Any:
        found.append(value)
        return value

    map_item_media(stage, item, collect)
    return iter(found)


def carried_media(artifacts: Dict[str, Any], executions: Optional[Dict[str, Any]] = None) -> Set[str]:
    """已提交视图中的全部媒体叶子：再次出现时视为已校验的旧引用，原样保留。"""
    carried: Set[str] = set()
    for stage, payload in (artifacts or {}).items():
        for value in iter_media(stage, payload):
            carried.add(str(value))
    for stage, execution in (executions or {}).items():
        if not isinstance(execution, dict):
            continue
        carried.update(str(key) for key in (execution.get("version_candidates") or {}))
        for bucket in ("active_items", "last_items"):
            for item in (execution.get(bucket) or {}).values():
                for value in iter_item_media(stage, item):
                    carried.add(str(value))
    return carried


def is_canonical(value: str) -> bool:
    text = str(value or "")
    return text.startswith(PUBLIC_PREFIX) or text.startswith("users/") or is_remote(text)


# ── 产物分层 ─────────────────────────────────────────────────
def strip_internal(payload: Any) -> Any:
    """编排器注入的 `_session_*` 等内部键不进入持久产物。"""
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if not str(key).startswith("_")}


def split_preview(artifact: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    if not isinstance(artifact, dict):
        return artifact, None
    preview = {key: artifact[key] for key in PREVIEW_KEYS if key in artifact}
    base = {key: value for key, value in artifact.items() if key not in PREVIEW_KEYS}
    return base, (preview or None)


def artifact_has_pending(stage: str, art: Any) -> bool:
    """完整性判据（与状态重算同口径）：True 表示仍有待处理条目，不能晋升为正式快照。"""
    if not isinstance(art, dict) or not art:
        return True
    if stage == "character_design":
        chars = art.get("characters", []) or []
        sets = art.get("settings", []) or []
        return any(not c.get("selected") for c in chars if isinstance(c, dict)) or any(
            not s.get("selected") for s in sets if isinstance(s, dict)
        )
    if stage == "storyboard":
        episodes = art.get("episodes", [])
        return not episodes or any(not ep.get("segments") for ep in episodes if isinstance(ep, dict))
    if stage == "reference_generation":
        scenes = art.get("scenes", [])
        return not scenes or any(not s.get("selected") for s in scenes if isinstance(s, dict))
    if stage == "prompt_rewrite":
        items = art.get("items", [])
        return not items or any(i.get("status") not in ("done", "failed") for i in items if isinstance(i, dict))
    if stage == "video_generation":
        clips = art.get("clips", [])
        return not clips or any(not c.get("selected") for c in clips if isinstance(c, dict))
    return False


_LEGACY_META_SHELL = {"project_params", "model_fallback", "boot_id"}


def session_meta(meta_json: Any) -> Dict[str, Any]:
    """会话 meta 读口径：引擎写入扁平参数；legacy 导入行是 {project_params, model_fallback, boot_id} 外壳，展开为扁平参数。"""
    meta = copy.deepcopy(meta_json) if isinstance(meta_json, dict) else {}
    params = meta.get("project_params")
    if not isinstance(params, dict) or not set(meta) <= _LEGACY_META_SHELL:
        return meta
    flat = copy.deepcopy(params)
    if meta.get("model_fallback"):
        flat.setdefault("model_fallback", copy.deepcopy(meta["model_fallback"]))
    return flat


def view_layers(snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """snapshot → (effective artifacts incl. preview keys, previews, executions)."""
    checkpoints = snapshot.get("checkpoints") or {}
    working = snapshot.get("working") or {}
    artifacts: Dict[str, Any] = {}
    previews: Dict[str, Any] = {}
    executions: Dict[str, Any] = {}
    for stage in sorted(set(checkpoints) | set(working)):
        layer = working.get(stage) or {}
        base = layer.get("working") if layer.get("working") is not None else checkpoints.get(stage)
        preview = layer.get("preview")
        artifact = copy.deepcopy(base) if base is not None else None
        if isinstance(preview, dict) and preview:
            previews[stage] = copy.deepcopy(preview)
            artifact = {**(artifact if isinstance(artifact, dict) else {}), **copy.deepcopy(preview)}
        if artifact is not None:
            artifacts[stage] = artifact
        if isinstance(layer.get("execution"), dict):
            executions[stage] = copy.deepcopy(layer["execution"])
    return artifacts, previews, executions


def build_stage_changes(
    before: Dict[str, Any],
    after: Dict[str, Any],
    *,
    has_working: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """比较前后有效产物，生成 commit 的 stage_changes（内容未变的阶段不出现）。"""
    working_stages = set(has_working)
    changes: List[Dict[str, Any]] = []
    for stage in sorted(set(before) | set(after)):
        b_base, b_preview = split_preview(before.get(stage))
        a_base, a_preview = split_preview(after.get(stage))
        change: Dict[str, Any] = {"stage": stage}
        if a_base != b_base and a_base is not None:
            base = a_base if isinstance(a_base, dict) else {"value": a_base}
            if artifact_has_pending(stage, base):
                change["working"] = base
            else:
                change["promote"] = True
                change["checkpoint"] = base
                if stage in working_stages:
                    change["clear_working"] = True
        if a_preview != b_preview:
            if a_preview:
                change["preview"] = a_preview
            else:
                change["clear_preview"] = True
        if len(change) > 1:
            changes.append(change)
    return changes


# ── 执行层投影 ───────────────────────────────────────────────
def execution_item_key(container: str, item_id: str) -> str:
    return f"{container}:{item_id}"


def execution_preview(stage: str, execution: Any) -> Optional[Dict[str, Any]]:
    """上次未完成执行的条目摘要（只读投影，不当成功快照，也不推进停点）。"""
    if not isinstance(execution, dict) or execution.get("status") not in ("failed", "interrupted"):
        return None
    items = []
    for key, item in (execution.get("last_items") or {}).items():
        if not isinstance(item, dict):
            continue
        container, _, item_id = str(key).partition(":")
        summary = {"type": container, "id": item_id, "status": item.get("status")}
        for field in ("selected", "versions", "error", "error_type", "rewritten_prompt", "truncated"):
            if field in item:
                summary[field] = copy.deepcopy(item[field])
        items.append(summary)
    if not items:
        return None
    return {"execution_id": execution.get("execution_id"), "status": execution.get("status"),
            "error": execution.get("error"), "items": items}


# ── 发布 ─────────────────────────────────────────────────────
class ExecutionPublisher:
    """一次执行（或一次人工编辑）的发布上下文：同一本地文件只上传一次。"""

    def __init__(
        self,
        *,
        owner: str,
        session_id: str,
        stage: str,
        execution_id: str,
        runtime_map: Optional[Dict[str, str]] = None,
        boot_id: str = "",
    ) -> None:
        self.owner = owner
        self.owner_key = owner_key(owner)
        self.session_id = session_id
        self.stage = stage
        self.execution_id = execution_id
        self.runtime_map: Dict[str, str] = dict(runtime_map or {})
        self.boot_id = boot_id
        self.published_local: Dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def work_root(self) -> str:
        from core.artifact_publisher import execution_work_root

        return execution_work_root(self.owner_key, "sessions", self.session_id, self.execution_id)

    def canonical_of(self, value: Any) -> str:
        text = str(value)
        if text in self.runtime_map:
            return self.runtime_map[text]
        if os.path.isabs(text):
            known = self.published_local.get(os.path.realpath(text))
            if known:
                return known
        if isinstance(value, ResolvedMedia) and str(getattr(value, "public_path", "") or "").startswith(PUBLIC_PREFIX):
            return str(value.public_path)
        public = to_public(text)
        return public if public.startswith(PUBLIC_PREFIX) else text

    def display(self, value: Any) -> str:
        """面向 API/SSE 的展示值：未发布的本地工作/缓存路径不外露。"""
        canonical = self.canonical_of(value)
        return canonical if is_canonical(canonical) else ""

    def publish(
        self,
        payloads: Dict[str, Any],
        carried: Set[str],
        *,
        item_mode: bool = False,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """发布 payload 中不在 carried 内的媒体叶子；返回 (映射后的 payload, 发布结果)。"""
        from core.artifact_publisher import PublicationManifest, publish

        walker = iter_item_media if item_mode else iter_media
        mapper = map_item_media if item_mode else map_media
        with self._lock:
            manifest = PublicationManifest(
                owner=self.owner,
                owner_key=self.owner_key,
                resource_kind="sessions",
                resource_id=self.session_id,
                stage=self.stage,
                execution_id=self.execution_id,
                runtime_map=self.runtime_map,
            )
            keys: Dict[str, str] = {}
            for stage, payload in payloads.items():
                for value in walker(stage, payload):
                    canonical = self.canonical_of(value)
                    if canonical in carried or canonical in self.published_local.values():
                        continue
                    keys[str(value)] = manifest.add(canonical if canonical != str(value) else value)
            result = None
            if any(not entry.get("remote") for entry in manifest.entries):
                result = publish(manifest, boot_id=self.boot_id)
                for item in result["published"]:
                    if not item.get("reused") and item.get("local_path"):
                        self.published_local[os.path.realpath(item["local_path"])] = item["public_path"]
            canonical_map = (result or {}).get("canonical_map") or {}

            def resolve(value: Any) -> str:
                canonical = self.canonical_of(value)
                key = keys.get(str(value))
                if key is None:
                    return canonical
                return str(canonical_map.get(key, key))

            mapped = {stage: mapper(stage, payload, resolve) for stage, payload in payloads.items()}
        return mapped, result


def published_paths(result: Optional[Dict[str, Any]]) -> Set[str]:
    return {str(item["public_path"]) for item in (result or {}).get("published") or []}


def merge_published(*results: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """多次发布合并为一次提交的补偿单元（files/retained/objects 拼接）。"""
    present = [result for result in results if result]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    merged = dict(present[0])
    merged["published"] = [item for result in present for item in result.get("published") or []]
    merged["operation_ids"] = [op for result in present for op in result.get("operation_ids") or []]
    merged["objects"] = [obj for result in present for obj in result.get("objects") or []]
    merged["canonical_map"] = {k: v for result in present for k, v in (result.get("canonical_map") or {}).items()}
    return merged


__all__ = [
    "ExecutionPublisher",
    "ITEM_CONTAINERS",
    "PREVIEW_KEYS",
    "STAGE_MEDIA",
    "artifact_has_pending",
    "build_stage_changes",
    "carried_media",
    "execution_item_key",
    "execution_preview",
    "is_canonical",
    "iter_item_media",
    "iter_media",
    "map_item_media",
    "map_media",
    "merge_published",
    "published_paths",
    "split_preview",
    "strip_internal",
    "view_layers",
]
