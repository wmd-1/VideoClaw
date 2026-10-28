"""Ownership checks shared by routers. Admin is not an implicit cross-reader."""
from __future__ import annotations

from fastapi import HTTPException

from config import settings
from core.identity import CurrentUser
from core.orchestrator import WorkflowEngine


def assert_session_owner(engine: WorkflowEngine, session_id: str, user: CurrentUser) -> None:
    owner = engine.owner_of(session_id)
    if not owner:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_owner_login(owner, user.login):
        raise HTTPException(status_code=404, detail="Session not found")


def legacy_owner() -> str:
    return str(getattr(settings, "USERS_LEGACY_OWNER", "local"))


def is_owner_login(owner: str | None, login: str) -> bool:
    return str(owner or legacy_owner()) == login


def is_owner(owner: str | None, user: CurrentUser) -> bool:
    return is_owner_login(owner, user.login)


def assert_owner(owner: str | None, user: CurrentUser, *, not_found: str = "Not found") -> None:
    if not is_owner(owner, user):
        raise HTTPException(status_code=404, detail=not_found)
