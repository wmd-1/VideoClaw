"""4.11：把受管派发运行时接入应用 lifespan；缺 DB 时不进入纯内存模式。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from config import settings
from core.video_dispatch_runtime import DispatchRuntime, set_dispatch_runtime
from core.video_dispatch_service import DispatchService
from core.video_dispatch_transport import build_custom_video_transport
from core.video_pool_config import load_pool_config
from core.video_probe_service import ProbeService

logger = logging.getLogger(__name__)


def _registries() -> tuple[Dict[str, Any], Dict[str, Any]]:
    values = settings.CONFIG if isinstance(settings.CONFIG, dict) else {}
    providers = (
        values.get("api_providers")
        if isinstance(values.get("api_providers"), dict)
        else {}
    )
    models = {
        str(item.get("id")): item
        for item in values.get("custom_models") or []
        if isinstance(item, dict) and item.get("id")
    }
    return providers, models


def build_dispatch_runtime() -> Optional[DispatchRuntime]:
    """创建并恢复调度运行时；调用方负责 start/stop，禁用时返回 None。"""
    if not bool(getattr(settings, "VIDEO_DISPATCH_ENABLED", True)):
        set_dispatch_runtime(None)
        logger.info("Video dispatch disabled by VC_VIDEO_DISPATCH__ENABLED")
        return None

    path = Path(str(getattr(settings, "VIDEO_PROFILES_FILE", "") or ""))

    def loader():
        # 每次 reload 读取当前 Config，管理员改模型注册表后无需重启调度闭包。
        providers, models = _registries()
        snapshot = load_pool_config(path, providers=providers, models=models)
        return snapshot.data, snapshot.revision

    probe = ProbeService()
    from core.video_dispatch_media import (
        prepare_dispatch_job,
        publish_dispatch_result,
    )

    service = DispatchService(
        pool_loader=loader,
        dispatch_enabled=lambda: bool(
            getattr(settings, "VIDEO_DISPATCH_ENABLED", True)
        ),
        boot_id=str(getattr(settings, "BOOT_ID", "") or ""),
        probe_service=probe,
        job_input_preparer=prepare_dispatch_job,
        result_publisher=publish_dispatch_result,
    )
    try:
        from db.repositories.sessions import SessionRepo

        deleting = service.reconcile_pending_deletions(
            lambda sid: SessionRepo().get(sid) is not None
        )
        runtime_state = service.restore_runtime_state()
        recovered = service.recover_startup(interrupt_queued=True)
    except Exception:
        probe.close()
        raise
    runtime = DispatchRuntime(
        service,
        build_custom_video_transport,
        on_stop=probe.close,
    )
    set_dispatch_runtime(runtime)
    logger.info(
        "Video dispatch runtime prepared: deleting=%s runtime=%s recovery=%s",
        deleting,
        runtime_state,
        recovered,
    )
    return runtime


__all__ = ["build_dispatch_runtime"]
