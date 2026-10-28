"""Saved-item metadata repository."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import transaction
from db.repositories._common import now_ms


class SavedRepo:
    def by_work(self, owner: str, work_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM saved_items WHERE owner = %s AND work_id = %s",
                    (owner, work_id),
                )
                return cur.fetchone()

    def get(self, saved_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM saved_items WHERE item_id = %s", (saved_id,))
                return cur.fetchone()

    def insert(self, item: Dict[str, Any]) -> None:
        row = {
            "item_id": str(item["item_id"]),
            "owner": str(item.get("owner") or ""),
            "work_id": str(item.get("work_id") or ""),
            "domain": str(item.get("domain") or ""),
            "title": item.get("title"),
            "source_bucket": str(item.get("source_bucket") or ""),
            "source_key": str(item.get("source_key") or ""),
            "bucket": str(item.get("bucket") or ""),
            "object_key": str(item.get("object_key") or ""),
            "size_bytes": item.get("size_bytes"),
            "saved_at": int(item.get("saved_at") or now_ms()),
        }
        if not row["owner"] or not row["work_id"]:
            raise ValueError("saved item owner/work_id must not be empty")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO saved_items
                      (item_id, owner, work_id, domain, title, source_bucket,
                       source_key, bucket, object_key, size_bytes, saved_at)
                    VALUES
                      (%(item_id)s, %(owner)s, %(work_id)s, %(domain)s, %(title)s,
                       %(source_bucket)s, %(source_key)s, %(bucket)s, %(object_key)s,
                       %(size_bytes)s, %(saved_at)s)
                    """,
                    row,
                )

    def list(self, owner: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        if not owner:
            raise ValueError("owner is required")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM saved_items WHERE owner = %s
                       ORDER BY saved_at DESC LIMIT %s OFFSET %s""",
                    (owner, limit, offset),
                )
                return list(cur.fetchall())

    def count(self, owner: str) -> int:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM saved_items WHERE owner = %s", (owner,))
                row = cur.fetchone()
                return int(row["n"]) if row else 0

    def delete(self, saved_id: str, owner: str) -> bool:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM saved_items WHERE item_id = %s AND owner = %s",
                    (saved_id, owner),
                )
                return cur.rowcount > 0
