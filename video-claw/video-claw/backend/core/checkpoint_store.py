"""Best-effort bridge from the file-backed engine to PostgreSQL checkpoints.

The JSON snapshot remains the recovery source in this batch; PostgreSQL is
written alongside it so stage writes become durable/immutable before the final
engine cutover. When database.dsn is empty (local dev), every helper is a no-op.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from config import settings
from db.repositories import FileRepo, SessionRepo, StageArtifactRepo

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(str(getattr(settings, "DATABASE_DSN", "") or "").strip())


def mirror_session(state: Any) -> None:
    if not enabled():
        return
    try:
        SessionRepo().upsert(state, boot_id=str(getattr(state, "boot_id", "") or ""))
    except Exception:
        logger.exception("checkpoint mirror session failed: %s", getattr(state, "session_id", ""))
        raise


def delete_session(session_id: str) -> None:
    if not enabled():
        return
    SessionRepo().delete(session_id)


def load_checkpoints(session_id: str) -> Dict[str, Any]:
    if not enabled():
        return {}
    return StageArtifactRepo().list_checkpoints(session_id)


def mirror_running(state: Any, stage: str) -> None:
    if not enabled():
        return
    try:
        StageArtifactRepo().mark_running(
            state.session_id,
            stage,
            str(getattr(state, "boot_id", "") or ""),
            session_patch={
                "current_stage": state.current_stage.value
                if hasattr(state.current_stage, "value")
                else str(state.current_stage),
                "status_json": state.status,
                "stage_progress_json": state.stage_progress,
                "boot_id": str(getattr(state, "boot_id", "") or "") or None,
            },
        )
    except Exception:
        logger.exception("checkpoint mirror running failed: %s/%s", state.session_id, stage)
        raise


def _registerable_files(state: Any, stage: str, payload: Any) -> List[Dict[str, Any]]:
    import hashlib
    import mimetypes
    import os

    from core.media_refs import PUBLIC_PREFIX, to_local, to_public

    found: Dict[str, Dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, str):
            public = to_public(node)
            if not public.startswith(PUBLIC_PREFIX):
                return
            local = to_local(public)
            if not os.path.isfile(local) or FileRepo().get_by_public_path(public):
                return
            try:
                size = os.path.getsize(local)
            except OSError:
                size = None
            found[public] = {
                "file_id": hashlib.sha256(public.encode()).hexdigest()[:32],
                "owner": str(getattr(state, "owner", "") or ""),
                "session_id": str(state.session_id),
                "task_id": None,
                "record_id": None,
                "stage": stage,
                "kind": "artifact",
                "storage_kind": "local",
                "bucket": None,
                "object_key": public.lstrip("/"),
                "public_path": public,
                "local_path": local,
                "media_type": mimetypes.guess_type(public)[0],
                "size_bytes": size,
            }
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return list(found.values())


def mirror_completed(state: Any, stage: str) -> None:
    if not enabled():
        return
    payload = state.artifacts.get(stage)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    try:
        StageArtifactRepo().complete(
            state.session_id,
            stage,
            payload,
            files=_registerable_files(state, stage, payload),
            session_patch={
                "status": state.current_stage.value if hasattr(state.current_stage, "value") else str(state.current_stage),
                "current_stage": state.current_stage.value if hasattr(state.current_stage, "value") else str(state.current_stage),
                "status_json": state.status,
                "stage_progress_json": state.stage_progress,
                "boot_id": None,
            },
        )
    except Exception:
        logger.exception("checkpoint mirror completed failed: %s/%s", state.session_id, stage)
        raise


def mirror_failed(state: Any, stage: str, error: str, status: str = "error") -> None:
    if not enabled():
        return
    try:
        StageArtifactRepo().fail(
            state.session_id,
            stage,
            error,
            status=status,
            session_patch={
                "status_json": state.status,
                "stage_progress_json": state.stage_progress,
                "boot_id": None,
            },
        )
    except Exception:
        logger.exception("checkpoint mirror failed: %s/%s", state.session_id, stage)
        raise
