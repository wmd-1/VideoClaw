"""Pipeline task storage facade over the DB (design D16).

External function names and the API metadata shape are unchanged; the JSON task
files are gone (legacy JSON is only read by the explicit legacy import CLI).

- epoch-millisecond columns are converted to the existing ISO strings at this
  boundary, `*_json` column names are not exposed;
- `input` keeps canonical values, the runner works on a materialized copy;
- every artifact is published (prepared → upload → verify) and committed together
  with its file rows before its event is emitted; the final commit reuses the
  already-published references and submits status/output/final in one transaction;
- a terminal or deleted task accepts no further writes (late callbacks are dropped).
"""
import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from config import settings
from core.object_operations import current_boot_id
from core.resource_publisher import ResourcePublisher, runtime_map_of
from db.repositories import TaskRepo
from db.repositories.commits import CommitParentMissing, CommitStale
from db.repositories.resource_commits import (
    TASK_TERMINAL,
    commit_task_artifact,
    create_task_row,
    delete_task_cascade,
    finish_task_row,
    update_task_row,
)

from .events import publish_task_event

logger = logging.getLogger(__name__)

PROCESS_BOOT_ID = uuid.uuid4().hex
PIPELINE_STAGE = "pipeline"
_publishers: Dict[str, ResourcePublisher] = {}
_publishers_lock = threading.RLock()


def _boot_id() -> str:
    return current_boot_id() or PROCESS_BOOT_ID


def new_task_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def now_iso() -> str:
    return datetime.now().isoformat()


def _iso(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value) / 1000).isoformat()


def _ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value)).timestamp() * 1000)


def task_view(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """DB 行 → 既有 API 元数据形状。"""
    if not row:
        return None
    return {
        "task_id": str(row["task_id"]),
        "owner": str(row.get("owner") or ""),
        "pipeline": str(row.get("pipeline") or ""),
        "status": str(row.get("status") or "pending"),
        "progress": int(row.get("progress") or 0),
        "message": row.get("message"),
        "input": row.get("input_json") or {},
        "output": row.get("output_json") or {},
        "artifacts": list(row.get("artifacts_json") or []),
        "error": row.get("error"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "boot_id": row.get("boot_id"),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
        "duration_seconds": row.get("duration_seconds"),
        "output_dir": row.get("output_dir"),
    }


def _publisher(task_id: str, owner: Optional[str] = None) -> ResourcePublisher:
    with _publishers_lock:
        publisher = _publishers.get(task_id)
        if publisher is None:
            if owner is None:
                owner = TaskRepo().owner_of(task_id)
                if not owner:
                    raise FileNotFoundError(f"Task not found: {task_id}")
            publisher = ResourcePublisher(
                owner=str(owner), domain="tasks", resource_id=task_id,
                stage=PIPELINE_STAGE, boot_id=_boot_id(),
            )
            _publishers[task_id] = publisher
        return publisher


def _forget(task_id: str) -> None:
    with _publishers_lock:
        _publishers.pop(task_id, None)


def task_output_dir(task_id: str) -> str:
    """本任务的执行工作根（服务端生成目录，不是可下载媒体位置）。"""
    return _publisher(task_id).ensure_root()


def set_runtime_inputs(task_id: str, canonical: Dict[str, Any], runtime: Dict[str, Any]) -> None:
    """runner 物化输入后登记反向映射：输出中回显的物化路径恢复为 canonical。"""
    _publisher(task_id).runtime_map.update(runtime_map_of(canonical, runtime))


def load_task(task_id: str) -> Optional[Dict[str, Any]]:
    return task_view(TaskRepo().get(task_id))


def list_tasks(limit: int = 100, owner: Optional[str] = None) -> list[Dict[str, Any]]:
    if not owner:
        raise ValueError("owner is required")
    return [task_view(row) for row in TaskRepo().list(owner, limit=limit)]


def create_task(pipeline: str, input_params: Dict[str, Any], owner: str = "") -> Dict[str, Any]:
    owner = str(owner or getattr(settings, "USERS_LEGACY_OWNER", "local"))
    task_id = new_task_id()
    publisher = _publisher(task_id, owner)
    output_dir = publisher.ensure_root()
    row = create_task_row(
        task_id=task_id, owner=owner, pipeline=pipeline, input_json=input_params,
        output_dir=output_dir, boot_id=_boot_id(),
    )
    logger.info("Created pipeline task: task_id=%s pipeline=%s output_dir=%s", task_id, pipeline, output_dir)
    return task_view(row)


def delete_task(task_id: str, owner: Optional[str] = None) -> bool:
    """终态任务：同事务登记全部对象/工作文件清理后删父行；pending/running 抛 CommitConflict。"""
    from core.object_operations import register_resource_delete

    owner = owner or TaskRepo().owner_of(task_id)
    if not owner:
        return False
    deleted = delete_task_cascade(
        task_id, owner,
        lambda cur: register_resource_delete(cur, owner=owner, domain="tasks", resource_id=task_id),
    )
    if deleted:
        _publisher(task_id, owner).cleanup_root()
        _forget(task_id)
        logger.info("Deleted pipeline task (object cleanup queued): task_id=%s", task_id)
    return deleted


def sweep_stale_running(current_boot_id: str | None = None) -> int:
    """Mark pending/running tasks owned by a previous process as interrupted (no auto-resume)."""
    return TaskRepo().sweep_stale_running(current_boot_id or _boot_id())


def update_task(task_id: str, **updates: Any) -> Dict[str, Any]:
    """进度/状态写入；终态状态走终态提交。终态或已删除任务的迟到回调被丢弃，不复活父行。"""
    if updates.get("status") in TASK_TERMINAL:
        return _finish(
            task_id, str(updates["status"]), error=updates.get("error"),
            message=str(updates.get("message") or f"Task {updates['status']}"),
            progress=updates.get("progress"),
        )
    patch = {key: value for key, value in updates.items() if key in ("status", "progress", "message", "error")}
    if "progress" in patch:
        patch["progress"] = int(patch["progress"] or 0)
    if "started_at" in updates:
        patch["started_at"] = _ms(updates["started_at"])
    try:
        row = update_task_row(task_id, patch)
    except (CommitStale, CommitParentMissing) as exc:
        logger.info("Late task update ignored: task_id=%s (%s)", task_id, exc)
        return load_task(task_id) or {}
    metadata = task_view(row)
    if "progress" in updates or "status" in updates:
        logger.info(
            "Task update: task_id=%s status=%s progress=%s message=%s",
            task_id, metadata.get("status"), metadata.get("progress"), metadata.get("message"),
        )
    publish_task_event(task_id, {
        "type": "progress",
        "status": metadata.get("status"),
        "progress": metadata.get("progress", 0),
    })
    return metadata


def _public_artifact(publisher: ResourcePublisher, item: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    path = item.get("path")
    public = publisher.resolve(path) or mapping.get(str(path or "")) or ""
    published = dict(item)
    published["path"] = public
    published["exists"] = bool(public)
    published.setdefault("created_at", now_iso())
    return published


def _artifact_refs(publisher: ResourcePublisher, items) -> list:
    refs = []
    for item in items:
        path = str((item or {}).get("path") or "")
        if not path or publisher.resolve(path) is not None:
            continue
        if publisher.is_output(path) and not (item or {}).get("exists", True):
            continue  # 未产出的工作文件不发布，也不外露本地路径
        refs.append((path, str(item.get("kind") or "artifact")))
    return refs


def append_artifact(task_id: str, new_artifact: Dict[str, Any]) -> Dict[str, Any]:
    """中间产物：发布 + 文件行 + 产物条目同事务提交后才发事件；失败补偿本批对象。"""
    publisher = _publisher(task_id)
    result, rows, mapping = publisher.publish(_artifact_refs(publisher, [new_artifact]))
    artifact = _public_artifact(publisher, new_artifact, mapping)
    try:
        row = commit_task_artifact(
            task_id=task_id, owner=publisher.owner, artifact=artifact,
            files=rows, retained=(result or {}).get("operation_ids") or [],
        )
    except BaseException:
        publisher.abort(result)
        raise
    publisher.mark_committed(result)
    metadata = task_view(row)
    logger.info(
        "Task artifact: task_id=%s kind=%s name=%s path=%s",
        task_id, artifact.get("kind"), artifact.get("name"), artifact.get("path"),
    )
    publish_task_event(task_id, {
        "type": "artifact",
        "status": metadata.get("status"),
        "progress": metadata.get("progress", 0),
        "artifact": artifact,
    })
    return metadata


async def publish_artifact(task_id: str, new_artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Pipeline 协程内使用：上传/提交在线程池执行，不阻塞事件循环。"""
    return await asyncio.to_thread(append_artifact, task_id, new_artifact)


def mark_running(task_id: str) -> Dict[str, Any]:
    return update_task(task_id, status="running", progress=1, message="Task running", started_at=now_iso())


def _replace(node: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {key: _replace(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_replace(value, mapping) for value in node]
    if isinstance(node, str) and node in mapping:
        return mapping[node]
    return node


def mark_completed(task_id: str, output: Dict[str, Any], artifacts: list[Dict[str, Any]]) -> Dict[str, Any]:
    """终态：复用已发布引用，剩余输出文件与 status/output/final 同事务提交。"""
    publisher = _publisher(task_id)
    current = TaskRepo().get(task_id) or {}
    started = current.get("started_at")
    duration = (time.time() - int(started) / 1000) if started else None
    mapped_output, pending = publisher.map_output(output or {})
    result, rows, mapping = publisher.publish(pending + _artifact_refs(publisher, artifacts or []))
    final_output = _replace(mapped_output, mapping)
    final_artifacts = [_public_artifact(publisher, item, mapping) for item in artifacts or []]
    try:
        row = finish_task_row(
            task_id=task_id, owner=publisher.owner, status="completed", output=final_output,
            artifacts=final_artifacts, files=rows, retained=(result or {}).get("operation_ids") or [],
            error=None, message="Task completed", progress=100, duration_seconds=duration,
        )
    except (CommitStale, CommitParentMissing) as exc:
        publisher.abort(result)
        logger.info("Late task completion ignored: task_id=%s (%s)", task_id, exc)
        return load_task(task_id) or {}
    except BaseException:
        publisher.abort(result)
        raise
    publisher.mark_committed(result)
    publisher.cleanup_root()
    _forget(task_id)
    publish_task_event(task_id, {"type": "completed", "status": "completed", "progress": 100})
    return task_view(row)


def mark_failed(task_id: str, error: str) -> Dict[str, Any]:
    """失败终态：保留已提交的中间产物（不进作品），不再接受迟到写入。"""
    return _finish(task_id, "failed", error=error, message="Task failed", progress=0)


def _finish(task_id: str, status: str, *, error: Optional[str], message: str, progress: Optional[int]) -> Dict[str, Any]:
    try:
        owner = _publisher(task_id).owner
        row = finish_task_row(
            task_id=task_id, owner=owner, status=status, error=error, message=message, progress=progress,
        )
    except (CommitStale, CommitParentMissing, FileNotFoundError) as exc:
        logger.info("Late task %s ignored: task_id=%s (%s)", status, task_id, exc)
        return load_task(task_id) or {}
    publisher = _publishers.get(task_id)
    if publisher is not None:
        publisher.cleanup_root()
    _forget(task_id)
    event_type = "completed" if status == "completed" else "failed"
    publish_task_event(task_id, {"type": event_type, "status": status, "progress": int(row.get("progress") or 0)})
    return task_view(row)
