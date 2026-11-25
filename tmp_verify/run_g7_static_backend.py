# -*- coding: utf-8 -*-
"""Isolated browser fixture server: keep cast states static and skip startup recovery."""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

from api.dependencies import workflow_engine
import core.cast_sync_service as cast_sync_module


class StaticCastSyncService:
    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        pass

    async def stop(self):
        pass

    def notify(self, _session_id):
        pass

    def has_active_or_pending(self, _session_id):
        return False

    def stop_session(self, _session_id):
        pass


workflow_engine.recover_cast_reparse_sessions = lambda: []
cast_sync_module.CastSyncService = StaticCastSyncService

from api.app import app
import uvicorn

uvicorn.run(app, host="0.0.0.0", port=8500)
