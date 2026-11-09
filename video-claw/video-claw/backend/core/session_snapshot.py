# -*- coding: utf-8 -*-
"""会话快照导出 / 回滚（试点前保底，不是常规备份）。

范围：sessions / session_stage_artifacts / session_stage_working / artifact_files /
object_operations，外加 artifact_files 指向的 MinIO 对象。

约束：
- DB 行在单个 REPEATABLE READ READ ONLY 事务内读取；
- object_operations 没有 session_id：按 file_id 关联，并兼容只用 resource_id 关联的行；
- restore 先做对象文件预检，再单事务「删父行（级联）→ 父到子重插 → 校验」，
  事务提交后才回写 MinIO，避免 DB 失败时对象已被改动；
- 默认只接受 *videoclaw_test* 库；生产库必须显式 allow_production。

本模块不替代 `pg_dump` + 桶级备份，只用于把单个会话恢复到导出时的状态。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

TABLES = (
    "sessions",
    "session_stage_artifacts",
    "session_stage_working",
    "artifact_files",
    "object_operations",
)
OBJECT_OPERATIONS_FILTER = (
    "(file_id IN (SELECT file_id FROM artifact_files WHERE session_id = %(sid)s) "
    "OR resource_id = %(sid)s)"
)


class SnapshotError(RuntimeError):
    """快照导出/回滚的可读失败。"""


def _table_filter(table: str) -> str:
    return OBJECT_OPERATIONS_FILTER if table == "object_operations" else "session_id = %(sid)s"


def _table_columns(conn, table: str) -> List[dict]:
    rows = conn.execute(
        "SELECT column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    if not rows:
        raise SnapshotError(f"表不存在：{table}")
    return [{"name": row["column_name"], "udt": row["udt_name"]} for row in rows]


def _read_tables(conn, session_id: str) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    for table in TABLES:
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} WHERE {}").format(
                sql.Identifier(table), sql.SQL(_table_filter(table))
            ),
            {"sid": session_id},
        ).fetchall()
        result[table] = {
            "columns": _table_columns(conn, table),
            "rows": [dict(row) for row in rows],
        }
    return result


def payload_digest(tables: Dict[str, dict]) -> str:
    raw = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def guard_dsn(dsn: str, allow_production: bool = False) -> None:
    dbname = str(conninfo_to_dict(dsn).get("dbname") or "")
    if allow_production or dbname.endswith("_test"):
        return
    raise SnapshotError(
        f"拒绝操作非测试库 {dbname!r}：确认目标后加 --allow-production"
    )


def storage_client():
    from minio import Minio

    from config import settings

    return Minio(
        settings.STORAGE_ENDPOINT,
        access_key=settings.STORAGE_ACCESS_KEY,
        secret_key=settings.STORAGE_SECRET_KEY,
        secure=bool(settings.STORAGE_SECURE),
    )


def _safe_key(object_key: str) -> str:
    key = str(object_key or "").strip()
    if not key or key.startswith("/") or ".." in Path(key).parts:
        raise SnapshotError(f"非法 object_key：{object_key!r}")
    return key


def export_session(
    dsn: str,
    session_id: str,
    out_dir: Path,
    *,
    client=None,
    allow_production: bool = False,
) -> dict:
    guard_dsn(dsn, allow_production)
    client = client or storage_client()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tables = _read_tables(conn, session_id)
    if not tables["sessions"]["rows"]:
        raise SnapshotError(f"会话不存在：{session_id}")

    objects: List[dict] = []
    for row in tables["artifact_files"]["rows"]:
        bucket = str(row.get("bucket") or "")
        object_key = str(row.get("object_key") or "")
        if row.get("storage_kind") != "minio" or not bucket:
            continue
        relative = Path("objects") / bucket / _safe_key(object_key)
        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        stat = client.stat_object(bucket, object_key)
        client.fget_object(bucket, object_key, str(target))
        objects.append(
            {
                "bucket": bucket,
                "object_key": object_key,
                "path": relative.as_posix(),
                "size": int(stat.size or 0),
                "etag": str(stat.etag or "").strip('"'),
            }
        )

    session_row = tables["sessions"]["rows"][0]
    snapshot = {
        "format": 1,
        "session_id": session_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "content_revision": session_row.get("content_revision"),
        "status": session_row.get("status"),
        "current_stage": session_row.get("current_stage"),
        "stages": {
            row["stage"]: {"status": row.get("status"), "version": row.get("version")}
            for row in tables["session_stage_artifacts"]["rows"]
        },
        "row_counts": {table: len(tables[table]["rows"]) for table in TABLES},
        "payload_digest": payload_digest(tables),
        "tables": tables,
        "objects": objects,
    }
    (out_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return snapshot


def _checked_snapshot(snapshot_dir: Path, expected_session_id: Optional[str]) -> dict:
    snapshot = json.loads((Path(snapshot_dir) / "snapshot.json").read_text(encoding="utf-8"))
    session_id = str(snapshot.get("session_id") or "")
    if expected_session_id and expected_session_id != session_id:
        raise SnapshotError(
            f"快照会话 {session_id!r} 与 --session-id {expected_session_id!r} 不一致"
        )
    for obj in snapshot.get("objects") or []:
        if not (Path(snapshot_dir) / obj["path"]).exists():
            raise SnapshotError(f"快照缺少对象文件：{obj['path']}")
    return snapshot


def _insert_tables(conn, tables: Dict[str, dict]) -> None:
    for table in TABLES:
        columns = tables[table]["columns"]
        names = [column["name"] for column in columns]
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(name) for name in names),
            sql.SQL(", ").join(sql.Placeholder() for _ in names),
        )
        for row in tables[table]["rows"]:
            conn.execute(statement, [_insert_value(column, row) for column in columns])


def _insert_value(column: dict, row: dict):
    raw = row.get(column["name"])
    if raw is None:
        return None
    if column["udt"] == "jsonb":
        return Jsonb(raw)
    return raw


def _verify(conn, snapshot: dict) -> None:
    session_id = snapshot["session_id"]
    row = conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = %s", (session_id,)
    ).fetchone()
    if not row:
        raise SnapshotError("回滚校验失败：sessions 行缺失")
    if row["content_revision"] != snapshot["content_revision"]:
        raise SnapshotError(
            f"回滚校验失败：content_revision {row['content_revision']} != {snapshot['content_revision']}"
        )
    tables = _read_tables(conn, session_id)
    for table in TABLES:
        actual = len(tables[table]["rows"])
        expected = snapshot["row_counts"][table]
        if actual != expected:
            raise SnapshotError(f"回滚校验失败：{table} 行数 {actual} != {expected}")
    if payload_digest(tables) != snapshot["payload_digest"]:
        raise SnapshotError("回滚校验失败：payload 摘要不一致")
    for stage, meta in (snapshot.get("stages") or {}).items():
        record = conn.execute(
            "SELECT version FROM session_stage_artifacts WHERE session_id = %s AND stage = %s",
            (session_id, stage),
        ).fetchone()
        if not record or record["version"] != meta.get("version"):
            raise SnapshotError(f"回滚校验失败：{stage} version 不一致")


def _restore_objects(client, snapshot: dict, snapshot_dir: Path) -> None:
    for obj in snapshot.get("objects") or []:
        client.fput_object(
            obj["bucket"], obj["object_key"], str(Path(snapshot_dir) / obj["path"])
        )
    for obj in snapshot.get("objects") or []:
        stat = client.stat_object(obj["bucket"], obj["object_key"])
        if int(stat.size or 0) != int(obj.get("size") or 0):
            raise SnapshotError(f"对象大小校验失败：{obj['object_key']}")
        expected = str(obj.get("etag") or "").strip('"')
        actual = str(stat.etag or "").strip('"')
        if expected and "-" not in expected and actual != expected:
            raise SnapshotError(f"对象 ETag 校验失败：{obj['object_key']}")


def restore_session(
    dsn: str,
    snapshot_dir: Path,
    *,
    client=None,
    allow_production: bool = False,
    session_id: Optional[str] = None,
    allow_missing: bool = False,
) -> dict:
    guard_dsn(dsn, allow_production)
    snapshot = _checked_snapshot(Path(snapshot_dir), session_id)
    target = snapshot["session_id"]
    client = client or storage_client()
    tables = snapshot["tables"]
    file_ids = [row["file_id"] for row in tables["artifact_files"]["rows"]]

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        if not allow_missing:
            exists = conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = %s", (target,)
            ).fetchone()
            if not exists:
                raise SnapshotError(
                    f"目标会话不存在：{target}；确认要新建时加 --allow-missing"
                )
        with conn.transaction():
            conn.execute("DELETE FROM sessions WHERE session_id = %s", (target,))
            conn.execute(
                "DELETE FROM object_operations WHERE file_id = ANY(%s) OR resource_id = %s",
                (file_ids, target),
            )
            _insert_tables(conn, tables)
            _verify(conn, snapshot)

    _restore_objects(client, snapshot, Path(snapshot_dir))
    return snapshot


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="会话快照导出 / 回滚")
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="导出单个会话")
    export_parser.add_argument("--session-id", required=True)
    export_parser.add_argument("--out", required=True)
    export_parser.add_argument("--dsn", required=True)
    export_parser.add_argument("--allow-production", action="store_true")

    restore_parser = sub.add_parser("restore", help="从快照回滚")
    restore_parser.add_argument("--snapshot", required=True)
    restore_parser.add_argument("--dsn", required=True)
    restore_parser.add_argument("--session-id")
    restore_parser.add_argument("--allow-production", action="store_true")
    restore_parser.add_argument("--allow-missing", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            snapshot = export_session(
                args.dsn,
                args.session_id,
                Path(args.out),
                allow_production=args.allow_production,
            )
            print(
                f"exported session={snapshot['session_id']} rows={snapshot['row_counts']} "
                f"objects={len(snapshot['objects'])} digest={snapshot['payload_digest'][:12]}"
            )
            return 0
        snapshot = restore_session(
            args.dsn,
            Path(args.snapshot),
            allow_production=args.allow_production,
            session_id=args.session_id,
            allow_missing=args.allow_missing,
        )
        print(
            f"restored session={snapshot['session_id']} rows={snapshot['row_counts']} "
            f"objects={len(snapshot['objects'])} digest={snapshot['payload_digest'][:12]}"
        )
        return 0
    except SnapshotError as exc:
        print(str(exc))
        return 1
