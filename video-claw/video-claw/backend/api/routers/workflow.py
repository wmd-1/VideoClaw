import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from api.dependencies import workflow_engine
from api.routers.files import merge_uploaded_file_into_idea
from api.schemas.project import InterventionRequest, ProjectStartRequest
from api.services.project_helpers import (
    make_cancellation,
    make_progress_channel,
    stream_workflow_task,
)
from config import settings
from core.orchestrator import validate_upload_identifier

router = APIRouter(tags=["Workflow"])

REQUIRED_MODEL_FIELDS = (
    "llm_model",
    "vlm_model",
    "image_t2i_model",
    "image_it2i_model",
)

VIDEO_MODE_TO_MODEL_FIELD = {
    "first_frame": "video_first_frame_model",
    "start_end_frame": "video_start_end_model",
    "reference": "video_reference_model",
}


_DEFAULT_VIDEO_MODEL_ATTR = {
    "video_first_frame_model": "VIDEO_FIRST_FRAME_MODEL",
    "video_start_end_model": "VIDEO_START_END_MODEL",
    "video_reference_model": "VIDEO_REFERENCE_MODEL",
}


def _video_mode_field(values: dict) -> str:
    """当前 video_generation_mode 对应的模型字段名（唯一的 mode→field 映射使用点）。"""
    mode = values.get("video_generation_mode") or "first_frame"
    return VIDEO_MODE_TO_MODEL_FIELD.get(mode, "video_first_frame_model")


def _active_video_model_field(values: dict) -> str:
    """_active_video_model 实际取到值的字段名。

    取值顺序与 _active_video_model 严格一致（含 legacy `video_model` 回退），
    因此预检报错的 field 一定等于本次真正会使用的模型来源（避免“检 A 用 B”）。
    """
    field = _video_mode_field(values)
    if not values.get(field) and values.get("video_model"):
        return "video_model"
    return field


def _active_video_model(values: dict) -> str:
    model_field = _video_mode_field(values)
    default_attr = _DEFAULT_VIDEO_MODEL_ATTR.get(model_field, "VIDEO_MODEL")
    # Legacy session compatibility: fall back to the old single video_model field when mode-specific fields are absent.
    return values.get(model_field) or values.get("video_model") or getattr(settings, default_attr, "") or ""


def _require_model_fields(values: dict) -> None:
    missing = [field for field in REQUIRED_MODEL_FIELDS if not values.get(field)]
    if not _active_video_model(values):
        missing.append("video_model")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Missing required model configuration: {', '.join(missing)}"
                "（默认模型未配置：请在「设置 → Default Models」选择，或在开始生成页重新指定）"
            ),
        )


# 需要图像模型可用性预检的阶段（模型字段，按阶段）。
# 第 5 阶段（video_generation）的字段随 video_generation_mode 变化，在 _preflight_stage_models 内
# 复用 _active_video_model_field / _active_video_model 解析，不在此重复列一份映射。
PREFLIGHT_STAGE_MODEL_FIELDS = {
    "character_design": ("image_t2i_model",),
    "reference_generation": ("image_t2i_model", "image_it2i_model"),
}


def _preflight_stage_models(stage: str, values: dict) -> None:
    """阶段执行前的模型可用性预检；不可用时返回 409 结构化错误（不启动生成）。

    范围声明：本预检只判定**配置层面**的可用性（未注册 / 供应商缺失或不完整 / 凭据未配），
    MUST NOT 被当成实时连通性检查——模型配置齐备但服务不可达时预检会放行，
    该类故障由运行期逐条失败分类呈现（openspec: video-stage-model-unavailable-fallback design D5）。
    """
    if stage == "video_generation":
        # 只校验本次生成方式实际会用的那一个视频模型（含 legacy 回退后的真实来源）
        field_pairs = ((_active_video_model_field(values), _active_video_model(values)),)
    else:
        fields = PREFLIGHT_STAGE_MODEL_FIELDS.get(stage)
        if not fields:
            return
        field_pairs = tuple((field, values.get(field) or "") for field in fields)
    from models.config_model import model_availability

    for field, model in field_pairs:
        availability = model_availability(model)
        if not availability["available"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "model_unavailable",
                    "stage": stage,
                    "field": field,
                    "model": model,
                    "reason": availability["reason"],
                    "error_code": availability["code"],
                },
            )


@router.post("/api/project/start")
async def start_project(req: ProjectStartRequest):
    try:
        final_idea = merge_uploaded_file_into_idea(req.idea, req.file_path)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    _require_model_fields(req.model_dump())

    session_id = str(int(time.time() * 1000))
    meta = {
        "idea": final_idea,
        "user_textbox_input": req.idea,
        "style": req.style or getattr(settings, "STYLE", None) or "realistic",
        "video_ratio": req.video_ratio or "9:16",
        "video_resolution": req.video_resolution or "720P",
        "video_duration": req.video_duration,
        "video_fps": req.video_fps,
        "video_short_edge": req.video_short_edge,
        "audio_reference_url": req.audio_reference_url,
        "expand_idea": req.expand_idea if req.expand_idea is not None else True,
        "llm_model": req.llm_model,
        "vlm_model": req.vlm_model,
        "image_t2i_model": req.image_t2i_model,
        "image_it2i_model": req.image_it2i_model,
        # Legacy request/session compatibility: clients created before the split only send video_model.
        "video_first_frame_model": req.video_first_frame_model or req.video_model or getattr(settings, "VIDEO_FIRST_FRAME_MODEL", ""),
        "video_start_end_model": req.video_start_end_model or getattr(settings, "VIDEO_START_END_MODEL", ""),
        "video_reference_model": req.video_reference_model or getattr(settings, "VIDEO_REFERENCE_MODEL", ""),
        "video_generation_mode": req.video_generation_mode or getattr(settings, "VIDEO_GENERATION_MODE", "first_frame"),
        "video_model": req.video_model,
        "enable_concurrency": req.enable_concurrency if req.enable_concurrency is not None else True,
        "web_search": req.web_search if req.web_search is not None else False,
        "episodes": req.episodes if req.episodes is not None else 4,
    }
    meta["video_model"] = _active_video_model(meta)
    session = workflow_engine.create_session(session_id, meta)

    return {
        "session_id": session_id,
        "status": session["status"],
        "params": {
            "idea": final_idea,
            "file_path": req.file_path,
            "style": req.style,
            "llm_model": meta["llm_model"],
            "vlm_model": meta["vlm_model"],
            "image_t2i_model": meta["image_t2i_model"],
            "image_it2i_model": meta["image_it2i_model"],
            "video_first_frame_model": meta["video_first_frame_model"],
            "video_start_end_model": meta["video_start_end_model"],
            "video_reference_model": meta["video_reference_model"],
            "video_generation_mode": meta["video_generation_mode"],
            "video_model": meta["video_model"],
            "episodes": meta["episodes"],
            "video_ratio": meta["video_ratio"],
            "video_resolution": meta["video_resolution"],
        }
    }


@router.post("/api/project/{session_id}/execute/{stage}")
async def execute_stage(session_id: str, stage: str, request: Request):
    try:
        validate_upload_identifier(session_id, "session_id")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    try:
        body = await request.json()
    except Exception:
        body = {}

    state, input_data = workflow_engine.prepare_stage_execution(session_id, stage, body)
    _require_model_fields(input_data)
    _preflight_stage_models(stage, input_data)

    cancellation_check, on_disconnect = make_cancellation(workflow_engine, session_id)
    progress_events, event_trigger, progress_callback = make_progress_channel()

    return StreamingResponse(
        stream_workflow_task(
            request=request,
            workflow_engine=workflow_engine,
            state=state,
            stage=stage,
            input_data=input_data,
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
            progress_events=progress_events,
            event_trigger=event_trigger,
            include_payload_summary=True,
            on_disconnect=on_disconnect,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/project/{session_id}/status")
async def get_project_status(session_id: str):
    snapshot = workflow_engine.get_status_snapshot(session_id)
    if not snapshot:
        raise HTTPException(404, "Session not found")
    return snapshot


@router.get("/api/project/{session_id}/status/from_disk")
async def get_project_status_from_disk(session_id: str):
    # 兼容旧前端路由名；实际读取统一走 WorkflowEngine 的内存状态入口。
    snapshot = workflow_engine.get_status_snapshot(session_id)
    if not snapshot:
        raise HTTPException(404, "Session not found")
    return snapshot


@router.get("/api/project/{session_id}/artifact/{stage}")
async def get_artifact(session_id: str, stage: str):
    try:
        artifact = workflow_engine.get_artifact_snapshot(session_id, stage)
    except KeyError:
        raise HTTPException(404, "Session not found")

    if artifact is not None:
        return {"stage": stage, "artifact": artifact}

    raise HTTPException(404, f"Artifact for stage '{stage}' not found")


@router.patch("/api/project/{session_id}/models")
async def update_models(session_id: str, request: Request):
    body = await request.json()
    allowed_keys = (
        "llm_model",
        "vlm_model",
        "image_t2i_model",
        "image_it2i_model",
        "video_model",
        "video_first_frame_model",
        "video_start_end_model",
        "video_reference_model",
        "video_generation_mode",
        "video_ratio",
        "video_resolution",
        "video_duration",
        "video_fps",
        "video_short_edge",
        "audio_reference_url",
        "style",
        "enable_concurrency",
    )
    try:
        return workflow_engine.update_session_meta(session_id, body if isinstance(body, dict) else {}, allowed_keys)
    except KeyError:
        raise HTTPException(404, "Session not found")


@router.post("/api/project/{session_id}/artifact/{stage}/upload_image")
async def upload_artifact_image(
    session_id: str,
    stage: str,
    item_type: str = Form(...),
    item_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload a user-provided image into a stage artifact and persist the session."""
    try:
        return workflow_engine.upload_artifact_image(
            session_id=session_id,
            stage=stage,
            item_type=item_type,
            item_id=item_id,
            file_obj=file.file,
            filename=file.filename or "",
        )
    except KeyError:
        raise HTTPException(404, "Session not found")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, detail=str(exc)) from exc


@router.patch("/api/project/{session_id}/artifact/{stage}")
async def update_artifact(session_id: str, stage: str, request: Request):
    """保存用户在某阶段的选择/修改，同时更新内存状态和磁盘快照。"""
    body = await request.json()
    try:
        return workflow_engine.update_artifact(session_id, stage, body if isinstance(body, dict) else {})
    except KeyError:
        raise HTTPException(404, "Session not found")


@router.post("/api/project/{session_id}/intervene")
async def intervene(session_id: str, req: InterventionRequest, request: Request):
    try:
        state, input_data = workflow_engine.prepare_intervention_execution(
            session_id=session_id,
            stage=req.stage,
            modifications=req.modifications,
        )
    except KeyError:
        raise HTTPException(404, "Session not found")
    _preflight_stage_models(req.stage, input_data)

    cancellation_check, on_disconnect = make_cancellation(workflow_engine, session_id)
    progress_events, event_trigger, progress_callback = make_progress_channel()

    return StreamingResponse(
        stream_workflow_task(
            request=request,
            workflow_engine=workflow_engine,
            state=state,
            stage=req.stage,
            input_data=input_data,
            cancellation_check=cancellation_check,
            progress_callback=progress_callback,
            progress_events=progress_events,
            event_trigger=event_trigger,
            intervention=req.modifications,
            on_disconnect=on_disconnect,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/project/{session_id}/continue")
async def continue_workflow(session_id: str):
    if not workflow_engine.get_status_snapshot(session_id):
        raise HTTPException(404, "Session not found")
    return await workflow_engine.continue_workflow(session_id)


@router.post("/api/project/{session_id}/stop")
async def stop_project(session_id: str):
    workflow_engine.stop_session(session_id)
    return {"status": "stopped", "session_id": session_id}


@router.get("/api/project/{session_id}/scene/{scene_number}/assets")
async def check_scene_assets(session_id: str, scene_number: int):
    try:
        return workflow_engine.get_scene_asset_counts(session_id, scene_number)
    except KeyError:
        raise HTTPException(404, "Session not found")
