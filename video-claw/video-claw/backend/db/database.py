"""PostgreSQL access for user-space metadata.

The pool is created lazily; importing this module never connects to the database.
All callers are sync (routes wrap them with run_in_threadpool).
"""
from __future__ import annotations

import logging
import threading
import time as _time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

logger = logging.getLogger(__name__)

_POOL: Optional[ConnectionPool] = None
_POOL_LOCK = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """Raised when the configured database cannot be opened."""


def _dsn() -> str:
    dsn = str(getattr(settings, "DATABASE_DSN", "") or "").strip()
    if not dsn:
        raise DatabaseUnavailable(
            "database.dsn is empty; set VC_DATABASE__DSN (PostgreSQL DSN) before starting"
        )
    return dsn


def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            pool = ConnectionPool(
                conninfo=_dsn(),
                min_size=int(getattr(settings, "DATABASE_POOL_MIN", 1)),
                max_size=int(getattr(settings, "DATABASE_POOL_MAX", 10)),
                kwargs={"row_factory": dict_row, "autocommit": False},
                timeout=10.0,
                open=False,
            )
            pool.open(wait=True, timeout=15.0)
            _POOL = pool
            logger.info("PostgreSQL connection pool opened")
    return _POOL


def close_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.close()
            _POOL = None
            logger.info("PostgreSQL connection pool closed")


def init_db() -> None:
    """Run each pending migration in filename order, recording a per-file marker.

    标记与 DDL 在同一事务提交：只凭 ``schema_meta`` 的总版本号不能证明某个文件
    已执行（历史库可能已手动跳过）。重入时已标记文件直接跳过。
    """
    migrations = sorted(
        (Path(__file__).resolve().parent / "migrations").glob("[0-9][0-9][0-9]_*.sql")
    )
    if not migrations:
        raise DatabaseUnavailable("no migration files found under db/migrations")
    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(name TEXT PRIMARY KEY, applied_at BIGINT NOT NULL)"
                )
                cur.execute("SELECT name FROM schema_migrations")
                applied = {str(row["name"]) for row in cur.fetchall()}
    for migration in migrations:
        if migration.name in applied:
            continue
        sql = migration.read_text(encoding="utf-8")
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations(name, applied_at) VALUES (%s, %s) "
                        "ON CONFLICT (name) DO NOTHING",
                        (migration.name, int(_time.time() * 1000)),
                    )
        logger.info("PostgreSQL schema applied from %s", migration.name)


def schema_version() -> str:
    """已应用的最大迁移版本（schema_meta），用于启动自检与诊断。"""
    row = fetch_one("SELECT value FROM schema_meta WHERE key = %s", ("schema_version",))
    return str((row or {}).get("value") or "")


@contextmanager
def transaction() -> Iterator[Any]:
    """Short transaction helper used by repositories and the checkpoint boundary."""
    with get_pool().connection() as conn:
        with conn.transaction():
            yield conn


def fetch_one(sql: str, params: Any = None) -> Optional[dict[str, Any]]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def fetch_all(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())


def execute(sql: str, params: Any = None) -> None:
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
