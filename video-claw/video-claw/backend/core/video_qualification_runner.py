"""5.4：管理员真实生成诊断 runner；经统一调度入队并占用真实槽位。"""
from __future__ import annotations

import base64
import logging
import os
import time
import uuid
from typing import Any, Dict, Iterable, Optional

from config import Config
from core.video_dispatch_runtime import get_dispatch_runtime
from core.identity import owner_key
from core.video_dispatch_service import DispatchUnavailable
from models.config_model import resolve_model_entry

logger = logging.getLogger(__name__)

_TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2n0kAAAAASUVORK5CYII="
)


def _tasks(meta: Dict[str, Any]) -> set[str]:
    return {
        str(item)
        for item in ((meta.get("capabilities") or {}).get("tasks") or [])
    }


def _mode_for(meta: Dict[str, Any], contracts: Iterable[str]) -> Optional[str]:
    """同时满足模型能力与池契约；池未声明时不能靠模型自报绕过。"""
    allowed = {str(item) for item in contracts}
    tasks = _tasks(meta)
    for task, mode in (
        ("t2va", "t2va"),
        ("fl2va", "first_frame"),
        ("ref2va", "reference"),
    ):
        if task in tasks and mode in allowed:
            return mode
    return None


def _profile_for_instance(
    snapshot: Any, instance_id: str, meta: Dict[str, Any]
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    data = snapshot.data if hasattr(snapshot, "data") else snapshot
    candidates = []
    for row in (data or {}).get("video_profiles") or []:
        if not isinstance(row, dict) or not row.get("enabled", True):
            continue
        if instance_id not in {str(item) for item in row.get("instances") or []}:
            continue
        candidates.append(row)
    for row in candidates:
        mode = _mode_for(meta, (row.get("contracts") or {}).keys())
        if mode is not None:
            return row, mode
    return (candidates[0], None) if candidates else (None, None)


def build_qualification_test_runner(
    *, timeout_seconds: float = 120.0, poll_interval: float = 1.0
):
    def run(instance: Dict[str, Any], snapshot: Any) -> Dict[str, Any]:
        kind, meta = resolve_model_entry(str(instance.get("model_id") or ""))
        if kind != "custom":
            raise RuntimeError("实例模型不是自定义视频模型，不能做真实生成诊断")
        instance_id = str(instance.get("instance_id") or "")
        profile, mode = _profile_for_instance(snapshot, instance_id, meta)
        if profile is None or not profile.get("id"):
            raise RuntimeError("实例不属于任何启用的资源池，不能走统一调度诊断")
        if mode is None:
            raise RuntimeError("模型能力与资源池 contract 没有交集，不能做真实生成诊断")
        profile_id = str(profile["id"])

        dispatch_runtime = get_dispatch_runtime()
        if dispatch_runtime is None:
            raise DispatchUnavailable("调度运行时未启用", error_code="dispatch_disabled")
        service = dispatch_runtime.service

        job_id = f"qualify-{uuid.uuid4().hex}"
        save_dir = os.path.join(str(Config.TEMP_DIR), "qualification_test", job_id)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, "result.mp4")
        image_path = None
        reference_image_paths = None
        if mode in ("first_frame", "reference"):
            image_path = os.path.join(save_dir, "input.png")
            with open(image_path, "wb") as handle:
                handle.write(_TEST_PNG)
            if mode == "reference":
                reference_image_paths = [image_path]
                image_path = None

        job = {
            "request_key": job_id,
            "item_id": "diagnostic",
            "source_kind": "qualification",
            "source_id": instance_id,
            "execution_id": job_id,
            "dispatch_mode": "auto",
            "profile_id": profile_id,
            "profile_revision": int(profile.get("revision") or 1),
            "snapshot_json": {
                "diagnostic": True,
                "mode": mode,
                "members": [instance_id],
                "enable_concurrency": False,
                "max_concurrency_per_execution": 1,
                "request": {
                    "prompt": "a small red dot slowly moving on white background",
                    "save_path": save_path,
                    "image_path": image_path,
                    "reference_image_paths": reference_image_paths,
                    "duration": 1,
                    "video_ratio": "1:1",
                    "resolution": "480x480",
                },
            },
        }
        try:
            service.enqueue_batch(
                owner=owner_key("system:qualification"),
                batch_id=job_id,
                jobs=[job],
                queue_timeout_seconds=3600,
                max_queued_jobs=1000,
            )
            deadline = time.monotonic() + max(0.001, float(timeout_seconds))
            while time.monotonic() < deadline:
                current = service.get_job(job_id)
                if current is None:
                    raise RuntimeError("诊断作业在调度账本中丢失")
                state = str(current.get("state") or "")
                if state == "succeeded":
                    if not os.path.isfile(save_path) or os.path.getsize(save_path) <= 0:
                        raise RuntimeError("诊断作业成功但未取得非空下载产物")
                    return {
                        "generate_test": True,
                        "completed": True,
                        "mode": mode,
                        "job_id": job_id,
                    }
                if state in ("failed", "cancelled"):
                    error = current.get("error_json") or {}
                    raise RuntimeError(
                        f"诊断作业 {state}：{error.get('error_code') or ''} "
                        f"{error.get('reason') or ''}".strip()
                    )
                time.sleep(max(0.001, float(poll_interval)))
            # 超时只请求取消，不直接释放可能已 POST 的槽；取消未确认仍由统一核对收口。
            cancel_requested = False
            try:
                service.cancel_job(
                    job_id, transport_factory=dispatch_runtime.transport_factory
                )
                cancel_requested = True
            except Exception:  # noqa: BLE001 - 诊断超时不能拖垮请求
                logger.warning(
                    "qualification diagnostic cancel failed: %s", job_id, exc_info=True
                )
            return {
                "generate_test": False,
                "generate_submitted": state not in ("queued",),
                "pending": True,
                "cancel_requested": cancel_requested,
                "job_id": job_id,
                "mode": mode,
            }
        finally:
            if image_path:
                try:
                    os.unlink(image_path)
                except OSError:
                    pass

    return run


__all__ = ["build_qualification_test_runner"]
