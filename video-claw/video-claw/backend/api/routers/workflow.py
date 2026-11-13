import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from api.dependencies import workflow_engine
from api.media_paths import resolve_uploaded_document
from api.routers.files import merge_uploaded_file_into_idea
from api.schemas.project import InterventionRequest, ProjectStartRequest
from api.services.project_helpers import (
    make_cancellation,
    make_progress_channel,
    stream_workflow_task,
)
from config import settings
from core.artifact_publisher import PublicationError
from core.identity import CurrentUser, get_current_user
from core.media_refs import MediaInputError, MediaNotFound, canonicalize_media_payload, canonicalize_media_value
from core.orchestrator import validate_upload_identifier
from core.ownership import assert_session_owner
from db.repositories.commits import CommitConflict

router = APIRouter(tags=["Workflow"])


async def current_owner_session(
    session_id: str, user: CurrentUser = Depends(get_current_user)
) -> CurrentUser:
    await run_in_threadpool(assert_session_owner, workflow_engine, session_id, user)
    return user


async def _engine_call(fn, *args, **kwargs):
    """引擎写入走线程池（DB/MinIO I/O 不占用事件循环），并把提交类错误映射为 HTTP。"""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, "Session not found") from exc
    except PublicationError as exc:
        raise HTTPException(400, detail=f"媒体引用无法发布: {exc}") from exc
    except CommitConflict as exc:
        raise HTTPException(409, detail=f"会话已被并发修改，请刷新后重试: {exc}") from exc


def _canonical_payload(body: dict, session_id: str, user: CurrentUser) -> dict:
    try:
        return canonicalize_media_payload(body, user, session_id)
    except MediaNotFound as exc:
        raise HTTPException(404, detail=f"媒体不属于当前会话或用户: {exc}") from exc
    except MediaInputError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

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
    required = REQUIRED_MODEL_FIELDS
    mode = str(values.get("video_generation_mode") or "first_frame")
    if mode == "reference":
        # reference 模式参考图阶段跳过（不生成分镜图），图生图模型非必需；
        # 文生图仍必需（角色/场景设计依赖）。本函数无阶段参数，故按会话模式放行。
        required = tuple(field for field in REQUIRED_MODEL_FIELDS if field != "image_it2i_model")
    missing = [field for field in required if not values.get(field)]
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

# 视频任务能力预检的严格推导集合（video-parameter-mapping D2）：取主流程唯一可能下发的 task。
# t2va 不纳入——缺首帧图/参考素材时由视频阶段前置失败（missing_input_image /
# missing_reference_images），主流程不会下发 t2va；t2va 仅对沙盒/ Pipeline /连通测试等直调入口可达。
VIDEO_MODE_TO_TASKS = {
    "reference": ("ref2va",),
    "start_end_frame": ("fl2va",),
    "first_frame": ("fl2va",),
}

_VIDEO_MODE_LABELS = {
    "reference": "参考图生视频",
    "start_end_frame": "首尾帧生视频",
    "first_frame": "首帧生视频",
}


def _preflight_video_task_capability(stage: str, field: str, model: str, values: dict) -> None:
    """视频任务能力预检：推导会话模式在主流程中可能下发的 task，与模型声明集合比对。

    只读本地配置与会话参数，MUST NOT 发起网络探测、连通性检查或任何生成类调用（design D2）。
    判定顺序 MUST 为先授权后交集：显式授权降级（allow_frame_as_reference）优先于交集判定。
    未声明 tasks 或非自定义模型一律放行（行为与变更前一致）。
    """
    from models.config_model import resolve_model_entry
    from models.custom_common import frame_as_reference_enabled, parse_task_decl

    kind, metadata = resolve_model_entry(model)
    if kind != "custom":
        return
    caps = metadata.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    state, declared = parse_task_decl(caps)
    if state == "missing":
        return
    if state == "invalid":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_unavailable",
                "stage": stage,
                "field": field,
                "model": model,
                "reason": (
                    f"模型 {model} 的 capabilities.tasks 声明无效：解析后没有任何合法值"
                    f"（当前声明：{caps.get('tasks')!r}）；合法取值为 t2va / fl2va / ref2va，请修正后重试"
                ),
                "error_code": "tasks_invalid",
            },
        )
    mode = values.get("video_generation_mode") or "first_frame"
    if mode != "reference" and frame_as_reference_enabled(caps) and "ref2va" in declared:
        # 先授权后交集：降级已被显式授权，不得以“交集为空”为由拒绝（tasks:['t2va','ref2va'] 场景）
        return
    expected = VIDEO_MODE_TO_TASKS.get(mode, VIDEO_MODE_TO_TASKS["first_frame"])
    if set(expected) & set(declared):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "model_unavailable",
            "stage": stage,
            "field": field,
            "model": model,
            "reason": (
                f"会话选择{_VIDEO_MODE_LABELS.get(mode, mode)}，需要任务 {'/'.join(expected)}，"
                f"而模型声明的 capabilities.tasks 仅支持 {'/'.join(declared)}。"
                "请任选其一：① 改选与实例匹配的生成方式；"
                "② 修正模型的 capabilities.tasks（对应推理服务的 --task-type）；"
                "③ 若要以参考图语义使用首/尾帧图，在模型 capabilities 显式声明 allow_frame_as_reference: true"
            ),
            "error_code": "task_not_supported",
        },
    )


def _preflight_stage_models(stage: str, values: dict) -> None:
    """阶段执行前的模型可用性预检；不可用时返回 409 结构化错误（不启动生成）。

    范围声明：本预检只判定**配置层面**的可用性（未注册 / 供应商缺失或不完整 / 凭据未配），
    MUST NOT 被当成实时连通性检查——模型配置齐备但服务不可达时预检会放行，
    该类故障由运行期逐条失败分类呈现（openspec: video-stage-model-unavailable-fallback design D5）。
    """
    if stage == "reference_generation" and str(values.get("video_generation_mode") or "first_frame") == "reference":
        # reference 模式下参考图阶段跳过（reference-mode-skip-scene-images）：不校验图像模型字段
        return
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

    if stage == "video_generation":
        # 任务能力预检（video-parameter-mapping）：只读本地配置，位于可用性判定之后。
        # 插入点：`reference_generation` 的模式放行分支（reference-mode-skip-scene-images）
        # 应在下面的视频分支之外单独追加，两个变更互不覆盖。
        for field, model in field_pairs:
            _preflight_video_task_capability(stage, field, model, values)


@router.post("/api/project/start")
async def start_project(req: ProjectStartRequest, user: CurrentUser = Depends(get_current_user)):
    script_source = (req.script_source or "idea").strip().lower()
    if script_source not in {"idea", "upload"}:
        raise HTTPException(400, detail="script_source 必须是 idea 或 upload")
    if script_source == "upload":
        if not req.file_path:
            raise HTTPException(400, detail="导入剧本必须上传文件")
        # 创建入口与消费入口共用同一守卫：只接受 TEMP_DIR 内、本人 owner 前缀下的文档
        try:
            resolve_uploaded_document(str(req.file_path), user.owner_key)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        # 导入模式：文件内容由剧本阶段保真解析，不拼进 idea
        final_idea = req.idea or ""
    else:
        try:
            final_idea = merge_uploaded_file_into_idea(req.idea, req.file_path, user.owner_key)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
    _require_model_fields(req.model_dump())

    session_id = str(int(time.time() * 1000))
    meta = {
        "idea": final_idea,
        "user_textbox_input": req.idea,
        "script_source": script_source,
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
    if script_source == "upload":
        meta["file_path"] = req.file_path
    from core.language import resolve_language

    if req.language:
        resolved = resolve_language(explicit=req.language)
    elif script_source == "idea":
        resolved = resolve_language(instruction=req.idea or "", body=final_idea)
    else:
        resolved = None
    if resolved:
        meta["language"] = resolved["language"]
        meta["language_source"] = resolved["source"]
    if meta.get("audio_reference_url"):
        try:
            meta["audio_reference_url"] = str(
                canonicalize_media_value(str(meta["audio_reference_url"]), user)
            )
        except MediaNotFound as exc:
            raise HTTPException(404, detail=f"音频参考不属于当前用户: {exc}") from exc
        except MediaInputError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
    meta["video_model"] = _active_video_model(meta)
    session = await _engine_call(workflow_engine.create_session, session_id, meta, owner=user.login)

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
async def execute_stage(
    session_id: str, stage: str, request: Request, user: CurrentUser = Depends(current_owner_session)
):
    try:
        validate_upload_identifier(session_id, "session_id")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = _canonical_payload(body if isinstance(body, dict) else {}, session_id, user)

    state, input_data = await _engine_call(workflow_engine.prepare_stage_execution, session_id, stage, body)
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
async def get_project_status(session_id: str, user: CurrentUser = Depends(current_owner_session)):
    snapshot = await run_in_threadpool(workflow_engine.get_status_snapshot, session_id)
    if not snapshot:
        raise HTTPException(404, "Session not found")
    return snapshot


@router.get("/api/project/{session_id}/status/from_disk")
async def get_project_status_from_disk(
    session_id: str, user: CurrentUser = Depends(current_owner_session)
):
    # 兼容旧前端路由名；实际读取统一走 WorkflowEngine 的 DB 读模型。
    snapshot = await run_in_threadpool(workflow_engine.get_status_snapshot, session_id)
    if not snapshot:
        raise HTTPException(404, "Session not found")
    return snapshot


@router.get("/api/project/{session_id}/artifact/{stage}")
async def get_artifact(
    session_id: str, stage: str, user: CurrentUser = Depends(current_owner_session)
):
    artifact = await _engine_call(workflow_engine.get_artifact_snapshot, session_id, stage)

    if artifact is not None:
        return {"stage": stage, "artifact": artifact}

    raise HTTPException(404, f"Artifact for stage '{stage}' not found")


@router.patch("/api/project/{session_id}/models")
async def update_models(
    session_id: str, request: Request, user: CurrentUser = Depends(current_owner_session)
):
    body = await request.json()
    body = _canonical_payload(body if isinstance(body, dict) else {}, session_id, user)
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
    return await _engine_call(
        workflow_engine.update_session_meta, session_id, body if isinstance(body, dict) else {}, allowed_keys,
    )


@router.post("/api/project/{session_id}/artifact/{stage}/upload_image")
async def upload_artifact_image(
    session_id: str,
    stage: str,
    item_type: str = Form(...),
    item_id: str = Form(...),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(current_owner_session),
):
    """Upload a user-provided image into a stage artifact and commit it."""
    try:
        return await _engine_call(
            workflow_engine.upload_artifact_image,
            session_id=session_id,
            stage=stage,
            item_type=item_type,
            item_id=item_id,
            file_obj=file.file,
            filename=file.filename or "",
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, detail=str(exc)) from exc


@router.patch("/api/project/{session_id}/artifact/{stage}")
async def update_artifact(
    session_id: str, stage: str, request: Request, user: CurrentUser = Depends(current_owner_session)
):
    """保存用户在某阶段的选择/修改：同一事务提交到 DB，并刷新读模型缓存。"""
    body = await request.json()
    body = _canonical_payload(body if isinstance(body, dict) else {}, session_id, user)
    return await _engine_call(
        workflow_engine.update_artifact, session_id, stage, body if isinstance(body, dict) else {},
    )


@router.post("/api/project/{session_id}/intervene")
async def intervene(
    session_id: str,
    req: InterventionRequest,
    request: Request,
    user: CurrentUser = Depends(current_owner_session),
):
    modifications = _canonical_payload(req.modifications, session_id, user)
    state, input_data = await _engine_call(
        workflow_engine.prepare_intervention_execution,
        session_id=session_id,
        stage=req.stage,
        modifications=modifications,
    )
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
            intervention=modifications,
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
async def continue_workflow(
    session_id: str, request: Request, user: CurrentUser = Depends(current_owner_session)
):
    # 可选 body `{"stage": "..."}`：从哪个阶段点确认就只确认哪个阶段；无 body / 非 JSON 时保持旧行为
    stage = None
    if (request.headers.get("content-type") or "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 请求体不可解析不是错误：按旧行为继续
            body = None
        if isinstance(body, dict) and isinstance(body.get("stage"), str):
            stage = body["stage"].strip() or None
    if not await run_in_threadpool(workflow_engine.get_status_snapshot, session_id):
        raise HTTPException(404, "Session not found")
    # 与其他写入端点同一错误口径：提交冲突不能以 500 泄给前端（否则无法区分“未确认”与“服务异常”）
    try:
        return await workflow_engine.continue_workflow(session_id, stage)
    except CommitConflict as exc:
        raise HTTPException(409, detail=f"会话已被并发修改，请刷新后重试: {exc}") from exc


@router.post("/api/project/{session_id}/stop")
async def stop_project(
    session_id: str, user: CurrentUser = Depends(current_owner_session)
):
    await _engine_call(workflow_engine.stop_session, session_id)
    return {"status": "stopped", "session_id": session_id}


@router.get("/api/project/{session_id}/scene/{scene_number}/assets")
async def check_scene_assets(
    session_id: str, scene_number: int, user: CurrentUser = Depends(current_owner_session)
):
    return await _engine_call(workflow_engine.get_scene_asset_counts, session_id, scene_number)
