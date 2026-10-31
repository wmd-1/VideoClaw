"""Read-only facade over the committed workflow checkpoints (design D13/D15).

Writes no longer go through this module: every workflow change is committed by
`core.persistence_service.PersistenceService` (revision CAS, one transaction for
stage layers, files, object operations and the session row). The former
best-effort JSON/DB mirror helpers were removed together with the JSON writes.
"""
from __future__ import annotations

from typing import Any, Dict

from config import settings
from db.repositories import StageArtifactRepo


def enabled() -> bool:
    return bool(str(getattr(settings, "DATABASE_DSN", "") or "").strip())


def load_checkpoints(session_id: str) -> Dict[str, Any]:
    """Last completed checkpoint payload per stage (version > 0), independent of run status."""
    if not enabled():
        return {}
    return StageArtifactRepo().list_checkpoints(session_id)
