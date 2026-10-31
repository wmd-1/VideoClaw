"""One-shot legacy JSON/local-file import.

This is a CLI, not startup work. 迁移顺序按数据现状分场景（不存在统一的“先认领”）：
  A. 空库 + JSON 显式 owner：先普通导入（保留 JSON owner）→ 再显式认领 → 最后文件迁移
  B. 已导入数据库：可直接认领；再次普通导入不会回退归属
  C. JSON 缺 owner：首次导入用 --fallback-owner 填入目标归属，再按 B 处理

  python -m db.legacy_import --fallback-owner alice --dry-run
  python -m db.legacy_import --reassign-from local --to alice
  python -m db.legacy_import --files --files-owner alice --dry-run
  python -m db.legacy_import --all-files --confirm-all-files
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
        # 迁移只补缺：已存在的会话（含已认领的 owner）不被旧 JSON 回退
        inserted = SessionRepo().insert_if_absent(
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
        if not inserted:
            # 已有行保持现状；检查点导入本身是 DO NOTHING，可以继续补缺
            logger.info("session %s already imported; kept existing row", sid)
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
        TaskRepo().insert_if_absent(
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


def _lookup_parent_owner(parent_kind: str, parent_id: str) -> str:
    """父资源在 DB 中的当前 owner（认领后即为权威值）。"""
    try:
        if parent_kind == "session":
            row = SessionRepo().get(parent_id)
            return str((row or {}).get("owner") or "")
        if parent_kind == "task":
            return str(TaskRepo().owner_of(parent_id) or "")
        return str(SandboxRepo().owner_of(parent_id) or "")
    except Exception as exc:
        logger.warning("owner lookup failed for %s %s: %s", parent_kind, parent_id, exc)
        return ""


def _resolve_file_owner(
    parent_kind: str,
    parent_id: str,
    record_owner: str,
    existing: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """归属解析：父资源当前 owner 是权威，文件行只用于一致性校验。

    返回 (owner, source)。source 为 ``conflict`` 表示父资源与文件行归属不一致：
    调用方必须拒绝本次迁移并报告待处理，不能隐式选择其中一方。
    """
    parent_owner = _lookup_parent_owner(parent_kind, parent_id)
    row_owner = str((existing or {}).get("owner") or "")
    if parent_owner and row_owner and parent_owner != row_owner:
        return "", "conflict"
    if parent_owner:
        return parent_owner, "parent"
    if row_owner:
        return row_owner, "row"
    return str(record_owner or ""), "json"


def _parent_matches(existing: Dict[str, Any], parent_kind: str, parent_id: str) -> bool:
    """已存在行必须属于同一个父资源，否则不动它（避免跨父资源冒充）。"""
    field = {"session": "session_id", "task": "task_id", "sandbox": "record_id"}[parent_kind]
    current = existing.get(field)
    return bool(current) and str(current) == parent_id


def _verify_object(key: str, local: str, bucket: Optional[str] = None) -> Tuple[bool, str]:
    """上传/重入都要核验对象存在与大小，防止只“上报成功”。"""
    try:
        info = get_storage().stat(key, bucket=bucket or settings.STORAGE_BUCKET)
    except StorageError as exc:
        return False, f"stat failed: {exc}"
    if info is None:
        return False, "object missing"
    expected = os.path.getsize(local)
    actual = info.get("size")
    if actual is None or int(actual) != expected:
        return False, f"size mismatch expected={expected} actual={actual}"
    return True, ""


def _schema_ready() -> bool:
    """只读探测 schema 是否已初始化；dry-run 不得通过 init_db 写入。"""
    try:
        from db.database import transaction

        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.sessions') AS t")
                return bool(cur.fetchone()["t"])
    except Exception as exc:
        logger.info("schema probe failed: %s", exc)
        return False


def _cleanup_object(key: str, bucket: Optional[str] = None) -> bool:
    """本次新上传对象的补偿删除；返回是否确认已不存。"""
    try:
        get_storage().delete(key, bucket=bucket or settings.STORAGE_BUCKET)
    except Exception as exc:
        logger.warning("compensation delete failed for %s: %s", key, exc)
        return False
    try:
        return get_storage().stat(key, bucket=bucket or settings.STORAGE_BUCKET) is None
    except StorageError:
        return False


def _import_files(
    records: List[Dict[str, Any]],
    dry_run: bool,
    *,
    target_owner: str = "",
    all_files: bool = False,
) -> Dict[str, int]:
    """按 owner 范围上传本地旧文件，并把 local 行升级为 minio。

    - ``--files``：只处理 ``target_owner`` 名下的文件；
    - ``--all-files``：显式全量（调用方必须已完成二次确认）；
    - 上传失败保留/建立 local 行，保证媒体网关仍可读且可重试（R19）；
    - 已有 local 行上传成功后原位转换为 minio，已完成 minio 行幂等跳过（R18）。
    """
    counters = {
        "uploaded": 0, "converted": 0, "skipped": 0,
        "skipped_owner": 0, "failed": 0, "conflict": 0,
    }
    seen: set = set()
    owner_by_parent = {
        (record["kind"], record["id"]): record["owner"] for record in records
    }
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

        record_owner = owner_by_parent.get((parent_kind, parent_id), "")
        existing = FileRepo().get_by_public_path(public)
        resolved_owner, owner_source = _resolve_file_owner(parent_kind, parent_id, record_owner, existing)
        if owner_source == "conflict":
            # 父资源与文件行归属不一致：不隐式选边，留给人工处理
            counters["conflict"] += 1
            logger.error(
                "file %s owner conflict: parent=%s row=%s; left untouched",
                public,
                _lookup_parent_owner(parent_kind, parent_id),
                (existing or {}).get("owner"),
            )
            continue
        if not resolved_owner:
            counters["failed"] += 1
            logger.warning("file %s ignored: owner unresolved", public)
            continue
        if existing and not _parent_matches(existing, parent_kind, parent_id):
            counters["failed"] += 1
            logger.warning(
                "file %s ignored: existing row belongs to another parent (%s)", public, parent_kind
            )
            continue
        if record_owner and record_owner != resolved_owner:
            logger.info(
                "file %s owner overridden by %s: json=%s -> %s",
                public, owner_source, record_owner, resolved_owner,
            )
        if not all_files and target_owner and resolved_owner != target_owner:
            counters["skipped_owner"] += 1
            continue

        # 去重必须在父关系/归属/范围全部通过之后：同一路径可能先被“引用方”扫描到
        seen.add(public)

        if existing and existing.get("storage_kind") == "minio":
            ok, reason = _verify_object(
                str(existing.get("object_key") or ""), local, str(existing.get("bucket") or "")
            )
            if ok:
                # 对象恢复后重试：核验通过必须清缺失标记，否则网关/空间仍按 missing 过滤（R18）
                if not dry_run and existing.get("missing_at") is not None:
                    FileRepo().mark_missing(str(existing["file_id"]), False)
                    logger.info("file %s verified again; cleared missing flag", public)
                counters["skipped"] += 1
                continue
            if dry_run:
                counters["failed"] += 1
                logger.info("[dry-run] file %s minio row unverifiable: %s", public, reason)
                continue
            # 保留远端定位（不降级、不盲删），只标记待核验
            FileRepo().mark_missing(str(existing["file_id"]), True)
            counters["failed"] += 1
            logger.warning(
                "file %s minio row unverifiable (%s); kept remote location %s/%s and marked missing",
                public, reason, existing.get("bucket"), existing.get("object_key"),
            )
            continue

        if dry_run:
            counters["converted" if existing else "uploaded"] += 1
            continue

        digest = hashlib.sha1(public.encode("utf-8")).hexdigest()[:16]
        key = f"{owner_key(resolved_owner)}/legacy/{digest}/{os.path.basename(local)}"
        media_type = mimetypes.guess_type(local)[0] or "application/octet-stream"
        try:
            get_storage().upload(local, key, media_type)
        except StorageError as exc:
            _ensure_local_row(parent_kind, parent_id, stage, public, local, resolved_owner, media_type, existing)
            counters["failed"] += 1
            logger.warning("file upload failed, kept local row %s: %s", public, exc)
            continue

        ok, reason = _verify_object(key, local)
        if not ok:
            # 本次新上传的对象不可用：先补偿删除，再按“有/无旧行”分别处理
            cleaned = _cleanup_object(key)
            if existing:
                # 原有行的远端关联保持不变（不盲删、不降级）
                counters["failed"] += 1
                logger.warning(
                    "file %s upload unverified (%s); compensated delete=%s, kept existing row as-is",
                    public, reason, cleaned,
                )
            else:
                _ensure_local_row(parent_kind, parent_id, stage, public, local, resolved_owner, media_type, None)
                counters["failed"] += 1
                logger.warning(
                    "file %s upload unverified (%s); compensated delete=%s, local row kept",
                    public, reason, cleaned,
                )
            continue

        if existing:
            FileRepo().update_storage(
                str(existing["file_id"]),
                storage_kind="minio",
                bucket=settings.STORAGE_BUCKET,
                object_key=key,
                local_path=local,
                size_bytes=os.path.getsize(local),
            )
            counters["converted"] += 1
            continue

        parent = {
            "session_id": parent_id if parent_kind == "session" else None,
            "task_id": parent_id if parent_kind == "task" else None,
            "record_id": parent_id if parent_kind == "sandbox" else None,
        }
        FileRepo().upsert_many([{
            "file_id": f"legacy_{digest}",
            "owner": resolved_owner,
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
        counters["uploaded"] += 1
    return counters


def _ensure_local_row(
    parent_kind: str,
    parent_id: str,
    stage: str,
    public: str,
    local: str,
    owner: str,
    media_type: str,
    existing: Optional[Dict[str, Any]],
) -> None:
    """首次上传失败时建立可读的 local 回退行（已存在则不动），供网关读取与重试。"""
    if existing:
        FileRepo().update_storage(
            str(existing["file_id"]),
            storage_kind="local",
            bucket=None,
            object_key=str(existing.get("object_key") or public.lstrip("/")),
            local_path=local,
            size_bytes=os.path.getsize(local),
        )
        return
    digest = hashlib.sha1(public.encode("utf-8")).hexdigest()[:16]
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
        "storage_kind": "local",
        "bucket": None,
        "object_key": public.lstrip("/"),
        "public_path": public,
        "local_path": local,
        "media_type": media_type,
        "size_bytes": os.path.getsize(local),
        **parent,
    }])


def main(argv: Optional[List[str]] = None) -> int:
    """退出码：0 成功；1 实际或预览存在 failed/conflict；2 非法参数组合或 dry-run 缺 schema（写前拒绝）。"""
    parser = argparse.ArgumentParser(description="Import legacy VideoClaw JSON/local files")
    parser.add_argument("--fallback-owner", default="", help="owner used only when a record has no owner")
    parser.add_argument("--reassign-from", default="", help="claim only: existing owner to move from")
    parser.add_argument("--to", default="", help="claim only: target owner for --reassign-from")
    parser.add_argument("--files", action="store_true", help="upload referenced local files for one owner")
    parser.add_argument("--files-owner", default="", help="target owner for --files (defaults to --fallback-owner)")
    parser.add_argument("--all-files", action="store_true", help="admin-only: upload files for every owner")
    parser.add_argument(
        "--confirm-all-files",
        action="store_true",
        help="admin second confirmation required by --all-files",
    )
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    file_mode = bool(args.files or args.all_files)
    claim_mode = bool(args.reassign_from or args.to)
    if claim_mode:
        if not (args.reassign_from and args.to):
            parser.error("--reassign-from and --to must be used together")
        if file_mode:
            parser.error(
                "--reassign-from/--to 不能与 --files/--all-files 同批执行；"
                "请先单独完成认领，再另一次命令执行文件迁移"
            )
        mixed = [
            flag for flag, value in (
                ("--fallback-owner", args.fallback_owner),
                ("--files-owner", args.files_owner),
                ("--confirm-all-files", args.confirm_all_files),
            ) if value
        ]
        if mixed:
            parser.error(f"纯认领只接受 --reassign-from/--to/--dry-run，不能混用 {', '.join(mixed)}")
        if args.dry_run:
            if not _schema_ready():
                logger.error(
                    "dry-run 需要已初始化的数据库 schema；请先完成部署初始化。"
                    "本命令不做写入，也不会建表"
                )
                return 2
            preview = OwnerRepo().count_owner(args.reassign_from)
            logger.info(
                "[dry-run] claim %s -> %s preview: %s", args.reassign_from, args.to, preview
            )
            if not any(preview.values()):
                logger.warning(
                    "%s 名下无记录：空库场景请先执行普通导入（保留 JSON owner）再认领，"
                    "否则纯认领会更新 0 行",
                    args.reassign_from,
                )
            return 0
        init_db()
        counts = OwnerRepo().reassign(args.reassign_from, args.to)
        logger.info("claimed %s -> %s: %s", args.reassign_from, args.to, counts)
        if not any(counts.values()):
            logger.warning(
                "认领未改动任何行：请确认来源 owner；空库场景需先导入元数据再认领"
            )
        return 0

    if args.files and args.all_files:
        parser.error("--files 与 --all-files 互斥；全量迁移请用 --all-files --confirm-all-files")
    if args.all_files and not args.confirm_all_files:
        parser.error("--all-files 需要管理员二次确认：追加 --confirm-all-files")
    if args.confirm_all_files and not args.all_files:
        parser.error("--confirm-all-files 只能与 --all-files 同用")
    if args.files_owner and args.all_files:
        parser.error("--all-files 不限定 owner，不能与 --files-owner 同用")
    if args.files_owner and not args.files:
        parser.error("--files-owner 只能与 --files 同用")
    target_owner = args.files_owner or args.fallback_owner
    if args.files and not target_owner:
        parser.error("--files 需要指定本次文件迁移的目标 owner：--files-owner 或 --fallback-owner")
    if args.dry_run and not _schema_ready():
        logger.error("dry-run 需要已初始化的数据库 schema；本命令不做写入，也不会建表")
        return 2

    fallback = _fallback_owner(args.fallback_owner)
    if not args.dry_run:
        init_db()
    session_count, sessions = _import_sessions(fallback, args.dry_run)
    task_count, tasks = _import_tasks(fallback, args.dry_run)
    sandbox_count, sandboxes = _import_sandbox(fallback, args.dry_run)
    records = sessions + tasks + sandboxes

    file_counts = {
        "uploaded": 0, "converted": 0, "skipped": 0,
        "skipped_owner": 0, "failed": 0, "conflict": 0,
    }
    if file_mode:
        scope = "all-owners" if args.all_files else f"owner={target_owner}"
        logger.info("%s file migration scope: %s", "[dry-run]" if args.dry_run else "starting", scope)
        file_counts = _import_files(
            records,
            args.dry_run,
            target_owner="" if args.all_files else target_owner,
            all_files=args.all_files,
        )
    logger.info(
        "%s sessions=%d tasks=%d sandbox=%d files[uploaded=%d converted=%d skipped=%d "
        "skipped_owner=%d failed=%d conflict=%d] scope=%s",
        "[dry-run]" if args.dry_run else "imported",
        session_count,
        task_count,
        sandbox_count,
        file_counts["uploaded"],
        file_counts["converted"],
        file_counts["skipped"],
        file_counts["skipped_owner"],
        file_counts["failed"],
        file_counts["conflict"],
        "all-owners" if args.all_files else (target_owner or "-"),
    )
    problems = file_counts["failed"] + file_counts["conflict"]
    if problems:
        logger.error(
            "%s finished with failed=%d conflict=%d; exit 1",
            "[dry-run] preview" if args.dry_run else "import",
            file_counts["failed"], file_counts["conflict"],
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
