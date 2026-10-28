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
from storage import reset_storage


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
    reset_storage()
    yield
    database.close_pool()


@pytest.fixture(autouse=True)
def clean_state():
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE TABLE artifact_files, session_stage_artifacts, sessions, "
                "pipeline_tasks, sandbox_records, saved_items RESTART IDENTITY CASCADE"
            )
    workflow_engine.sessions.clear()
    workflow_engine._active_sessions.clear()
    yield


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
