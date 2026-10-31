"""Personal-space aggregation and saved-item copy service.

All list queries are owner-scoped at the repository boundary. Works are a
derived view over session post-production artifacts and completed pipeline
tasks; saved items are copies in the saved bucket, independent of source rows.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from config import settings
from core.artifact_publisher import abort_publication
from core.media_refs import to_local, to_public
from core.object_operations import current_boot_id, register_saved_delete
from core.resource_claims import CLAIMS, object_identity
from core.workflow_store import session_meta
from db.repositories import FileRepo, SavedRepo, SessionRepo, StageArtifactRepo, TaskRepo
from db.repositories.operations import ObjectOperationRepo
from db.repositories.resource_commits import commit_saved_item, delete_saved_cascade
from storage import StorageError, get_storage

logger = logging.getLogger(__name__)

_DOMAINS = {"main", "standard", "action_transfer", "digital_human"}
_DOMAIN_LABEL = {
    "main": "主流程",
    "standard": "文艺短视频",
    "action_transfer": "动作迁移",
    "digital_human": "数字人口播",
}
_SAVE_LOCKS: Dict[str, threading.Lock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()
# 显式成片标识；动作迁移/数字人口播会把输入视频与中间片段排在 final 之前
_FINAL_ARTIFACT_NAMES = {"final", "final_video", "成片"}


def _lock(owner: str) -> threading.Lock:
    with _SAVE_LOCKS_GUARD:
        return _SAVE_LOCKS.setdefault(owner, threading.Lock())


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _public_path(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    public = to_public(value)
    return public if public.startswith("/code/result/") else ""


def _title_from_meta(meta: Any, episode: int) -> str:
    if isinstance(meta, dict):
        base = meta.get("idea") or meta.get("user_textbox_input") or ""
        if base:
            return f"{str(base)[:80]} · 第 {episode} 集"
    return f"第 {episode} 集"


def _media_from_file(public: str, row: Optional[Dict[str, Any]], name: str = "成片") -> Optional[Dict[str, Any]]:
    if not public:
        return None
    local = str((row or {}).get("local_path") or to_local(public))
    storage_kind = str((row or {}).get("storage_kind") or "local")
    if row and row.get("missing_at"):
        return None
    if storage_kind == "local" and not os.path.isfile(local):
        return None
    media_type = str((row or {}).get("media_type") or mimetypes.guess_type(public)[0] or "video/mp4")
    size = (row or {}).get("size_bytes")
    if size is None:
        try:
            size = os.path.getsize(local)
        except OSError:
            size = None
    return {
        "kind": "video",
        "name": name,
        "url": public,
        "size": size,
        "media_type": media_type,
        "_source": {
            "public_path": public,
            "local_path": local if os.path.isfile(local) else None,
            "storage_kind": storage_kind,
            "bucket": (row or {}).get("bucket"),
            "object_key": (row or {}).get("object_key"),
            "media_type": media_type,
            "size": size,
            "file_id": (row or {}).get("file_id"),
        },
    }


def _saved_work_ids(owner: str) -> set[str]:
    try:
        return {str(row.get("work_id")) for row in SavedRepo().list(owner, limit=10000)}
    except Exception:
        return set()


def _session_works(owner: str, saved_ids: set[str]) -> List[Dict[str, Any]]:
    works: List[Dict[str, Any]] = []
    for row in SessionRepo().list(owner):
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        stage = StageArtifactRepo().get(sid, "post_production")
        if not stage:
            continue
        payload = stage.get("payload_json") or {}
        if not isinstance(payload, dict):
            continue
        videos = payload.get("final_videos")
        if not isinstance(videos, list) or not videos:
            one = payload.get("final_video")
            videos = [{"path": one, "episode": 1}] if one else []
        meta = session_meta(row.get("meta_json"))
        for index, video in enumerate(videos):
            if not isinstance(video, dict):
                continue
            public = _public_path(video.get("path"))
            if not public:
                continue
            file_row = FileRepo().get_by_public_path(public)
            media = _media_from_file(public, file_row, str(video.get("name") or "成片"))
            if not media:
                continue
            episode = _int(video.get("episode"), index + 1)
            work_id = f"session:{sid}:ep:{episode}"
            works.append({
                "work_id": work_id,
                "source": "session",
                "domain": "main",
                "title": _title_from_meta(meta, episode),
                "episode": episode,
                "created_at": _int(row.get("created_at")),
                "completed_at": _int(stage.get("completed_at"), _int(row.get("updated_at"))),
                "media": [{key: value for key, value in media.items() if key != "_source"}],
                "saved": work_id in saved_ids,
                "_source": media["_source"],
            })
    return works


def _task_works(owner: str, saved_ids: set[str]) -> List[Dict[str, Any]]:
    works: List[Dict[str, Any]] = []
    domain_by_pipeline = {
        "standard": "standard",
        "quick_create": "standard",
        "action_transfer": "action_transfer",
        "digital_human": "digital_human",
    }
    for row in TaskRepo().list(owner, limit=10000):
        if str(row.get("status") or "") != "completed":
            continue
        pipeline = str(row.get("pipeline") or "")
        domain = domain_by_pipeline.get(pipeline)
        if not domain:
            continue
        artifacts = row.get("artifacts_json") or []
        output = row.get("output_json") or {}
        videos = [
            item
            for item in (artifacts if isinstance(artifacts, list) else [])
            if isinstance(item, dict) and item.get("kind") == "video" and item.get("path")
        ]
        # 只接受明确标识的 final 或流水线权威输出；两者都没有就跳过，
        # 不能按产物顺序推断成片（否则中间片段会被当成片展示/收藏）。
        chosen = next(
            (item for item in videos if str(item.get("name") or "") in _FINAL_ARTIFACT_NAMES),
            None,
        )
        candidate = str((chosen or {}).get("path") or "")
        name = str((chosen or {}).get("name") or "成片")
        if not candidate and isinstance(output, dict):
            candidate = str(output.get("video_path") or output.get("final_video") or "")
            name = "成片"
        if not candidate:
            continue
        if name == "final":
            name = "成片"
        public = _public_path(candidate)
        if not public:
            continue
        file_row = FileRepo().get_by_public_path(public)
        media = _media_from_file(public, file_row, name)
        if not media:
            continue
        task_id = str(row.get("task_id") or "")
        input_data = row.get("input_json") or {}
        title = ""
        if isinstance(output, dict):
            title = str(output.get("title") or output.get("goods_title") or "")
        if not title and isinstance(input_data, dict):
            title = str(input_data.get("title") or input_data.get("goods_title") or input_data.get("text") or "")
        if not title:
            title = f"{_DOMAIN_LABEL[domain]}作品"
        work_id = f"task:{task_id}:final"
        works.append({
            "work_id": work_id,
            "source": "task",
            "domain": domain,
            "title": title[:100],
            "episode": None,
            "created_at": _int(row.get("created_at")),
            "completed_at": _int(row.get("completed_at"), _int(row.get("updated_at"))),
            "media": [{key: value for key, value in media.items() if key != "_source"}],
            "saved": work_id in saved_ids,
            "_source": media["_source"],
        })
    return works


def list_works(owner: str) -> List[Dict[str, Any]]:
    if not owner:
        raise ValueError("owner is required")
    saved_ids = _saved_work_ids(owner)
    works = _session_works(owner, saved_ids) + _task_works(owner, saved_ids)
    works.sort(key=lambda item: (_int(item.get("completed_at")), _int(item.get("created_at"))), reverse=True)
    return works


def public_works(owner: str, domain: str = "all", limit: int = 24, offset: int = 0) -> Dict[str, Any]:
    if domain != "all" and domain not in _DOMAINS:
        raise ValueError("invalid domain")
    works = list_works(owner)
    if domain != "all":
        works = [item for item in works if item["domain"] == domain]
    total = len(works)
    page = works[offset: offset + limit]
    return {
        "items": [{key: value for key, value in item.items() if key != "_source"} for item in page],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def counts(owner: str) -> Dict[str, int]:
    works = list_works(owner)
    result = {domain: 0 for domain in _DOMAINS}
    for item in works:
        result[item["domain"]] = result.get(item["domain"], 0) + 1
    return {
        **result,
        "saved": SavedRepo().count(owner),
        "projects": len(SessionRepo().list(owner)),
    }


def find_work(owner: str, work_id: str) -> Optional[Dict[str, Any]]:
    for item in list_works(owner):
        if item["work_id"] == work_id:
            return item
    return None


def _saved_public_item(row: Dict[str, Any]) -> Dict[str, Any]:
    work_id = str(row.get("work_id") or "")
    source_deleted = False
    if work_id.startswith("session:"):
        source_id = work_id.split(":", 2)[1] if work_id.count(":") >= 1 else ""
        source_deleted = not bool(source_id and SessionRepo().get(source_id))
    elif work_id.startswith("task:"):
        source_id = work_id.split(":", 2)[1] if work_id.count(":") >= 1 else ""
        source_deleted = not bool(source_id and TaskRepo().get(source_id))
    item_id = str(row.get("item_id") or "")
    bucket = str(row.get("bucket") or settings.STORAGE_SAVED_BUCKET)
    try:
        available = bool(get_storage().stat(str(row.get("object_key") or ""), bucket=bucket))
    except StorageError:
        available = False
    media = []
    if available:
        media.append({
            "kind": "video",
            "name": "收藏成片",
            "url": f"/api/space/saved/{item_id}/media",
            "size": row.get("size_bytes"),
            "media_type": mimetypes.guess_type(str(row.get("object_key") or ""))[0] or "video/mp4",
        })
    return {
        "saved_id": item_id,
        "work_id": work_id,
        "source": "saved",
        "domain": str(row.get("domain") or ""),
        "title": str(row.get("title") or ""),
        "episode": None,
        "created_at": _int(row.get("saved_at")),
        "completed_at": _int(row.get("saved_at")),
        "media": media,
        "saved": True,
        "saved_at": _int(row.get("saved_at")),
        "source_deleted": source_deleted,
        "available": available,
    }


def list_saved(owner: str, limit: int = 24, offset: int = 0) -> Dict[str, Any]:
    rows = SavedRepo().list(owner, limit=limit, offset=offset)
    return {
        "items": [_saved_public_item(row) for row in rows],
        "total": SavedRepo().count(owner),
        "limit": limit,
        "offset": offset,
    }


def _copy_source(source: Dict[str, Any], target_bucket: str, target_key: str) -> None:
    storage = get_storage()
    storage_kind = str(source.get("storage_kind") or "local")
    local = str(source.get("local_path") or "")
    if storage_kind == "minio" and source.get("object_key"):
        storage.copy(
            str(source.get("bucket") or settings.STORAGE_BUCKET),
            str(source["object_key"]),
            target_bucket,
            target_key,
        )
        return
    if not local or not os.path.isfile(local):
        raise FileNotFoundError("source media missing")
    storage.upload(local, target_key, str(source.get("media_type") or "video/mp4"), bucket=target_bucket)


def save_work(owner: str, work_id: str) -> Dict[str, Any]:
    if not owner or not work_id:
        raise ValueError("owner/work_id required")
    with _lock(owner):
        existing = SavedRepo().by_work(owner, work_id)
        if existing:
            item = _saved_public_item(existing)
            item["saved"] = True
            return item
        if SavedRepo().count(owner) >= int(settings.USERS_SAVED_LIMIT):
            raise OverflowError("saved_limit_exceeded")
        work = find_work(owner, work_id)
        if not work:
            raise KeyError("work not found")
        source = work.get("_source") or {}
        item_id = f"sv_{uuid.uuid4().hex}"
        public = str(source.get("public_path") or "")
        ext = os.path.splitext(public)[1] or ".mp4"
        target_bucket = str(settings.STORAGE_SAVED_BUCKET)
        target_key = f"{hashlib.sha256(owner.encode()).hexdigest()[:16]}/{item_id}{ext}"
        row = {
            "item_id": item_id,
            "owner": owner,
            "work_id": work_id,
            "domain": work["domain"],
            "title": work["title"],
            "source_bucket": str(source.get("bucket") or "local"),
            "source_key": str(source.get("object_key") or public),
            "bucket": target_bucket,
            "object_key": target_key,
            "size_bytes": source.get("size"),
        }
        # D17：副本在复制前登记 prepared（已提交），收藏行与 retained 同事务；失败只补偿本副本
        operation_id = ObjectOperationRepo().register_prepared(
            owner=owner, target_kind="minio", bucket=target_bucket, object_key=target_key,
            domain="saved", resource_id=item_id, stage="saved", execution_id=item_id,
            boot_id=current_boot_id(), reason="save_copy",
        )
        batch = {"objects": [{"operation_id": operation_id, "bucket": target_bucket, "object_key": target_key}],
                 "bucket": target_bucket}
        try:
            with CLAIMS.pinned([object_identity(target_bucket, target_key)]):
                _copy_source(source, target_bucket, target_key)
                info = get_storage().stat(target_key, bucket=target_bucket)
                expected = source.get("size")
                if info is None or (expected and int(info.get("size") or -1) != int(expected)):
                    raise StorageError(f"saved copy unverified: {target_bucket}/{target_key}")
            commit_saved_item(item=row, operation_id=operation_id)
        except Exception:
            issues = abort_publication(batch)
            if issues:
                logger.warning("saved copy compensation deferred: %s", issues)
            raise
        stored = SavedRepo().by_work(owner, work_id)
        return _saved_public_item(stored or {**row, "saved_at": 0})


def delete_saved(owner: str, saved_id: str) -> bool:
    """取消收藏：同事务登记副本定位后删行，物理删除由清理器完成（失败有日志重试）。"""
    if not owner or not saved_id:
        return False
    row = SavedRepo().get(saved_id)
    if not row:
        return True
    if str(row.get("owner") or "") != owner:
        raise PermissionError("saved item not found")
    delete_saved_cascade(
        saved_id, owner,
        lambda cur, saved: register_saved_delete(cur, owner=owner, saved=saved),
    )
    return True


def saved_media_row(saved_id: str) -> Optional[Dict[str, Any]]:
    return SavedRepo().get(saved_id)
