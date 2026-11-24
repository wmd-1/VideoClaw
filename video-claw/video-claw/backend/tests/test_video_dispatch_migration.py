"""003 视频调度迁移：幂等、唯一键与业务删除不解除槽位。"""
from __future__ import annotations

import time
import uuid

import pytest
from psycopg import errors

from db import database


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_migration_is_idempotent_and_does_not_release_slots():
    database.init_db()
    database.init_db()

    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            )
            assert cur.fetchone()["value"] == "3"
            cur.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations WHERE name = %s",
                ("003_video_dispatch.sql",),
            )
            assert cur.fetchone()["count"] == 1

    suffix = uuid.uuid4().hex[:10]
    job_id = f"job-{suffix}"
    attempt_id = f"attempt-{suffix}"
    resource_key = f"gpu-{suffix}"
    session_id = f"session-{suffix}"
    now = _now_ms()

    try:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO sessions(session_id, owner, status, created_at, updated_at) "
                        "VALUES (%s, %s, 'running', %s, %s)",
                        (session_id, "owner-a", now, now),
                    )
                    cur.execute(
                        "INSERT INTO video_dispatch_jobs("
                        "job_id, owner, batch_id, source_kind, execution_id, item_id, "
                        "request_key, dispatch_mode, accepted_at, queue_deadline_at"
                        ") VALUES (%s, %s, %s, 'workflow', %s, 'item-1', %s, 'auto', %s, %s)",
                        (job_id, "owner-a", f"batch-{suffix}", session_id, f"req-{suffix}", now, now + 3600_000),
                    )
                    cur.execute(
                        "INSERT INTO video_dispatch_attempts("
                        "attempt_id, job_id, attempt_no, instance_id, resource_key"
                        ") VALUES (%s, %s, 1, 'sglang-01', %s)",
                        (attempt_id, job_id, resource_key),
                    )
                    cur.execute(
                        "INSERT INTO video_resource_slots(resource_key, active_attempt_id, token) "
                        "VALUES (%s, %s, %s)",
                        (resource_key, attempt_id, "token-1"),
                    )

        with database.get_pool().connection() as conn:
            with pytest.raises(errors.UniqueViolation):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO video_dispatch_attempts("
                            "attempt_id, job_id, attempt_no, instance_id, resource_key"
                            ") VALUES (%s, %s, 1, 'sglang-01', %s)",
                            (f"attempt-dup-{suffix}", job_id, resource_key),
                        )

        # 业务会话删除没有外键连到调度账本，槽位必须保留。
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
                    cur.execute(
                        "SELECT active_attempt_id FROM video_resource_slots WHERE resource_key = %s",
                        (resource_key,),
                    )
                    assert cur.fetchone()["active_attempt_id"] == attempt_id

        # 直接删 job 会级联 attempt，但槽位 RESTRICT 必须阻止这次删除。
        with database.get_pool().connection() as conn:
            with pytest.raises(errors.ForeignKeyViolation):
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM video_dispatch_jobs WHERE job_id = %s", (job_id,)
                        )
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT active_attempt_id FROM video_resource_slots WHERE resource_key = %s",
                        (resource_key,),
                    )
                    assert cur.fetchone()["active_attempt_id"] == attempt_id
    finally:
        with database.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM video_resource_slots WHERE resource_key = %s", (resource_key,)
                    )
                    cur.execute(
                        "DELETE FROM video_dispatch_jobs WHERE job_id = %s", (job_id,)
                    )
                    cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
