"""Shared helpers for repository modules (no SQL outside this package)."""
from __future__ import annotations

import time
from typing import Any, Dict

from psycopg.types.json import Jsonb


def now_ms() -> int:
    return int(time.time() * 1000)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value if value is not None else {})


def jsonb_list(value: Any) -> Jsonb:
    return Jsonb(value if value is not None else [])


def state_value(state: Any, name: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def state_session_row(state: Any, *, boot_id: str = "") -> Dict[str, Any]:
    """Map the existing WorkflowState-ish object/dict to a sessions row."""
    sid = str(state_value(state, "session_id"))
    return {
        "session_id": sid,
        "owner": str(state_value(state, "owner", "")),
        "status": str(state_value(state, "status", "idle")),
        "current_stage": state_value(state, "current_stage"),
        "status_json": state_value(state, "status_json", None) or state_value(state, "status", {}),
        "stage_progress_json": state_value(state, "stage_progress", {}),
        "meta_json": state_value(state, "meta", None) or {
            "project_params": state_value(state, "project_params", {}),
            "model_fallback": state_value(state, "model_fallback", {}),
            "boot_id": boot_id,
        },
        "boot_id": boot_id or None,
    }
