import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from api.schemas.sandbox import (
    SandboxI2IRequest,
    SandboxLLMRequest,
    SandboxT2IRequest,
    SandboxVideoRequest,
    SandboxVLMRequest,
)
from core.identity import CurrentUser, get_current_user
from core.media_refs import canonicalize_media_value, materialize_media_value
from core.object_operations import current_boot_id, register_resource_delete
from core.ownership import is_owner
from core.resource_publisher import ResourcePublisher
from db.repositories import SandboxRepo
from db.repositories.resource_commits import commit_sandbox_record, delete_sandbox_cascade

router = APIRouter(tags=["Sandbox"])
logger = logging.getLogger(__name__)

# 活动任务仍在进程内（D16）；成功记录、文件索引与路径索引只在 DB
SANDBOX_ACTIVE_TASKS: dict[str, dict] = {}
SANDBOX_STAGE = "sandbox"
HISTORY_LIMIT = 1000
SANDBOX_LOCK = threading.RLock()


def _media_input(value: str, field: str, user: CurrentUser) -> Tuple[str, str]:
    """用户媒体入参 → (canonical 持久值, 物化运行值)；记录只存前者。"""
    del field  # validation errors are raised by canonicalize/materialize
    canonical = str(canonicalize_media_value(value, user))
    return canonical, materialize_media_value(canonical, user)


def _media_inputs(values: List[str] | None, field: str, user: CurrentUser) -> Tuple[List[str], List[str]]:
    pairs = [_media_input(item, field, user) for item in (values or []) if str(item or "").strip()]
    return [canonical for canonical, _ in pairs], [runtime for _, runtime in pairs]


def _iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000).isoformat()
    except (TypeError, ValueError):
        return ""


def _record_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(row["record_id"]),
        "owner": str(row.get("owner") or ""),
        "tool": str(row.get("tool") or ""),
        "model": str(row.get("model") or ""),
        "input": row.get("input_json") or {},
        "output": row.get("output_json") or {},
        "files": list(row.get("files_json") or []),
        "created_at": _iso(row.get("created_at")),
    }


def _load_history(owner: str) -> List[dict]:
    return [_record_view(row) for row in SandboxRepo().list(owner, limit=HISTORY_LIMIT)]


def _publisher(record_id: str, user: CurrentUser) -> ResourcePublisher:
    return ResourcePublisher(
        owner=user.login, domain="sandbox", resource_id=record_id, stage=SANDBOX_STAGE, boot_id=current_boot_id(),
    )


def _replace(node: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _replace(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_replace(value, mapping) for value in node]
    if isinstance(node, str) and node in mapping:
        return mapping[node]
    return node


def _commit_record(
    publisher: ResourcePublisher,
    *,
    tool: str,
    model: str,
    input_data: dict,
    output_data: dict,
    media: List[Tuple[str, str]] = (),
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """成功结果：发布本次输出 → 记录 + 文件行 + 路径索引同事务；任一步失败补偿本批对象。"""
    result, rows, mapping = publisher.publish(media)
    output = _replace(output_data, mapping)
    public_files = [str(row["public_path"]) for row in rows]
    try:
        row = commit_sandbox_record(
            record={
                "record_id": publisher.resource_id, "owner": publisher.owner, "tool": tool, "model": model,
                "input": input_data, "output": output, "files": public_files,
            },
            files=rows,
            retained=(result or {}).get("operation_ids") or [],
            paths=public_files,
        )
    except BaseException:
        publisher.abort(result)
        raise
    publisher.mark_committed(result)
    return _record_view(row), mapping


def _start_active_task(tool: str, model: str, input_data: dict, owner: str) -> str:
    with SANDBOX_LOCK:
        task_id = uuid.uuid4().hex
        SANDBOX_ACTIVE_TASKS[task_id] = {
            "id": task_id,
            "owner": str(owner),
            "tool": tool,
            "model": model,
            "input": input_data,
            "status": "running",
            "progress": 1,
            "created_at": datetime.now().isoformat(),
        }
        return task_id


def _finish_active_task(task_id: str) -> None:
    with SANDBOX_LOCK:
        SANDBOX_ACTIVE_TASKS.pop(task_id, None)


def _audio_reference(value: str, user: CurrentUser, record_id: str):
    """音频参考 → 推理可取的 HTTP 地址（D16/D17）：远程/data 原样；受管对象按需预签名；
    legacy/上传文件复制为本记录名下的受管暂存对象再签发。不再拼裸 `/code` 网关地址。

    返回 (canonical 持久值, 推理用 URL, 暂存对象或 None)。
    """
    from core.reference_values import StagingUnavailable, stage_for_http
    from models.media_types import UnsupportedReference, select_for_protocol

    text = (value or "").strip()
    if not text:
        raise ValueError("音频参考为空（audio_url 不能为空字符串）")
    if text.startswith(("http://", "https://", "data:")):
        return text, text, None
    canonical = str(canonicalize_media_value(text, user))
    try:
        ref, staged = stage_for_http(
            canonical, user, domain="sandbox", resource_id=record_id,
            execution_id=record_id, boot_id=current_boot_id(),
        )
        url = select_for_protocol("http_uri", ref)["http_uri"]
    except (StagingUnavailable, UnsupportedReference) as exc:
        raise ValueError(f"音频参考无法生成推理端可访问的地址（需要对象存储可用）: {exc}") from exc
    return canonical, url, staged


def _release(staged) -> None:
    if staged is None:
        return
    from core.reference_values import release_staging

    try:
        release_staging(staged, remote_done=True)
    except Exception:
        logger.warning("sandbox staging release failed: %s", staged, exc_info=True)


# 请求模型
@router.get("/api/sandbox/history")
async def sandbox_get_history(user: CurrentUser = Depends(get_current_user)):
    """获取历史记录列表"""
    history = await run_in_threadpool(_load_history, user.login)
    return {
        "success": True,
        "records": [
            {
                "id": r["id"],
                "tool": r["tool"],
                "model": r["model"],
                "input": r["input"],
                "output": r.get("output"),
                "created_at": r["created_at"],
            }
            for r in history
        ]
    }


@router.get("/api/sandbox/tasks")
async def sandbox_get_active_tasks(user: CurrentUser = Depends(get_current_user)):
    """获取临时工作台正在执行的任务"""
    with SANDBOX_LOCK:
        tasks = [
            task for task in SANDBOX_ACTIVE_TASKS.values()
            if is_owner(task.get("owner"), user)
        ]
    return {"success": True, "tasks": tasks}


@router.get("/api/sandbox/history/{record_id}")
async def sandbox_get_record(record_id: str, user: CurrentUser = Depends(get_current_user)):
    """获取单条历史记录详情"""
    row = await run_in_threadpool(SandboxRepo().get, record_id)
    if row and is_owner(row.get("owner"), user):
        return {"success": True, "record": _record_view(row)}
    return {"success": False, "error": "记录不存在"}


@router.delete("/api/sandbox/history/{record_id}")
async def sandbox_delete_record(record_id: str, user: CurrentUser = Depends(get_current_user)):
    """删除历史记录：同事务登记对象/本地文件清理后删记录（级联文件行与路径索引）。"""
    owner = user.login

    def delete() -> bool:
        return delete_sandbox_cascade(
            record_id, owner,
            lambda cur: register_resource_delete(cur, owner=owner, domain="sandbox", resource_id=record_id),
        )

    if not await run_in_threadpool(delete):
        return {"success": False, "error": "记录不存在"}
    logger.info("Sandbox history deleted (object cleanup queued): record_id=%s", record_id)
    return {"success": True}


@router.post("/api/sandbox/llm")
async def sandbox_llm(
    req: SandboxLLMRequest, user: CurrentUser = Depends(get_current_user)
):
    """临时工作台 - LLM 文字生成"""
    from models.llm_client import LLM
    client = LLM()
    input_data = {"prompt": req.prompt, "web_search": req.web_search}
    task_id = _start_active_task("llm", req.model, input_data, user.login)
    try:
        logger.info("Sandbox LLM started: model=%s web_search=%s", req.model, req.web_search)
        result = await run_in_threadpool(
            client.query,
            req.prompt,
            model=req.model,
            web_search=req.web_search,
        )
        await run_in_threadpool(
            _commit_record, _publisher(task_id, user), tool="llm", model=req.model,
            input_data=input_data, output_data={"response": result},
        )
        logger.info("Sandbox LLM completed: model=%s record_id=%s", req.model, task_id)
        return {"success": True, "result": result, "record_id": task_id}
    except Exception as e:
        logger.exception("Sandbox LLM failed: model=%s", req.model)
        return {"success": False, "error": str(e)}
    finally:
        _finish_active_task(task_id)


@router.post("/api/sandbox/vlm")
async def sandbox_vlm(
    req: SandboxVLMRequest, user: CurrentUser = Depends(get_current_user)
):
    """临时工作台 - VLM 图片理解"""
    from models.vlm_client import VLM
    client = VLM()
    # 图片入参统一守卫（I-A）：本地路径限定白名单，越界在发起请求前拒绝
    try:
        image_inputs, image_paths = _media_inputs(req.images, "图片", user)
    except ValueError as e:
        logger.warning("Sandbox VLM media input rejected: model=%s %s", req.model, e)
        return {"success": False, "error": str(e)}
    input_data = {"prompt": req.prompt, "images": image_inputs}
    task_id = _start_active_task("vlm", req.model, input_data, user.login)
    try:
        logger.info("Sandbox VLM started: model=%s images=%d", req.model, len(image_paths))
        result = await run_in_threadpool(
            client.query,
            req.prompt,
            image_paths=image_paths,
            model=req.model,
        )
        await run_in_threadpool(
            _commit_record, _publisher(task_id, user), tool="vlm", model=req.model,
            input_data=input_data, output_data={"response": result},
        )
        logger.info("Sandbox VLM completed: model=%s record_id=%s", req.model, task_id)
        return {"success": True, "result": result, "record_id": task_id}
    except Exception as e:
        logger.exception("Sandbox VLM failed: model=%s", req.model)
        return {"success": False, "error": str(e)}
    finally:
        _finish_active_task(task_id)


async def _image_tool(
    tool: str, model: str, prompt: str, ratio: Optional[str], input_data: dict,
    image_paths: Optional[List[str]], user: CurrentUser,
) -> dict:
    from models.image_client import ImageClient

    client = ImageClient()
    task_id = _start_active_task(tool, model, input_data, user.login)
    publisher = _publisher(task_id, user)
    try:
        save_dir = await run_in_threadpool(publisher.ensure_root)
        logger.info("Sandbox %s started: model=%s ratio=%s", tool.upper(), model, ratio)
        result = await run_in_threadpool(
            client.generate_image,
            prompt,
            model=model,
            image_paths=image_paths,
            save_dir=save_dir,
            video_ratio=ratio,
        )
        images = [str(path) for path in result] if isinstance(result, list) else []
        record, mapping = await run_in_threadpool(
            _commit_record, publisher, tool=tool, model=model, input_data=input_data,
            output_data={"images": images}, media=[(path, "image") for path in images],
        )
        logger.info("Sandbox %s completed: model=%s record_id=%s images=%d", tool.upper(), model, task_id, len(images))
        return {"success": True, "result": record["output"].get("images", []), "record_id": task_id}
    except Exception as e:
        logger.exception("Sandbox %s failed: model=%s", tool.upper(), model)
        return {"success": False, "error": str(e)}
    finally:
        await run_in_threadpool(publisher.cleanup_root)
        _finish_active_task(task_id)


@router.post("/api/sandbox/t2i")
async def sandbox_t2i(
    req: SandboxT2IRequest, user: CurrentUser = Depends(get_current_user)
):
    """临时工作台 - 文生图"""
    input_data = {"prompt": req.prompt, "style": req.style, "ratio": req.ratio}
    return await _image_tool("t2i", req.model, req.prompt, req.ratio, input_data, None, user)


@router.post("/api/sandbox/i2i")
async def sandbox_i2i(
    req: SandboxI2IRequest, user: CurrentUser = Depends(get_current_user)
):
    """临时工作台 - 图生图"""
    # 图片入参统一守卫（I-A）：本地路径限定白名单，越界在发起生成前拒绝
    try:
        image_input, image_path = _media_input(req.image or "", "参考图片", user)
    except ValueError as e:
        logger.warning("Sandbox I2I media input rejected: model=%s %s", req.model, e)
        return {"success": False, "error": str(e)}
    input_data = {"prompt": req.prompt, "reference_image": image_input}
    return await _image_tool("i2i", req.model, req.prompt, req.ratio, input_data, [image_path], user)


@router.post("/api/sandbox/video")
async def sandbox_video(
    req: SandboxVideoRequest, request: Request, user: CurrentUser = Depends(get_current_user)
):
    """临时工作台 - 视频生成"""
    del request  # 不再按请求基址拼裸 `/code` 地址
    from models.video_client import VideoClient
    client = VideoClient()
    duration = int(req.duration or 5)
    task_id = _start_active_task("video", req.model, {"prompt": req.prompt}, user.login)
    staged = None
    publisher = _publisher(task_id, user)
    try:
        # 图片/视频参考：本地路径白名单校验（失败即报错，不静默降级）；image 可选（文生视频）
        try:
            image_input, image_path = _media_input(req.image, "参考图片", user) if req.image else ("", "")
            video_inputs, reference_video_paths = _media_inputs(req.reference_videos, "参考视频", user)
            audio_input, audio_reference_url = ("", None)
            if req.audio_url:
                audio_input, audio_reference_url, staged = await run_in_threadpool(
                    _audio_reference, req.audio_url, user, task_id,
                )
        except ValueError as e:
            logger.warning("Sandbox video media reference rejected: model=%s %s", req.model, e)
            return {"success": False, "error": str(e)}
        input_data = {
            "prompt": req.prompt,
            "reference_image": image_input,
            "audio_url": audio_input,
            "reference_videos": video_inputs,
            "ratio": req.ratio,
            "resolution": req.resolution,
            "duration": duration,
            "fps": req.fps,
            "short_edge": req.short_edge,
        }
        with SANDBOX_LOCK:
            if task_id in SANDBOX_ACTIVE_TASKS:
                SANDBOX_ACTIVE_TASKS[task_id]["input"] = input_data
        # 参数夹取/忽略记录：随响应透出（可用时）
        adjustments: list = []
        save_dir = await run_in_threadpool(publisher.ensure_root)
        save_path = os.path.join(save_dir, "video.mp4")
        logger.info(
            "Sandbox video started: model=%s image=%s audio_ref=%s video_refs=%d ratio=%s resolution=%s duration=%ss fps=%s short_edge=%s",
            req.model,
            bool(image_path),
            bool(audio_reference_url),
            len(req.reference_videos or []),
            req.ratio,
            req.resolution,
            duration,
            req.fps,
            req.short_edge,
        )

        result = await run_in_threadpool(
            client.generate_video,
            prompt=req.prompt,
            image_path=image_path or "",
            save_path=save_path,
            model=req.model,
            duration=duration,
            shot_type="multi",
            video_ratio=req.ratio or "16:9",
            resolution=req.resolution or "720P",
            fps=req.fps,
            short_edge=req.short_edge,
            audio_reference_url=audio_reference_url,
            reference_video_paths=reference_video_paths or None,
            adjustments=adjustments,
        )
        media = [(save_path, "video")] if os.path.isfile(save_path) else []
        record, mapping = await run_in_threadpool(
            _commit_record, publisher, tool="video", model=req.model, input_data=input_data,
            output_data={"video": result, "video_path": save_path if media else ""}, media=media,
        )
        video_path = record["output"].get("video_path") or ""
        logger.info("Sandbox video completed: model=%s record_id=%s video=%s", req.model, task_id, video_path)
        return {
            "success": True,
            "result": record["output"].get("video"),
            "video_path": video_path,
            "record_id": task_id,
            "warnings": adjustments,
        }
    except Exception as e:
        logger.exception("Sandbox video failed: model=%s", req.model)
        return {"success": False, "error": str(e)}
    finally:
        _release(staged)
        await run_in_threadpool(publisher.cleanup_root)
        _finish_active_task(task_id)
