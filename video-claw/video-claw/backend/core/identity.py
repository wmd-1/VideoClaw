"""GUIP/single identity and admin authorization.

`X-User-Login-Name` is a trust input, not authentication. This module only
answers "which declared login is this request?", then routes enforce ownership.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from config import settings

logger = logging.getLogger(__name__)

_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_RESERVED = {"", ".", ".."}


def owner_key(login: str) -> str:
    return hashlib.sha256(str(login).strip().encode("utf-8")).hexdigest()[:16]


def safe_login(value: str) -> str:
    login = str(value or "").strip()
    if login in _RESERVED or not _LOGIN_RE.fullmatch(login) or ".." in login:
        raise HTTPException(status_code=401, detail="身份格式非法")
    return login


@dataclass(frozen=True)
class CurrentUser:
    login: str
    owner_key: str
    is_admin: bool = False

    @property
    def owner(self) -> str:
        return self.login


def _trusted_source(request: Request) -> bool:
    raw = str(getattr(settings, "USERS_TRUSTED_PROXY_CIDRS", "") or "").strip()
    if not raw:
        return True
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
        networks = [ipaddress.ip_network(item.strip(), strict=False) for item in raw.split(",") if item.strip()]
    except ValueError:
        logger.warning("Invalid users.trusted_proxy_cidrs rejected: %s", raw)
        return False
    return any(address in network for network in networks)


def is_admin_login(login: str) -> bool:
    if str(getattr(settings, "USERS_MODE", "guip")).lower() == "single":
        return True
    admins = getattr(settings, "USERS_ADMIN_LOGINS", []) or []
    return login in {str(item) for item in admins}


async def get_current_user(request: Request) -> CurrentUser:
    mode = str(getattr(settings, "USERS_MODE", "guip")).lower()
    if mode == "single":
        login = "local"
    else:
        login = safe_login(getattr(request.state, "user_login_name", ""))
        if not _trusted_source(request):
            raise HTTPException(status_code=401, detail="身份来源不受信任")
    return CurrentUser(login=login, owner_key=owner_key(login), is_admin=is_admin_login(login))


async def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


class UserIdentityMiddleware:
    """Pure ASGI middleware: record the declared login, never issue a cookie."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw = headers.get(b"x-user-login-name", b"").decode("latin-1").strip()
        scope.setdefault("state", {})["user_login_name"] = raw
        await self.app(scope, receive, send)
