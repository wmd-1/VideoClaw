"""4.9：派发时把 canonical 媒体物化为 job 私有运行副本，并在成功后调用发布器。"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Mapping

from config import settings
from core import media_refs
from core.artifact_publisher import execution_work_root
from core.identity import owner_key
from core.reference_values import build_media_reference
from core.workflow_store import ExecutionPublisher


def _copy_to_job_dir(source: str, target: str) -> str:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(source, target)
    return target


def _materialize_scalar(value: Any, user: Dict[str, Any], target: str) -> str:
    ref = media_refs.materialize(str(value), user)
    if media_refs.is_remote(str(ref)):
        return str(ref)
    local = str(getattr(ref, "local_path", "") or ref)
    if not local or not os.path.isfile(local):
        raise RuntimeError(f"canonical 媒体不可物化: {value}")
    return _copy_to_job_dir(local, target)


def prepare_dispatch_job(job: Mapping[str, Any], attempt: Mapping[str, Any]) -> Dict[str, Any]:
    """返回仅用于本次提交的 job 副本；原快照仍保存 canonical/运行副本，不写预签名。"""
    snapshot = dict(job.get("snapshot_json") or {})
    canonical = snapshot.get("canonical_media")
    if not isinstance(canonical, dict):
        return dict(job)
    owner_login = str(snapshot.get("owner_login") or "")
    if not owner_login:
        raise RuntimeError("派发快照缺少 owner_login，不能物化 canonical 媒体")
    owner = owner_key(owner_login)
    request = dict(snapshot.get("request") or {})
    source_id = str(job.get("source_id") or "")
    execution_id = str(job.get("execution_id") or "")
    if not source_id or not execution_id:
        raise RuntimeError("派发快照缺少 session/execution 归属")
    job_id = str(job.get("job_id") or "")
    input_root = os.path.join(
        execution_work_root(owner, "sessions", source_id, execution_id),
        "inputs",
        job_id,
    )
    if os.path.isdir(input_root):
        shutil.rmtree(input_root)
    os.makedirs(input_root, exist_ok=True)
    user = {"login": owner_login}

    for field in ("image_path", "last_image_path"):
        value = canonical.get(field)
        if value:
            if str(value) == str(request.get(field) or ""):
                continue  # 已是本次执行物化副本，无需再走 owner 解析
            ext = os.path.splitext(str(value))[1] or ".bin"
            request[field] = _materialize_scalar(
                value, user, os.path.join(input_root, f"{field}{ext}")
            )
    for field, prefix in (
        ("reference_image_paths", "reference_image"),
        ("reference_video_paths", "reference_video"),
    ):
        references = canonical.get(field) or []
        if not references:
            continue
        materialized = []
        for index, value in enumerate(references):
            ext = os.path.splitext(str(value))[1] or ".bin"
            existing = (request.get(field) or [])
            if index < len(existing) and str(value) == str(existing[index]):
                materialized.append(str(value))
            else:
                materialized.append(
                    _materialize_scalar(
                        value, user, os.path.join(input_root, f"{prefix}_{index}{ext}")
                    )
                )
        request[field] = materialized

    audio = canonical.get("audio_reference_url")
    if audio:
        ref = build_media_reference(audio, user)
        candidates = [item for item in ref.candidates() if item.startswith("http")]
        if candidates:
            request["audio_reference_url"] = candidates[0]
        else:
            request["audio_reference_url"] = _materialize_scalar(
                audio, user, os.path.join(input_root, "audio_reference.bin")
            )

    snapshot["request"] = request
    return {**job, "snapshot_json": snapshot}


def publish_dispatch_result(
    job: Mapping[str, Any], attempt: Mapping[str, Any], result: Any
) -> Dict[str, Any] | None:
    """下载成功后通过既有 ExecutionPublisher 上传；不提交 DB，阶段提交仍由工作流完成。"""
    snapshot = dict(job.get("snapshot_json") or {})
    if str(job.get("source_kind") or "") != "workflow":
        return None
    if not snapshot.get("publish_result"):
        return None
    owner_login = str(snapshot.get("owner_login") or "")
    session_id = str(job.get("source_id") or "")
    execution_id = str(job.get("execution_id") or "")
    save_path = str((result or {}).get("save_path") or "")
    if not owner_login or not session_id or not execution_id or not save_path:
        raise RuntimeError("发布器缺少 owner/session/execution 或输出路径")
    publisher = ExecutionPublisher(
        owner=owner_login,
        session_id=session_id,
        stage="video_generation",
        execution_id=execution_id,
        boot_id=str(getattr(settings, "BOOT_ID", "") or ""),
    )
    _, published = publisher.publish(
        {
            "video_generation": {
                "clips": [{"selected": save_path, "versions": [save_path]}]
            }
        },
        set(),
    )
    return published


__all__ = ["prepare_dispatch_job", "publish_dispatch_result"]
