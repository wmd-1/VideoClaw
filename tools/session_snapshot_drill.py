#!/usr/bin/env python3
"""会话快照隔离演练（可复跑）：seed → 负例 → 导出 → 破坏 → 回滚 → 校验。

前置：在既有 backend 容器内先跑过后端测试（videoclaw_test 已有完整 schema）。
执行（compose 未挂载 tools/，需要先 docker cp）：

  docker cp tools/session_snapshot.py tools/session_snapshot_drill.py \\
      video-claw-backend:/tmp/
  docker exec video-claw-backend /app/.venv/bin/python /tmp/session_snapshot_drill.py

演练只在隔离库/桶里进行：
- 主链路用 videoclaw_test + videoclaw-test；
- 放行路径用临时库 videoclaw_drill（不以 _test 结尾），验证 --allow-production；
- 结束后删除临时库与该会话对象，不触碰业务库/bucket。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import psycopg
from minio import Minio
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

BACKEND = Path(os.environ.get("VC_BACKEND", "/app"))
sys.path.insert(0, str(BACKEND))

from core.session_snapshot import (  # noqa: E402
    SnapshotError,
    export_session,
    restore_session,
)

BUCKET = "videoclaw-test"
DRILL_DB = "videoclaw_drill"
BASE = conninfo_to_dict(os.environ["VC_DATABASE__DSN"])


def _dsn(dbname: str) -> str:
    return make_conninfo(**{**BASE, "dbname": dbname})


def _client() -> Minio:
    return Minio(
        os.environ.get("VC_STORAGE__ENDPOINT", "minio:9000"),
        access_key=os.environ.get("VC_STORAGE__ACCESS_KEY", ""),
        secret_key=os.environ.get("VC_STORAGE__SECRET_KEY", ""),
        secure=False,
    )


def _drop_drill_db() -> None:
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DRILL_DB}" WITH (FORCE)')


def _create_drill_db() -> None:
    _drop_drill_db()
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{DRILL_DB}" TEMPLATE videoclaw_test')


def _seed(dsn: str, session_id: str) -> list[str]:
    keys = [f"drill/{session_id}/{index}.png" for index in range(3)]
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, owner, status, current_stage, status_json, "
            "stage_progress_json, meta_json, boot_id, created_at, updated_at, content_revision) "
            "VALUES (%s,'local','completed','storyboard','{}'::jsonb,'{}'::jsonb,%s,"
            "'boot-1',1,2,7)",
            (session_id, Jsonb({"idea": "drill"})),
        )
        for stage, version in (
            ("character_design", 1),
            ("script_generation", 6),
            ("storyboard", 2),
        ):
            conn.execute(
                "INSERT INTO session_stage_artifacts (session_id, stage, status, payload_json, "
                "error, boot_id, version, started_at, completed_at, updated_at) "
                "VALUES (%s,%s,'completed',%s,NULL,'boot-1',%s,10,20,30)",
                (session_id, stage, Jsonb({"stage": stage, "lines": [1, 2, 3]}), version),
            )
        for index in range(5):
            conn.execute(
                "INSERT INTO session_stage_working (session_id, stage, working_json, preview_json, "
                "execution_json, execution_id, base_version, updated_at) "
                "VALUES (%s,%s,%s,NULL,NULL,%s,%s,40)",
                (session_id, f"work_{index}", Jsonb({"index": index}), f"exec_{index}", index),
            )
        for index, key in enumerate(keys):
            conn.execute(
                "INSERT INTO artifact_files (file_id, owner, session_id, task_id, record_id, stage, "
                "kind, storage_kind, bucket, object_key, public_path, local_path, media_type, "
                "size_bytes, missing_at, created_at) "
                "VALUES (%s,'local',%s,NULL,NULL,'character_design','image','minio',%s,%s,%s,"
                "NULL,'image/png',%s,NULL,50)",
                (f"file_{index}", session_id, BUCKET, key, f"/media/drill/{session_id}/{index}.png", 8 + index),
            )
            conn.execute(
                "INSERT INTO object_operations (operation_id, owner, domain, resource_id, stage, "
                "execution_id, boot_id, file_id, target_kind, bucket, object_key, public_path, "
                "local_paths_json, state, reason, attempts, last_error, next_retry_at, "
                "created_at, updated_at) "
                "VALUES (%s,'local','sessions',%s,'character_design','exec_1','boot-1',%s,"
                "'minio',%s,%s,%s,'[]'::jsonb,'done',NULL,1,NULL,NULL,60,61)",
                (f"op_{index}", session_id, f"file_{index}", BUCKET, key, f"/media/drill/{session_id}/{index}.png"),
            )
        conn.execute(
            "INSERT INTO object_operations (operation_id, owner, domain, resource_id, stage, "
            "execution_id, boot_id, file_id, target_kind, bucket, object_key, public_path, "
            "local_paths_json, state, reason, attempts, last_error, next_retry_at, "
            "created_at, updated_at) "
            "VALUES ('op_no_file','local','sessions',%s,'storyboard','exec_1','boot-1',NULL,"
            "'minio',%s,'drill/no-file.png','/media/drill/no-file.png','[]'::jsonb,'done',"
            "NULL,1,NULL,NULL,62,63)",
            (session_id, BUCKET),
        )
    client = _client()
    for index, key in enumerate(keys):
        payload = f"drill-object-{index}".encode()
        client.put_object(BUCKET, key, io.BytesIO(payload), len(payload))
    return keys


def _negative(dsn: str, session_id: str) -> None:
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO artifact_files (file_id, owner, session_id, kind, storage_kind, "
                "bucket, object_key, public_path, created_at) "
                "VALUES ('bad_owner','intruder',%s,'image','minio',%s,'drill/bad.png',"
                "'/media/drill/bad.png',70)",
                (session_id, BUCKET),
            )
    except psycopg.Error as exc:
        assert "artifact owner mismatch" in str(exc), str(exc)
        print("negative: owner 不一致被触发器拒绝 -> OK")
    else:
        raise AssertionError("owner 不一致没有被拒绝")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO session_stage_working (session_id, stage, base_version, updated_at) "
                "VALUES ('missing_session','x',0,1)"
            )
    except psycopg.errors.ForeignKeyViolation:
        print("negative: 子表先插违反外键 -> OK")
    else:
        raise AssertionError("先插子表没有被外键拒绝")


def _cleanup(dsn: str, session_id: str, keys: list[str]) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
        conn.execute("DELETE FROM object_operations WHERE resource_id = %s", (session_id,))
    client = _client()
    for key in keys:
        try:
            client.remove_object(BUCKET, key)
        except Exception:
            pass


def main() -> int:
    _create_drill_db()
    drill_dsn = _dsn(DRILL_DB)
    session_id = "snap_drill_1"
    keys = _seed(drill_dsn, session_id)
    print("seed rows: 1/3/5/3/4, objects=3")
    _negative(drill_dsn, session_id)

    try:
        export_session(drill_dsn, session_id, Path("/tmp/snap_drill_out"))
    except SnapshotError as exc:
        print("guard: 未加放行导出生产库被拒绝 ->", str(exc))
    else:
        raise AssertionError("非测试库导出没有被拒绝")

    out = Path("/tmp/snap_drill_out")
    snapshot = export_session(
        drill_dsn, session_id, out, allow_production=True
    )
    assert snapshot["row_counts"] == {
        "sessions": 1,
        "session_stage_artifacts": 3,
        "session_stage_working": 5,
        "artifact_files": 3,
        "object_operations": 4,
    }, snapshot["row_counts"]
    print("export rows:", snapshot["row_counts"], "objects:", len(snapshot["objects"]))

    with psycopg.connect(drill_dsn) as conn:
        conn.execute(
            "UPDATE sessions SET content_revision = 99 WHERE session_id = %s", (session_id,)
        )
        conn.execute(
            "DELETE FROM session_stage_working WHERE session_id = %s AND stage = 'work_3'",
            (session_id,),
        )
    _client().remove_object(BUCKET, keys[2])

    try:
        restore_session(drill_dsn, out, session_id=session_id)
    except SnapshotError as exc:
        print("guard: 未加放行回滚生产库被拒绝 ->", str(exc))
    else:
        raise AssertionError("非测试库回滚没有被拒绝")

    restored = restore_session(
        drill_dsn, out, session_id=session_id, allow_production=True
    )
    assert restored["payload_digest"] == snapshot["payload_digest"]
    with psycopg.connect(drill_dsn) as conn:
        assert conn.execute(
            "SELECT content_revision FROM sessions WHERE session_id = %s", (session_id,)
        ).fetchone() == (7,)
    assert _client().stat_object(BUCKET, keys[2]) is not None
    print("restore digest match: True, objects restored: True")

    _cleanup(drill_dsn, session_id, keys)
    _drop_drill_db()
    print("cleanup done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
