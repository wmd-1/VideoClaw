# -*- coding: utf-8 -*-
"""会话快照导出 / 回滚的隔离演练（videoclaw_test 库 + videoclaw-test* 桶）。"""

from __future__ import annotations

import io
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

from config import settings
from core.session_snapshot import (
    SnapshotError,
    export_session,
    guard_dsn,
    restore_session,
    storage_client,
)

BUCKET = "videoclaw-test"


def _objects(session_id: str) -> list[str]:
    return [f"drill/{session_id}/{index}.png" for index in range(3)]


def _seed(session_id: str) -> list[str]:
    keys = _objects(session_id)
    with psycopg.connect(settings.DATABASE_DSN) as conn:
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
        # 只靠 resource_id 关联、没有 file_id 的操作行（导出过滤要兼容）
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
    client = storage_client()
    for index, key in enumerate(keys):
        payload = f"drill-object-{index}".encode()
        client.put_object(BUCKET, key, io.BytesIO(payload), len(payload))
    return keys


def test_export_restore_roundtrip(tmp_path):
    session_id = f"snap_{uuid.uuid4().hex[:8]}"
    keys = _seed(session_id)

    snapshot = export_session(settings.DATABASE_DSN, session_id, tmp_path)
    assert snapshot["row_counts"] == {
        "sessions": 1,
        "session_stage_artifacts": 3,
        "session_stage_working": 5,
        "artifact_files": 3,
        "object_operations": 4,
    }
    assert snapshot["content_revision"] == 7
    assert len(snapshot["objects"]) == 3
    assert all(obj["etag"] for obj in snapshot["objects"])

    with psycopg.connect(settings.DATABASE_DSN) as conn:
        conn.execute(
            "UPDATE sessions SET content_revision = 99, status = 'idle' WHERE session_id = %s",
            (session_id,),
        )
        conn.execute(
            "UPDATE session_stage_artifacts SET version = 99, payload_json = %s "
            "WHERE session_id = %s AND stage = 'storyboard'",
            (Jsonb({"broken": True}), session_id),
        )
        conn.execute(
            "DELETE FROM session_stage_working WHERE session_id = %s AND stage = 'work_3'",
            (session_id,),
        )
    storage_client().remove_object(BUCKET, keys[2])

    restored = restore_session(settings.DATABASE_DSN, tmp_path, session_id=session_id)
    assert restored["payload_digest"] == snapshot["payload_digest"]

    with psycopg.connect(settings.DATABASE_DSN) as conn:
        assert conn.execute(
            "SELECT content_revision, status FROM sessions WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (7, "completed")
        assert conn.execute(
            "SELECT version FROM session_stage_artifacts WHERE session_id = %s AND stage = 'storyboard'",
            (session_id,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM session_stage_working WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (5,)
    assert storage_client().stat_object(BUCKET, keys[2]) is not None


def test_restore_precheck_keeps_objects_when_snapshot_incomplete(tmp_path):
    session_id = f"snap_{uuid.uuid4().hex[:8]}"
    keys = _seed(session_id)
    snapshot = export_session(settings.DATABASE_DSN, session_id, tmp_path)
    client = storage_client()
    before = str(client.stat_object(BUCKET, keys[0]).etag)

    with psycopg.connect(settings.DATABASE_DSN) as conn:
        conn.execute(
            "UPDATE sessions SET content_revision = 99 WHERE session_id = %s", (session_id,)
        )
    (tmp_path / snapshot["objects"][0]["path"]).unlink()

    with pytest.raises(SnapshotError):
        restore_session(settings.DATABASE_DSN, tmp_path)

    assert str(client.stat_object(BUCKET, keys[0]).etag) == before
    with psycopg.connect(settings.DATABASE_DSN) as conn:
        assert conn.execute(
            "SELECT content_revision FROM sessions WHERE session_id = %s", (session_id,)
        ).fetchone() == (99,)


def test_restore_requires_matching_session_id_and_existing_target(tmp_path):
    session_id = f"snap_{uuid.uuid4().hex[:8]}"
    _seed(session_id)
    export_session(settings.DATABASE_DSN, session_id, tmp_path)

    with pytest.raises(SnapshotError):
        restore_session(settings.DATABASE_DSN, tmp_path, session_id="other")

    with psycopg.connect(settings.DATABASE_DSN) as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
    with pytest.raises(SnapshotError):
        restore_session(settings.DATABASE_DSN, tmp_path)

    restore_session(settings.DATABASE_DSN, tmp_path, allow_missing=True)
    with psycopg.connect(settings.DATABASE_DSN) as conn:
        assert conn.execute(
            "SELECT content_revision FROM sessions WHERE session_id = %s", (session_id,)
        ).fetchone() == (7,)


def test_guard_dsn():
    with pytest.raises(SnapshotError):
        guard_dsn("postgresql://u:p@postgres:5432/videoclaw")
    guard_dsn("postgresql://u:p@postgres:5432/videoclaw_test")
    guard_dsn("postgresql://u:p@postgres:5432/videoclaw", allow_production=True)
