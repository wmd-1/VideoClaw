"""One-shot legacy JSON/local-file import.

This is a CLI, not startup work:
  python -m db.legacy_import --fallback-owner alice --dry-run
  python -m db.legacy_import --fallback-owner alice --files
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import settings
from core.media_refs import owner_key, to_local, to_public
from db import init_db
from db.repositories import (
    FileRepo,
    OwnerRepo,
    SandboxRepo,
    SessionRepo,
    StageArtifactRepo,
    TaskRepo,
)
from storage import StorageError, get_storage

logger = logging.getLogger(__name__)


def _ms(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Existing sessions use seconds; task/sandbox use ISO. Values >= 1e12 are ms.
        return int(value if float(value) >= 1_000_000_000_000 else float(value) * 1000)
    text = str(value).strip()
    if text.isdigit():
        return _ms(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        logger.warning("Unparseable timestamp %r; using now", value)
        return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _fallback_owner(cli_value: str) -> str:
    return cli_value or os.environ.get("VC_USERS__LEGACY_OWNER", "local")


def _session_files() -> Iterable[str]:
    return glob.glob(os.path.join(settings.SESSION_DIR, "*.json"))


def _task_files() -> Iterable[str]:
    return glob.glob(os.path.join(settings.TASK_DIR, "*.json"))


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)


def _import_sessions(fallback: str, dry_run: bool) -> Tuple[int, List[Dict[str, Any]]]:
    count = 0
    records: List[Dict[str, Any]] = []
    for path in _session_files():
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            logger.warning("skip unreadable session %s: %s", path, exc)
            continue
        sid = str(data.get("session_id") or os.path.splitext(os.path.basename(path))[0])
        owner = str(data.get("owner") or fallback)
        count += 1
        records.append({"kind": "session", "id": sid, "owner": owner, "data": data})
        if dry_run:
            continue
        status = data.get("status")
        status_json = status if isinstance(status, dict) else {}
        SessionRepo().upsert(
            {
                "session_id": sid,
                "owner": owner,
                "status": status if isinstance(status, str) else "idle",
                "current_stage": data.get("current_stage"),
                "status_json": status_json,
                "stage_progress": data.get("stage_progress") or {},
                "project_params": data.get("project_params") or data.get("meta") or {},
            }
        )
        artifacts = data.get("artifacts") or {}
        for stage, payload in artifacts.items():
            StageArtifactRepo().import_checkpoint(
                sid,
                str(stage),
                payload if isinstance(payload, dict) else {"value": payload},
            )
    return count, records


def _import_tasks(fallback: str, dry_run: bool) -> Tuple[int, List[Dict[str, Any]]]:
    count = 0
    records: List[Dict[str, Any]] = []
    for path in _task_files():
        try:
            with open(path, encoding="utf-8") as handle:
                task = json.load(handle)
        except Exception as exc:
            logger.warning("skip unreadable task %s: %s", path, exc)
            continue
        task_id = str(task.get("task_id") or os.path.splitext(os.path.basename(path))[0])
        owner = str(task.get("owner") or fallback)
        count += 1
        records.append({"kind": "task", "id": task_id, "owner": owner, "data": task})
        if dry_run:
            continue
        TaskRepo().upsert(
            {
                **task,
                "task_id": task_id,
                "owner": owner,
                "created_at_ms": _ms(task.get("created_at")),
                "updated_at_ms": _ms(task.get("updated_at")),
                "started_at_ms": _ms(task.get("started_at")),
                "completed_at_ms": _ms(task.get("completed_at")),
                "output_dir": task.get("output_dir") or os.path.join(settings.RESULT_DIR, "task", task_id),
            }
        )
    return count, records


def _sandbox_history() -> List[Dict[str, Any]]:
    path = os.path.join(settings.RESULT_DIR, "sandbox", "history.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("skip sandbox history: %s", exc)
        return []
    if isinstance(data, dict):
        data = data.get("records") or data.get("items") or []
    return data if isinstance(data, list) else []


def _import_sandbox(fallback: str, dry_run: bool) -> Tuple[int, List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    for item in _sandbox_history():
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("id") or item.get("record_id") or "")
        if not record_id:
            continue
        owner = str(item.get("owner") or fallback)
        records.append({"kind": "sandbox", "id": record_id, "owner": owner, "data": item})
        if dry_run:
            continue
        SandboxRepo().insert(
            {
                **item,
                "record_id": record_id,
                "owner": owner,
                "tool": item.get("tool") or item.get("type") or "sandbox",
                "model": item.get("model") or "",
                "input": item.get("input") or item.get("input_data") or {},
                "output": item.get("output") or item.get("output_data") or {},
                "files": item.get("files") or item.get("output_files") or [],
                "created_at_ms": _ms(item.get("created_at")),
            }
        )
        paths = item.get("paths") or item.get("files") or []
        if isinstance(paths, list):
            SandboxRepo().add_paths(record_id, [str(p) for p in paths if isinstance(p, str)])
    return len(records), records


def _iter_payload_paths(records: List[Dict[str, Any]]) -> Iterable[Tuple[str, str, str, str]]:
    """Yield (parent_kind, parent_id, stage, path_string) for referenced files."""
    for record in records:
        kind, parent_id, data = record["kind"], record["id"], record["data"]
        if kind == "session":
            for stage, payload in (data.get("artifacts") or {}).items():
                for value in _walk_strings(payload):
                    yield "session", parent_id, str(stage), value
        elif kind == "task":
            for value in _walk_strings(data.get("artifacts") or data.get("output") or {}):
                yield "task", parent_id, "", value
        else:
            for value in _walk_strings(data.get("files") or data.get("output_files") or []):
                yield "sandbox", parent_id, "", value


def _import_files(records: List[Dict[str, Any]], dry_run: bool) -> int:
    imported = 0
    seen: set = set()
    for parent_kind, parent_id, stage, raw in _iter_payload_paths(records):
        if not raw or raw.startswith(("http://", "https://", "data:", "oss://")):
            continue
        public = to_public(raw)
        if not public.startswith("/code/result/"):
            continue
        local = to_local(public)
        if not os.path.isfile(local):
            continue
        if public in seen:
            continue
        seen.add(public)
        if dry_run:
            imported += 1
            continue
        if FileRepo().get_by_public_path(public):
            continue
        owner = next((r["owner"] for r in records if r["kind"] == parent_kind and r["id"] == parent_id), "")
        if not owner:
            continue
        digest = hashlib.sha1(public.encode("utf-8")).hexdigest()[:16]
        key = f"{owner_key(owner)}/legacy/{digest}/{os.path.basename(local)}"
        media_type = mimetypes.guess_type(local)[0] or "application/octet-stream"
        try:
            get_storage().upload(local, key, media_type)
        except StorageError as exc:
            logger.warning("file import skipped %s: %s", local, exc)
            continue
        parent = {
            "session_id": parent_id if parent_kind == "session" else None,
            "task_id": parent_id if parent_kind == "task" else None,
            "record_id": parent_id if parent_kind == "sandbox" else None,
        }
        FileRepo().upsert_many([{
            "file_id": f"legacy_{digest}",
            "owner": owner,
            "stage": stage or None,
            "kind": "legacy",
            "storage_kind": "minio",
            "bucket": settings.STORAGE_BUCKET,
            "object_key": key,
            "public_path": public,
            "local_path": local,
            "media_type": media_type,
            "size_bytes": os.path.getsize(local),
            **parent,
        }])
        imported += 1
    return imported


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Import legacy VideoClaw JSON/local files")
    parser.add_argument("--fallback-owner", default="", help="owner used only when a record has no owner")
    parser.add_argument("--reassign-from", default="", help="existing owner to move from")
    parser.add_argument("--to", default="", help="target owner for --reassign-from")
    parser.add_argument("--files", action="store_true", help="upload referenced local files")
    parser.add_argument("--all-files", action="store_true", help="admin-only: allow all referenced owners")
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.reassign_from or args.to:
        if not (args.reassign_from and args.to):
            parser.error("--reassign-from and --to must be used together")
        if args.dry_run:
            logger.info("[dry-run] reassign %s -> %s", args.reassign_from, args.to)
        else:
            init_db()
            logger.info("reassigned: %s", OwnerRepo().reassign(args.reassign_from, args.to))

    fallback = _fallback_owner(args.fallback_owner)
    if not args.dry_run:
        init_db()
    session_count, sessions = _import_sessions(fallback, args.dry_run)
    task_count, tasks = _import_tasks(fallback, args.dry_run)
    sandbox_count, sandboxes = _import_sandbox(fallback, args.dry_run)
    records = sessions + tasks + sandboxes
    file_count = _import_files(records, args.dry_run) if args.files or args.all_files else 0
    logger.info(
        "%s sessions=%d tasks=%d sandbox=%d files=%d",
        "[dry-run]" if args.dry_run else "imported",
        session_count,
        task_count,
        sandbox_count,
        file_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
