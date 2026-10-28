"""PostgreSQL access for user-space metadata.

The pool is created lazily; importing this module never connects to the database.
All callers are sync (routes wrap them with run_in_threadpool).
"""
from __future__ import annotations

import logging
import threading
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
    """Run the idempotent schema migration through the existing pool."""
    migration = Path(__file__).resolve().parent / "migrations" / "001_init.sql"
    sql = migration.read_text(encoding="utf-8")
    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
    logger.info("PostgreSQL schema initialized from %s", migration.name)


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
