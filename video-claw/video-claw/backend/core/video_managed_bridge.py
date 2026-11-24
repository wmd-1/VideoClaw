"""4.8：沙盒/Pipeline/模型测试的受管同步桥；统一走 DispatchService 共享槽。"""
from __future__ import annotations

import os
import uuid
from typing import Any, Callable, Dict, List, Optional

from config import Config
from core.identity import owner_key
from core.video_dispatch_runtime import get_dispatch_runtime
from core.video_dispatch_service import DispatchUnavailable
from core.video_generation_service import BatchVideoItem, resolve_managed_instance


class ManagedBridgeError(RuntimeError):
    def __init__(self, error_code: str, reason: str) -> None:
        super().__init__(reason)
        self.error_code = error_code
        self.reason = reason


def is_managed_video_model(model: str) -> bool:
    try:
        return resolve_managed_instance(str(model or "")) is not None
    except Exception:  # noqa: BLE001 - 多实例未指定等由 bridge 给出明确错误
        return True


def _profile_for_instance(runtime, instance_id: str, mode: str) -> Optional[str]:
    for profile_id, profile in runtime.profiles.items():
        if (
            profile.enabled
            and instance_id in profile.members
            and mode in profile.contracts
        ):
            return profile_id
    return None


def managed_video_sync(
    *,
    model: str,
    prompt: str,
    save_path: str,
    owner: str,
    session_id: str,
    execution_id: str,
    mode: str,
    duration: int = 5,
    video_ratio: str = "16:9",
    resolution: Optional[str] = None,
    fps: Optional[int] = None,
    short_edge: Optional[int] = None,
    image_path: Optional[str] = None,
    last_image_path: Optional[str] = None,
    reference_image_paths: Optional[List[str]] = None,
    reference_video_paths: Optional[List[str]] = None,
    audio_reference_url: Optional[str] = None,
    profile_id: Optional[str] = None,
    instance_id: Optional[str] = None,
    wait_timeout: Optional[float] = None,
    on_job_created: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """同步等待一个受管片段；调用方负责在线程池中执行。"""
    dispatch_runtime = get_dispatch_runtime()
    if dispatch_runtime is None:
        raise ManagedBridgeError(
            "dispatch_not_started", "视频资源池调度运行时尚未启动"
        )
    try:
        managed = (
            resolve_managed_instance(model, instance_id)
            if instance_id
            else resolve_managed_instance(model)
        )
    except Exception as exc:  # noqa: BLE001 - 草稿/多实例均不能绕过
        raise ManagedBridgeError("managed_instance_invalid", str(exc)) from exc
    if managed is None:
        raise ManagedBridgeError(
            "not_managed_model", f"模型 {model} 不是受管视频模型"
        )

    runtime = dispatch_runtime.service.runtime()
    if not runtime.dispatch_enabled:
        raise ManagedBridgeError(
            "dispatch_disabled", "资源池调度已由运维停用"
        )
    instance_id = str(managed.get("instance_id") or instance_id or "")
    selected_profile = str(profile_id or "") or _profile_for_instance(
        runtime, instance_id, mode
    )
    profile = runtime.profiles.get(str(selected_profile or ""))
    if (
        profile is None
        or not profile.enabled
        or instance_id not in profile.members
        or mode not in profile.contracts
    ):
        reason = (
            "该池未声明首尾帧契约"
            if mode == "start_end_frame"
            else f"池 {selected_profile or '-'} 不包含实例 {instance_id} 或不支持 {mode}"
        )
        raise ManagedBridgeError("pool_incompatible", reason)
    resolution_obj = dispatch_runtime.service.resolve_execution_pool(
        mode=mode,
        explicit_profile_id=selected_profile,
        parameters={
            "duration": duration,
            "ratio": video_ratio,
            "resolution": resolution,
            "fps": fps,
            "short_edge": short_edge,
        },
        runtime=runtime,
    )
    if resolution_obj.error:
        raise ManagedBridgeError(
            resolution_obj.error, "所选资源池不可用或与新执行不兼容"
        )

    os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
    item = BatchVideoItem(
        item_id=str(uuid.uuid4().hex),
        prompt=prompt,
        save_path=save_path,
        image_path=image_path,
        last_image_path=last_image_path,
        reference_image_paths=reference_image_paths,
        reference_video_paths=reference_video_paths,
        duration=max(1, int(duration)),
        video_ratio=video_ratio,
        resolution=resolution,
        fps=fps,
        short_edge=short_edge,
        audio_reference_url=audio_reference_url,
    )
    batch_id = f"{execution_id}-{uuid.uuid4().hex[:8]}"
    try:
        handle = dispatch_runtime.generation.submit(
            owner=owner_key(owner),
            session_id=session_id,
            execution_id=execution_id,
            batch_id=batch_id,
            contract=resolution_obj.contract,
            members=list(resolution_obj.members),
            items=[item],
            enable_concurrency=False,
            max_concurrency_per_execution=1,
            queue_timeout_seconds=runtime.queue_timeout_seconds,
            max_queued_jobs=runtime.max_queued_jobs,
        )
    except DispatchUnavailable as exc:
        raise ManagedBridgeError(
            str(getattr(exc, "error_code", "dispatch_unavailable")), str(exc)
        ) from exc
    job_id = str(handle.job_ids.get(item.item_id, "") or "")
    if job_id and on_job_created is not None:
        try:
            on_job_created(job_id)
        except Exception:  # noqa: BLE001 - 观测回调不得影响派发
            pass
    timeout = wait_timeout
    if timeout is None:
        timeout = (
            float(runtime.queue_timeout_seconds)
            + float(getattr(Config, "TIMEOUT_VIDEO", 10800) or 10800)
            + 600.0
        )
    results = dispatch_runtime.generation.wait(
        handle, timeout=timeout
    )
    result = results.get(item.item_id) or {}
    if result.get("state") != "succeeded":
        error = result.get("error") or {}
        raise ManagedBridgeError(
            str(error.get("error_code") or "video_dispatch_failed"),
            str(error.get("reason") or error.get("error") or "受管视频生成失败"),
        )
    return {
        "save_path": result.get("save_path") or save_path,
        "job_id": job_id,
        "profile_id": resolution_obj.contract.profile_id,
        "execution_id": execution_id,
    }


__all__ = [
    "ManagedBridgeError",
    "is_managed_video_model",
    "managed_video_sync",
]
