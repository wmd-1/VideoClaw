"""Identity endpoints. No login/password/JWT/token issuance."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from config import settings
from core.identity import CurrentUser, get_current_user

router = APIRouter(tags=["Identity"])


@router.get("/api/identity/mode")
async def get_mode():
    return {
        "mode": str(getattr(settings, "USERS_MODE", "guip")),
        "allow_url_user": bool(getattr(settings, "USERS_ALLOW_URL_USER", True)),
    }


@router.get("/api/identity/me")
async def get_me(user: CurrentUser = Depends(get_current_user)):
    return {
        "mode": str(getattr(settings, "USERS_MODE", "guip")),
        "login": user.login,
        "is_admin": bool(user.is_admin),
    }


@router.post("/api/identity/logout")
async def logout(response: Response):
    # Compatibility: older builds issued vc_user. New builds never set it.
    response.delete_cookie("vc_user", path="/")
    return {"status": "ok"}
