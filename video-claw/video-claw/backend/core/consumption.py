"""Per-stage media consumption plan (design D14).

A plan is a list of `ConsumeTarget(source_stage, container, item_id, field, ...)`
derived from the stage, its mode and the actual item set being executed. Only those
fields are materialized, on a runtime copy, **after** `_session_meta/_session_artifacts/
_session_owner` injection; persisted state keeps canonical values.

Policy (mirrors the agents that consume these fields):
- a non-empty explicit selection that cannot be read is never silently swapped for
  another version: a required target fails the stage before any model call;
- an empty selection may fall back to the latest *indexed* version only where the
  agent historically did so (no disk glob); otherwise an empty selection is not fatal
  here — the agent keeps its per-item handling (that item fails or degrades as before)
  — except targets with `require_value` (post-production clips of a rebuilt episode),
  where an empty selection aborts instead of silently cutting an incomplete video;
- optional failures blank the field in the runtime copy so the consumer degrades
  (e.g. text_only grounding) instead of opening a canonical string as a path;
- "not found / missing" and "storage unavailable" are reported separately.

Materialization keeps `ResolvedMedia` (never `str(...)`): `_refs` cache paths must not
enter the copy-input guard again (R10 regression). Local files handed out are pinned
until `MaterializedInputs.release()` so the cleaner cannot remove them mid-use.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core import media_refs
from core.h3_modes import NEXT_FRAME_MODES
from core.resource_claims import CLAIMS, local_identity
from storage import StorageError

_KEY_FIELDS = ("id", "segment_id", "character_id", "setting_id", "clip_id")
_VERSION_PATH_FIELDS = ("path", "url", "image", "video", "selected")
_FRAME_MODES = ("first_frame", "start_end_frame")
_NEXT_FRAME_MODES = NEXT_FRAME_MODES  # 单一来源：core.h3_modes（避免“物化了但 Agent 不用”）


@dataclass(frozen=True)
class ConsumeTarget:
    source_stage: str
    container: str
    item_id: str
    field: str = "selected"
    required: bool = False
    fallback_versions: bool = False
    label: str = ""
    # 空选中也视为必需失败（后期重拼集内片段：不得跳过后生成不完整成片）
    require_value: bool = False
    # 消费该字段的条目（如需要此尾帧的上一片段），只用于诊断定位
    consumers: Tuple[str, ...] = ()
    # (消费条目, 用途) 对；合并后用于写清“哪个片段的首帧/尾帧”
    usages: Tuple[Tuple[str, str], ...] = ()


_STAGE_LABELS = {
    "script_generation": "剧本", "character_design": "角色/场景设计", "storyboard": "分镜",
    "reference_generation": "参考图", "prompt_rewrite": "提示词改写", "video_generation": "视频生成",
    "post_production": "后期制作",
}
_CONTAINER_LABELS = {
    "characters": "角色", "settings": "场景", "scenes": "参考图", "clips": "视频片段", "items": "提示词",
}
_REASON_LABELS = {
    "empty": "未选择", "missing": "文件已丢失", "not_found": "不存在或无权访问",
    "unavailable": "存储暂不可用", "invalid": "引用无效", "missing_item": "条目不存在",
}
_USE_LABELS = {
    "first_frame": "首帧", "last_frame": "尾帧", "clip": "拼接片段", "reference": "参考图",
    "edit": "编辑原图", "grounding": "改写参考", "explicit": "输入",
}


@dataclass(frozen=True)
class ConsumeIssue:
    target: ConsumeTarget
    reason: str  # missing_item | empty | missing | not_found | unavailable | invalid
    detail: str = ""

    def as_dict(self, stage: str) -> Dict[str, Any]:
        """前端可定位的缺失描述（不含本地路径）。"""
        target = self.target
        return {
            "stage": stage,
            "source_stage": target.source_stage,
            "container": target.container,
            "item_id": target.item_id,
            "field": target.field,
            "reason": self.reason,
            "uses": [label for label in target.label.split("+") if label],
            "consumers": list(target.consumers),
            "message": self.describe(),
        }

    def describe(self) -> str:
        target = self.target
        what = f"{_CONTAINER_LABELS.get(target.container, target.container)} {target.item_id}"
        if target.source_stage == "input":
            what = f"输入 {target.item_id}"
        reason = _REASON_LABELS.get(self.reason, self.reason)
        parts = []
        for consumer, label in target.usages:
            use = _USE_LABELS.get(label, label)
            parts.append(f"本条目的{use}" if consumer == target.item_id else f"片段 {consumer} 的{use}")
        used_by = f"（用于{'、'.join(dict.fromkeys(parts))}）" if parts else ""
        return f"{what} 的选中媒体{reason}{used_by}"


class ConsumptionError(RuntimeError):
    """Required input media cannot be read for the items this execution will process."""

    def __init__(self, stage: str, issues: Iterable[ConsumeIssue]):
        self.stage = stage
        self.issues = list(issues)
        self.missing = [issue.target for issue in self.issues]
        self.unavailable = any(issue.reason == "unavailable" for issue in self.issues)
        sources = sorted({_STAGE_LABELS.get(i.target.source_stage, i.target.source_stage)
                          for i in self.issues if i.target.source_stage != "input"})
        hint = f"请到「{'」「'.join(sources)}」阶段重新选择、上传或重新生成对应条目。" if sources else ""
        details = "；".join(issue.describe() for issue in self.issues)
        super().__init__(f"{_STAGE_LABELS.get(stage, stage)} 必需媒体不可用：{details}。{hint}")

    def as_dicts(self) -> List[Dict[str, Any]]:
        return [issue.as_dict(self.stage) for issue in self.issues]


@dataclass
class MaterializedInputs:
    runtime: Dict[str, Any]
    optional_issues: List[ConsumeIssue] = field(default_factory=list)
    runtime_map: Dict[str, str] = field(default_factory=dict)
    explicit: Dict[str, Any] = field(default_factory=dict)
    pinned: List[str] = field(default_factory=list)

    @property
    def missing_optional(self) -> List[ConsumeTarget]:
        return [issue.target for issue in self.optional_issues]

    def release(self) -> None:
        while self.pinned:
            CLAIMS.unpin(self.pinned.pop())

    def __enter__(self) -> "MaterializedInputs":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


# ── 结构定位 ────────────────────────────────────────────────
def _payload(artifacts: Dict[str, Any], stage: str) -> Dict[str, Any]:
    value = (artifacts or {}).get(stage)
    return value if isinstance(value, dict) else {}


def _item_key(item: Dict[str, Any], index: int) -> str:
    for name in _KEY_FIELDS:
        if item.get(name):
            return str(item[name])
    return str(index)


def _items(artifacts: Dict[str, Any], stage: str, container: str) -> Dict[str, Dict[str, Any]]:
    items = _payload(artifacts, stage).get(container)
    if not isinstance(items, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(items):
        if isinstance(item, dict):
            result[_item_key(item, index)] = item
    return result


def _segments(artifacts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """分镜片段（按播放顺序）：episodes[].segments[]，兼容顶层 segments。"""
    storyboard = _payload(artifacts, "storyboard")
    segments: List[Dict[str, Any]] = []
    for episode in storyboard.get("episodes") or []:
        if isinstance(episode, dict):
            segments.extend(seg for seg in episode.get("segments") or [] if isinstance(seg, dict))
    if not segments:
        segments = [seg for seg in storyboard.get("segments") or [] if isinstance(seg, dict)]
    return segments


def _segment_ids(segments: List[Dict[str, Any]]) -> List[str]:
    return [_item_key(seg, index) for index, seg in enumerate(segments)]


def _wanted(ids: List[str], target_ids: Optional[Iterable[str]]) -> List[str]:
    if target_ids is None:
        return list(ids)
    wanted = {str(item) for item in target_ids}
    return [item for item in ids if item in wanted]


def _match_asset(name: str, assets: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """与 video_agent/_collect_refs 相同的匹配次序：id/名称精确 → 名称精确 → 名称包含。"""
    clean = str(name or "").strip()
    if not clean:
        return None
    if clean in assets:
        return clean
    for key, asset in assets.items():
        if str(asset.get("name") or "").strip() == clean:
            return key
    for key, asset in assets.items():
        asset_name = str(asset.get("name") or "").strip()
        if asset_name and (asset_name in clean or clean in asset_name):
            return key
    return None


def _asset_target(container: str, key: str, asset: Dict[str, Any], label: str) -> ConsumeTarget:
    # 显式选中不可静默换版；未选中时沿用旧兼容：取已索引 versions 的最新一项（可选）
    if str(asset.get("selected") or "").strip():
        return ConsumeTarget("character_design", container, key, "selected", True, False, label)
    return ConsumeTarget("character_design", container, key, "selected", False, True, label)


def reference_assets_for_segment(artifacts: Dict[str, Any], segment: Dict[str, Any]
                                  ) -> List[Tuple[str, str, Dict[str, Any]]]:
    """参考集有序清单：**场景 → 角色**（角色按 `segment.characters` 顺序），按选中路径
    去重并保序。

    顺序以 `video_agent._get_segment_reference_assets` 为准（视频实际入参），
    `<Picture N>` 必须与该顺序一一对应。
    """
    characters = _items(artifacts, "character_design", "characters")
    settings_items = _items(artifacts, "character_design", "settings")
    ordered: List[Tuple[str, str, Dict[str, Any]]] = []
    seen: set = set()

    def add(container: str, key: Optional[str], items: Dict[str, Dict[str, Any]]) -> None:
        if key is None:
            return
        asset = items.get(key) or {}
        marker = str(asset.get("selected") or "").strip() or f"{container}:{key}"
        if marker in seen:
            return
        seen.add(marker)
        ordered.append((container, key, asset))

    add("settings", _match_asset(str(segment.get("location") or ""), settings_items), settings_items)
    for name in segment.get("characters") or []:
        add("characters", _match_asset(str(name), characters), characters)
    return ordered


def _asset_targets_for_segment(artifacts: Dict[str, Any], segment: Dict[str, Any], label: str) -> List[ConsumeTarget]:
    targets: List[ConsumeTarget] = []
    for container, key, asset in reference_assets_for_segment(artifacts, segment):
        targets.append(_asset_target(container, key, asset, label))
    return targets


def _clip_episode(clip: Dict[str, Any]) -> int:
    """与 editor_agent 相同：episode 字段 → id 中的集号 → name 中的集号 → 1。"""
    if clip.get("episode") is not None:
        try:
            return int(clip["episode"])
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:seg_|shot_)?(\d{1,3})_\d{1,3}", str(clip.get("id") or ""))
    if match:
        return int(match.group(1))
    name = str(clip.get("name") or "")
    match = re.search(r"第(\d+)集", name) or re.search(r"(\d+)", name)
    return int(match.group(1)) if match else 1


def _grounding_enabled() -> bool:
    from config import Config

    return bool(getattr(Config, "H3_REWRITE_GROUNDING_ENABLED", True))


# ── 计划 ────────────────────────────────────────────────────
def plan_targets(
    stage: str,
    artifacts: Dict[str, Any],
    *,
    mode: str = "",
    target_ids: Optional[Iterable[str]] = None,
    episodes: Optional[Iterable[int]] = None,
    grounding: Optional[bool] = None,
) -> List[ConsumeTarget]:
    """按阶段/模式/目标集合给出实际要读取的媒体字段（去重、保序）。

    - character_design：仅编辑图时（target_ids=条目 id）读取对应条目选中图；生成不读历史；
    - reference_generation / prompt_rewrite / video_generation：target_ids 为分镜片段 id；
    - post_production：target_ids 为片段 id、episodes 为重拼集号（任一为 None 表示不过滤）。
    """
    targets: List[ConsumeTarget] = []

    if stage == "character_design":
        if target_ids is None:
            return []
        characters = _items(artifacts, stage, "characters")
        settings_items = _items(artifacts, stage, "settings")
        for item_id in (str(t) for t in target_ids):
            container = "settings" if item_id in settings_items and item_id not in characters else "characters"
            targets.append(ConsumeTarget(stage, container, item_id, "selected", True, False, "edit", consumers=(item_id,)))

    elif stage == "reference_generation":
        segments = _segments(artifacts)
        by_id = dict(zip(_segment_ids(segments), segments))
        for seg_id in _wanted(list(by_id), target_ids):
            targets.extend(_used_by(seg_id, _asset_targets_for_segment(artifacts, by_id[seg_id], "reference")))

    elif stage == "prompt_rewrite":
        if not (_grounding_enabled() if grounding is None else grounding):
            return []
        segments = _segments(artifacts)
        ids = _segment_ids(segments)
        by_id = dict(zip(ids, segments))
        for seg_id in _wanted(ids, target_ids):
            if mode == "reference":
                # Ref2VA：与视频 reference 模式共用同一有序参考集（场景→角色）
                targets.extend(_used_by(
                    seg_id, _asset_targets_for_segment(artifacts, by_id[seg_id], "grounding")
                ))
                continue
            targets.append(ConsumeTarget("reference_generation", "scenes", seg_id, "selected", label="grounding",
                                         consumers=(seg_id,)))
            index = ids.index(seg_id)
            if mode in _NEXT_FRAME_MODES and index + 1 < len(ids):
                # 尾帧参考来自下一条分镜；目标集合外也要取
                targets.append(
                    ConsumeTarget("reference_generation", "scenes", ids[index + 1], "selected", label="grounding",
                                  consumers=(seg_id,))
                )

    elif stage == "video_generation":
        segments = _segments(artifacts)
        ids = _segment_ids(segments)
        by_id = dict(zip(ids, segments))
        video_mode = mode or "first_frame"
        for seg_id in _wanted(ids, target_ids):
            if video_mode == "reference":
                targets.extend(_used_by(seg_id, _asset_targets_for_segment(artifacts, by_id[seg_id], "reference")))
                continue
            if video_mode not in _FRAME_MODES:
                continue
            targets.append(ConsumeTarget("reference_generation", "scenes", seg_id, "selected", True, True, "first_frame",
                                         consumers=(seg_id,)))
            index = ids.index(seg_id)
            if video_mode == "start_end_frame" and index + 1 < len(ids):
                # 仅末片无尾帧沿用既有首帧回退；其余片段尾帧必需
                targets.append(
                    ConsumeTarget("reference_generation", "scenes", ids[index + 1], "selected", True, True, "last_frame",
                                  consumers=(seg_id,))
                )

    elif stage == "post_production":
        # 后期拼接的输入来自 video_generation，而不是 post_production 自身
        clips = _items(artifacts, "video_generation", "clips")
        wanted_episodes = None if episodes is None else {int(ep) for ep in episodes}
        for clip_id in _wanted(list(clips), target_ids):
            if wanted_episodes is not None and _clip_episode(clips[clip_id]) not in wanted_episodes:
                continue
            targets.append(ConsumeTarget("video_generation", "clips", clip_id, "selected", True, False, "clip", True,
                                         consumers=(clip_id,)))

    return _merge_targets(targets)


def _used_by(consumer: str, targets: List[ConsumeTarget]) -> List[ConsumeTarget]:
    return [replace(target, consumers=(consumer,)) for target in targets]


def _merge_targets(targets: List[ConsumeTarget]) -> List[ConsumeTarget]:
    """同一字段只物化一次（如下一片首帧同时是上一片尾帧）：必需性/回退取或，标签与消费方合并。"""
    merged: Dict[Tuple[str, str, str, str], ConsumeTarget] = {}
    for target in targets:
        if target.consumers and not target.usages:
            target = replace(target, usages=tuple((consumer, target.label) for consumer in target.consumers))
        key = (target.source_stage, target.container, target.item_id, target.field)
        current = merged.get(key)
        if current is None:
            merged[key] = target
            continue
        labels = "+".join(dict.fromkeys([*current.label.split("+"), target.label]))
        merged[key] = ConsumeTarget(
            *key,
            required=current.required or target.required,
            fallback_versions=current.fallback_versions or target.fallback_versions,
            label=labels,
            require_value=current.require_value or target.require_value,
            consumers=tuple(dict.fromkeys([*current.consumers, *target.consumers])),
            usages=tuple(dict.fromkeys([*current.usages, *target.usages])),
        )
    return list(merged.values())


# ── 物化 ────────────────────────────────────────────────────
def _latest_version(versions: Any) -> str:
    if not isinstance(versions, list):
        return ""
    for entry in reversed(versions):
        if isinstance(entry, str) and entry.strip():
            return entry.strip()
        if isinstance(entry, dict):
            for name in _VERSION_PATH_FIELDS:
                value = entry.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _materialize_pinned(value: str, user: Any, reference_factory: Optional[Callable[[str, Any], Any]] = None) -> Tuple[Any, str]:
    """物化并对本地文件加 pin；pin 后文件被并发清理则重取一次。返回 (值, pin 身份)。

    reference_factory(canonical, resolved) 可为推理适配器附加引用值对象（HTTP 候选等）。
    """
    for _ in range(2):
        resolved = media_refs.materialize(value, user)
        local = str(getattr(resolved, "local_path", "") or "")
        if not local or media_refs.is_remote(str(resolved)):
            return resolved, ""
        identity = local_identity(local)
        if not CLAIMS.pin(identity):
            raise media_refs.MediaNotFound(value)
        if os.path.isfile(local):
            if reference_factory is not None:
                try:
                    resolved.reference = reference_factory(value, resolved)
                except Exception:  # noqa: BLE001 - 引用候选是增强项，失败时仍可本地消费
                    resolved.reference = None
            return resolved, identity
        CLAIMS.unpin(identity)
    raise media_refs.MediaMissingError(value)


def _classify(exc: Exception) -> str:
    if isinstance(exc, media_refs.MediaMissingError):
        return "missing"
    if isinstance(exc, media_refs.MediaNotFound):
        return "not_found"
    if isinstance(exc, (media_refs.MediaUnavailable, StorageError)):
        return "unavailable"
    return "invalid"


def materialize_stage_inputs(
    stage: str,
    artifacts: Dict[str, Any],
    user: Any,
    *,
    mode: str = "",
    target_ids: Optional[Iterable[str]] = None,
    episodes: Optional[Iterable[int]] = None,
    grounding: Optional[bool] = None,
    explicit: Optional[Dict[str, Any]] = None,
    strict_required: bool = True,
    reference_factory: Optional[Callable[[str, Any], Any]] = None,
) -> MaterializedInputs:
    """物化本阶段目标字段与显式输入（如 audio_reference_url）；只改运行副本。

    必需项（非空显式选中；带 require_value 的目标含空选中）不可用时释放已取得的 pin 并抛 `ConsumptionError`；
    调用方用完后 `release()`。
    """
    runtime = copy.deepcopy(artifacts if isinstance(artifacts, dict) else {})
    result = MaterializedInputs(runtime=runtime)
    required_issues: List[ConsumeIssue] = []

    def fail(target: ConsumeTarget, reason: str, detail: str = "", *, explicit_value: bool = True) -> None:
        issue = ConsumeIssue(target, reason, detail)
        (required_issues if target.required and explicit_value else result.optional_issues).append(issue)

    def resolve(value: str) -> Any:
        resolved, identity = _materialize_pinned(value, user, reference_factory)
        if identity:
            result.pinned.append(identity)
        result.runtime_map[str(resolved)] = value
        return resolved

    try:
        for target in plan_targets(stage, runtime, mode=mode, target_ids=target_ids,
                                   episodes=episodes, grounding=grounding):
            item = _items(runtime, target.source_stage, target.container).get(target.item_id)
            # 取值始终来自原 canonical 快照，写入只落在运行副本
            source = _items(artifacts if isinstance(artifacts, dict) else {},
                            target.source_stage, target.container).get(target.item_id)
            if item is None or source is None:
                fail(target, "missing_item", explicit_value=False)
                continue
            value = str(source.get(target.field) or "").strip()
            explicit_value = bool(value)
            if not value and target.fallback_versions:
                value = _latest_version(source.get("versions"))
            if not value:
                fail(target, "empty", explicit_value=target.require_value)
                continue
            try:
                item[target.field] = resolve(value)  # 保留 ResolvedMedia，禁止 str() 降级
            except (media_refs.MediaInputError, StorageError) as exc:
                if not (target.required and explicit_value):
                    item[target.field] = ""  # 可选降级：消费方按缺图处理，不把 canonical 当路径
                fail(target, _classify(exc), str(exc), explicit_value=explicit_value)

        for name, value in (explicit or {}).items():
            target = ConsumeTarget("input", "", name, name, True, False, "explicit")
            values = value if isinstance(value, list) else [value]
            resolved_values = []
            for raw in values:
                text = str(raw or "").strip()
                if not text:
                    continue
                try:
                    resolved_values.append(resolve(text))
                except (media_refs.MediaInputError, StorageError) as exc:
                    fail(target, _classify(exc), str(exc))
            result.explicit[name] = resolved_values if isinstance(value, list) else (
                resolved_values[0] if resolved_values else value
            )

        if strict_required and required_issues:
            raise ConsumptionError(stage, required_issues)
    except BaseException:
        result.release()
        raise
    return result


def reverse_map(local_path: str, runtime_map: Dict[str, str]) -> str:
    """发布侧反向映射：物化后的本地路径 → 原始 canonical 值。"""
    return str(runtime_map.get(str(local_path)) or "")
