import logging
from typing import Any, Optional

from models.config_model import (
    list_api_models,
    parse_api_model,
    video_capabilities,
)
from models.image_client import ImageClient
from core.observability import current_call_context
from core.video_dispatch_runtime import get_dispatch_runtime
from core.video_generation_service import resolve_managed_instance
from core.video_managed_bridge import ManagedBridgeError, managed_video_sync
from pipelines.storage import record_video_job
from models.video_client import VideoClient

logger = logging.getLogger(__name__)


def list_api_workflows(
    media_type: Optional[str] = None,
    required_adapter_abilities: Optional[list[str]] = None,
    verified_only: bool = False,
) -> list[dict[str, Any]]:
    return list_api_models(
        media_type=media_type,
        required_adapter_abilities=required_adapter_abilities,
        verified_only=verified_only,
    )


def parse_api_workflow(workflow: str, media_type: str) -> tuple[str, str]:
    return parse_api_model(workflow, media_type)


def normalize_video_duration(provider: str, model: str, duration: int) -> int:
    contract = video_capabilities(provider, model).get("duration") or {}
    if contract.get("verified"):
        return min(max(int(duration), int(contract.get("min", duration))), int(contract.get("max", duration)))
    if provider == "dashscope":
        return 10 if duration >= 8 else 5
    if provider == "seedance" or "seedance" in (model or "").lower():
        return min(max(duration, 5), 10)
    return max(duration, 1)


def generate_image_api(
    *,
    prompt: str,
    model: str,
    output_dir: str,
    task_id: str,
    image_paths: Optional[list[str]] = None,
    video_ratio: str = "9:16",
    resolution: str = "1080P",
) -> str:
    _, resolved_model = parse_api_workflow(model, "image")
    logger.info(
        "Generating API image: model=%s refs=%d ratio=%s resolution=%s",
        resolved_model,
        len(image_paths or []),
        video_ratio,
        resolution,
    )
    paths = ImageClient().generate_image(
        prompt=prompt,
        image_paths=image_paths,
        model=resolved_model,
        save_dir=output_dir,
        session_id=task_id,
        video_ratio=video_ratio,
        resolution=resolution,
    )
    if not paths:
        raise RuntimeError(f"Image API returned no result for model={resolved_model}")
    return paths[0]


def generate_video_api(
    *,
    prompt: str,
    model: str,
    output_path: str,
    image_path: Optional[str] = None,
    duration: int = 5,
    video_ratio: str = "9:16",
    video_resolution: Optional[str] = None,
    **params,
) -> str:
    dispatch_mode = str(params.pop("video_dispatch_mode", "manual") or "manual").lower()
    requested_profile = str(params.pop("video_profile_id", "") or "")
    selected_instance = None
    effective_profile = requested_profile
    if dispatch_mode == "auto":
        runtime = get_dispatch_runtime()
        if runtime is None:
            raise ManagedBridgeError("dispatch_not_started", "视频调度运行时尚未启动")
        reference_paths = params.get("reference_image_paths") or []
        reference_videos = params.get("reference_video_paths") or []
        mode = (
            "start_end_frame"
            if image_path and params.get("last_image_path")
            else ("first_frame" if image_path else ("reference" if reference_paths or reference_videos else "t2va"))
        )
        resolution = runtime.service.resolve_execution_pool(
            mode=mode,
            explicit_profile_id=requested_profile or None,
            parameters={
                "duration": duration,
                "ratio": video_ratio,
                "resolution": video_resolution,
            },
        )
        if resolution.error:
            raise ManagedBridgeError(resolution.error, "没有支持当前生成方式的就绪资源池")
        effective_profile = str(resolution.contract.profile_id or "")
        pool_runtime = runtime.service.runtime()
        for member in resolution.members:
            instance = pool_runtime.instances.get(member)
            if instance is not None and instance.enabled:
                selected_instance = instance.instance_id
                model = instance.model_id
                break
        if not selected_instance:
            raise ManagedBridgeError("pool_not_ready", "资源池暂无可用实例")
    if not str(model or "").strip():
        raise ManagedBridgeError("model_required", "手动模式必须选择视频模型")
    provider, resolved_model = parse_api_workflow(model, "video")
    safe_duration = normalize_video_duration(provider, resolved_model, int(duration))
    logger.info(
        "Generating API video: provider=%s model=%s duration=%ss ratio=%s output=%s",
        provider or "unknown",
        resolved_model,
        safe_duration,
        video_ratio,
        output_path,
    )
    managed = (
        resolve_managed_instance(resolved_model, selected_instance)
        if selected_instance
        else resolve_managed_instance(resolved_model)
    )
    if managed is not None:
        context = current_call_context()
        owner = str(context.get("owner") or "").strip()
        execution_id = str(context.get("execution_id") or "").strip()
        if not owner or not execution_id:
            raise ManagedBridgeError(
                "trusted_context_required",
                "Pipeline 调用受管视频模型缺少可信 owner/执行上下文",
            )
        reference_image_paths = params.pop("reference_image_paths", None)
        reference_video_paths = params.pop("reference_video_paths", None)
        last_image_path = params.pop("last_image_path", None)
        if last_image_path and not image_path:
            raise ManagedBridgeError(
                "invalid_input",
                "仅尾帧缺少首帧，Pipeline 受管视频不能按 first_frame 静默降级",
            )
        mode = (
            "start_end_frame"
            if image_path and last_image_path
            else (
                "first_frame"
                if image_path
                else (
                    "reference"
                    if reference_image_paths or reference_video_paths
                    else "t2va"
                )
            )
        )
        bridged = managed_video_sync(
            model=resolved_model,
            prompt=prompt,
            save_path=output_path,
            owner=owner,
            session_id=execution_id,
            execution_id=execution_id,
            mode=mode,
            duration=safe_duration,
            video_ratio=video_ratio,
            resolution=video_resolution or params.pop("resolution", None),
            image_path=image_path,
            last_image_path=last_image_path,
            reference_image_paths=reference_image_paths,
            reference_video_paths=reference_video_paths,
            profile_id=effective_profile or None,
            instance_id=selected_instance,
            on_job_created=lambda job_id: record_video_job(execution_id, job_id),
        )
        record_video_job(execution_id, str(bridged.get("job_id") or ""))
        return output_path
    VideoClient().generate_video(
        prompt=prompt,
        image_path=image_path,
        save_path=output_path,
        model=resolved_model,
        duration=safe_duration,
        video_ratio=video_ratio,
        resolution=video_resolution or params.pop("resolution", None),
        **params,
    )
    return output_path
