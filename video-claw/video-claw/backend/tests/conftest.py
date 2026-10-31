"""Test fixtures: isolated PostgreSQL database and in-process FastAPI client.

Run inside the existing backend container. No image or dependency files are
modified; pytest may be installed temporarily in the container venv.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from api.dependencies import workflow_engine
from config import settings
from db import database
from storage import get_storage, reset_storage


def _test_dsn() -> str:
    base = conninfo_to_dict(settings.DATABASE_DSN)
    admin = make_conninfo(**{**base, "dbname": "postgres"})
    with connect(admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = 'videoclaw_test'"
        ).fetchone()
        if not exists:
            conn.execute('CREATE DATABASE "videoclaw_test"')
    return make_conninfo(**{**base, "dbname": "videoclaw_test"})


@pytest.fixture(scope="session", autouse=True)
def test_database():
    settings.DATABASE_DSN = _test_dsn()
    database.close_pool()
    database.init_db()
    settings.USERS_MODE = "guip"
    settings.USERS_ADMIN_LOGINS = ["admin"]
    settings.USERS_LEGACY_OWNER = "local"
    settings.USERS_SAVED_LIMIT = 50
    # 对象存储同样隔离：测试只写专用桶，不触碰业务桶 videoclaw / videoclaw-saved。
    # 隔离桶按需创建，业务桶既不读也不写。
    settings.STORAGE_BUCKET = "videoclaw-test"
    settings.STORAGE_SAVED_BUCKET = "videoclaw-test-saved"
    reset_storage()
    get_storage().ensure_bucket()
    yield
    database.close_pool()


@pytest.fixture(autouse=True)
def clean_state():
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE artifact_files, session_stage_artifacts, sessions, "
                "pipeline_tasks, sandbox_records, saved_items, session_stage_working, "
                "object_operations RESTART IDENTITY CASCADE"
            )
    workflow_engine.sessions.clear()
    workflow_engine._active_sessions.clear()
    workflow_engine._overlays.clear()
    workflow_engine._errors.clear()
    workflow_engine._persist.cache.clear()
    yield
    _delete_registered_test_objects()


def _delete_registered_test_objects() -> None:
    """对象一律先登记后上传：按操作日志删除测试桶里的对象，业务桶一律不碰。"""
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bucket, object_key FROM object_operations WHERE target_kind = 'minio'")
            targets = {(str(row["bucket"]), str(row["object_key"])) for row in cur.fetchall()}
    for bucket, key in sorted(targets):
        if bucket.startswith("videoclaw-test"):
            get_storage().delete(key, bucket=bucket)


@pytest.fixture
def client():
    from api.app import app

    return TestClient(app)


@pytest.fixture
def session_dir(monkeypatch):
    path = tempfile.mkdtemp(prefix="vc-test-sessions-")
    monkeypatch.setattr(workflow_engine, "_session_dir", path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def result_file():
    root = os.path.join(settings.RESULT_DIR, "tests", uuid.uuid4().hex)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "sample.bin")
    with open(path, "wb") as handle:
        handle.write(b"0123456789abcdef")
    yield path
    shutil.rmtree(os.path.dirname(root), ignore_errors=True)
