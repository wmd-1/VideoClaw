"""Session metadata repository. All SQL for sessions lives here."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import transaction
from db.repositories._common import jsonb, now_ms, state_session_row


class SessionRepo:
    def upsert(self, state: Any, *, boot_id: str = "") -> None:
        row = state_session_row(state, boot_id=boot_id)
        if not row["owner"]:
            raise ValueError("session owner must not be empty")
        ts = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions
                      (session_id, owner, status, current_stage, status_json,
                       stage_progress_json, meta_json, boot_id, created_at, updated_at)
                    VALUES
                      (%(session_id)s, %(owner)s, %(status)s, %(current_stage)s,
                       %(status_json)s, %(stage_progress_json)s, %(meta_json)s,
                       %(boot_id)s, %(now)s, %(now)s)
                    ON CONFLICT (session_id) DO UPDATE SET
                      owner = EXCLUDED.owner,
                      status = EXCLUDED.status,
                      current_stage = EXCLUDED.current_stage,
                      status_json = EXCLUDED.status_json,
                      stage_progress_json = EXCLUDED.stage_progress_json,
                      meta_json = EXCLUDED.meta_json,
                      boot_id = EXCLUDED.boot_id,
                      updated_at = EXCLUDED.updated_at
                    """,
                    {
                        **row,
                        "status_json": jsonb(row["status_json"]),
                        "stage_progress_json": jsonb(row["stage_progress_json"]),
                        "meta_json": jsonb(row["meta_json"]),
                        "now": ts,
                    },
                )

    def insert_if_absent(self, state: Any, *, boot_id: str = "") -> bool:
        """迁移专用：仅在会话不存在时插入，不覆盖既有 owner/状态/元数据。"""
        row = state_session_row(state, boot_id=boot_id)
        if not row["owner"]:
            raise ValueError("session owner must not be empty")
        ts = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions
                      (session_id, owner, status, current_stage, status_json,
                       stage_progress_json, meta_json, boot_id, created_at, updated_at)
                    VALUES
                      (%(session_id)s, %(owner)s, %(status)s, %(current_stage)s,
                       %(status_json)s, %(stage_progress_json)s, %(meta_json)s,
                       %(boot_id)s, %(now)s, %(now)s)
                    ON CONFLICT (session_id) DO NOTHING
                    """,
                    {
                        **row,
                        "status_json": jsonb(row["status_json"]),
                        "stage_progress_json": jsonb(row["stage_progress_json"]),
                        "meta_json": jsonb(row["meta_json"]),
                        "now": ts,
                    },
                )
                return cur.rowcount > 0

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sessions WHERE session_id = %s", (sid,))
                return cur.fetchone()

    def list(self, owner: str, limit: int = 0, offset: int = 0) -> List[Dict[str, Any]]:
        if not owner:
            raise ValueError("owner is required; owner=None is not allowed")
        sql = "SELECT * FROM sessions WHERE owner = %s ORDER BY updated_at DESC"
        params: list[Any] = [owner]
        if limit:
            sql += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def list_all(self) -> List[Dict[str, Any]]:
        """Startup/admin/import only."""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sessions ORDER BY updated_at DESC")
                return list(cur.fetchall())

    def owner_of(self, sid: str) -> Optional[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT owner FROM sessions WHERE session_id = %s", (sid,))
                row = cur.fetchone()
                return row["owner"] if row else None

    def ids(self) -> set[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_id FROM sessions")
                return {row["session_id"] for row in cur.fetchall()}

    def delete(self, sid: str) -> bool:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
                return cur.rowcount > 0
