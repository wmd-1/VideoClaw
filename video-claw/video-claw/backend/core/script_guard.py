# -*- coding: utf-8 -*-
"""剧本锁、下游就绪与阶段有意义产物的纯判据。"""
from __future__ import annotations

from typing import Any

from core import script_revision
from core.reference_skip import reference_skip_marker

SCRIPT_STAGE = "script_generation"
DOWNSTREAM_STAGES = (
    "character_design",
    "storyboard",
    "reference_generation",
    "prompt_rewrite",
    "video_generation",
    "post_production",
)
_ACCEPTED_FIELDS = ("accepted_by", "accepted_at", "accepted_base_version", "accepted_attempt")


def _stage_value(stage: Any) -> str:
    return str(getattr(stage, "value", stage))


def _nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def meaningful_artifact(
    stage: Any,
    artifact: Any,
    *,
    video_generation_mode: str = "first_frame",
) -> bool:
    """空壳不算下游进展；判据只读，不修改产物。"""
    stage_key = _stage_value(stage)
    if not isinstance(artifact, dict):
        return False
    if stage_key == "character_design":
        return _nonempty_list(artifact.get("characters")) or _nonempty_list(artifact.get("settings"))
    if stage_key == "storyboard":
        return any(
            isinstance(episode, dict) and _nonempty_list(episode.get("segments"))
            for episode in artifact.get("episodes") or []
        )
    if stage_key == "reference_generation":
        if _nonempty_list(artifact.get("scenes")):
            return True
        return str(video_generation_mode or "first_frame") == "reference" and bool(
            reference_skip_marker(artifact)
        )
    if stage_key == "prompt_rewrite":
        return _nonempty_list(artifact.get("items"))
    if stage_key == "video_generation":
        return _nonempty_list(artifact.get("clips"))
    if stage_key == "post_production":
        return bool(artifact.get("final_videos") or artifact.get("final_video"))
    # 新增阶段沿用“任一业务列表非空”的保守兜底。
    return any(_nonempty_list(value) for value in artifact.values())


def script_locked(state: Any) -> bool:
    """当前停点、下游执行或真实下游产物任一成立即锁。"""
    # 惰性导入避免 script_guard → orchestrator → script_guard 的导入环。
    from core.orchestrator import _FULL_STAGE_ORDER, WorkflowStage, get_stage_order

    current = _stage_value(getattr(state, "current_stage", WorkflowStage.INIT))
    if current == WorkflowStage.COMPLETED.value:
        return True
    enabled = [_stage_value(stage) for stage in get_stage_order()]
    full = [_stage_value(stage) for stage in _FULL_STAGE_ORDER]
    order = enabled if current in enabled else full
    if current in order and order.index(current) > order.index(SCRIPT_STAGE):
        return True

    executions = getattr(state, "executions", {})
    if isinstance(executions, dict):
        for stage in DOWNSTREAM_STAGES:
            if isinstance(executions.get(stage), dict) and executions[stage]:
                return True

    artifacts = getattr(state, "artifacts", {})
    meta = getattr(state, "meta", {})
    mode = str((meta or {}).get("video_generation_mode") or "first_frame")
    if isinstance(artifacts, dict):
        return any(
            meaningful_artifact(stage, artifacts.get(stage), video_generation_mode=mode)
            for stage in DOWNSTREAM_STAGES
        )
    return False


def cast_reparse_ready(state: Any) -> bool:
    """历史会话无内部键时兼容放行；其余只接受版本匹配的终态。"""
    meta = getattr(state, "meta", {})
    meta = meta if isinstance(meta, dict) else {}
    if "_cast_reparse" not in meta:
        return True
    record = meta.get("_cast_reparse")
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    # 接受审计字段只能属于 accepted_stale；残留即失败关闭，不能借 done/base_version 绕过。
    if status != "accepted_stale" and any(key in record for key in _ACCEPTED_FIELDS):
        return False
    artifacts = getattr(state, "artifacts", {})
    artifact = artifacts.get(SCRIPT_STAGE) if isinstance(artifacts, dict) else None
    current_version = script_revision.structure_fingerprint(
        artifact if isinstance(artifact, dict) else {}, scope="body"
    )
    if status == "done":
        return record.get("base_version") == current_version
    if status == "accepted_stale":
        return record.get("accepted_base_version") == current_version
    return False
