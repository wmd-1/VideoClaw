"""Sandbox metadata repository (records + path index)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import transaction
from db.repositories._common import jsonb, jsonb_list, now_ms


class SandboxRepo:
    def insert(self, record: Dict[str, Any]) -> None:
        row = {
            "record_id": str(record["record_id"]),
            "owner": str(record.get("owner") or ""),
            "tool": str(record.get("tool") or ""),
            "model": str(record.get("model") or ""),
            "input_json": jsonb(record.get("input")),
            "output_json": jsonb(record.get("output")),
            "files_json": jsonb_list(record.get("files")),
            "created_at": int(record.get("created_at_ms") or now_ms()),
        }
        if not row["owner"]:
            raise ValueError("sandbox owner must not be empty")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sandbox_records
                      (record_id, owner, tool, model, input_json, output_json,
                       files_json, created_at)
                    VALUES
                      (%(record_id)s, %(owner)s, %(tool)s, %(model)s, %(input_json)s,
                       %(output_json)s, %(files_json)s, %(created_at)s)
                    ON CONFLICT (record_id) DO NOTHING
                    """,
                    row,
                )

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sandbox_records WHERE record_id = %s", (record_id,))
                return cur.fetchone()

    def list(self, owner: str, limit: int = 100) -> List[Dict[str, Any]]:
        if not owner:
            raise ValueError("owner is required")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM sandbox_records WHERE owner = %s
                       ORDER BY created_at DESC LIMIT %s""",
                    (owner, limit),
                )
                return list(cur.fetchall())

    def owner_of(self, record_id: str) -> Optional[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT owner FROM sandbox_records WHERE record_id = %s", (record_id,))
                row = cur.fetchone()
                return row["owner"] if row else None

    def delete(self, record_id: str) -> bool:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sandbox_records WHERE record_id = %s", (record_id,))
                return cur.rowcount > 0

    def add_paths(self, record_id: str, paths: List[str]) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                for path in paths:
                    cur.execute(
                        """INSERT INTO sandbox_paths(path, record_id) VALUES (%s, %s)
                           ON CONFLICT (path) DO UPDATE SET record_id = EXCLUDED.record_id""",
                        (path, record_id),
                    )

    def owner_of_path(self, path: str) -> Optional[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT r.owner FROM sandbox_paths p
                       JOIN sandbox_records r ON r.record_id = p.record_id
                       WHERE p.path = %s""",
                    (path,),
                )
                row = cur.fetchone()
                return row["owner"] if row else None
