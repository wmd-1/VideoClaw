"""Explicit owner reassignment for admin/legacy migration."""
from __future__ import annotations

from db.database import transaction


class OwnerRepo:
    def count_owner(self, owner: str) -> dict:
        """只读统计各表归属，用于认领前预览与“零行”告警。"""
        if not owner:
            raise ValueError("owner must not be empty")
        counts = {}
        with transaction() as conn:
            with conn.cursor() as cur:
                for table in (
                    "sessions",
                    "pipeline_tasks",
                    "sandbox_records",
                    "saved_items",
                    "artifact_files",
                ):
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE owner = %s", (owner,))
                    counts[table] = int(cur.fetchone()["n"])
        return counts

    def reassign(self, source: str, target: str) -> dict:
        """Move metadata from source to target; parent rows first, then artifacts."""
        if not source or not target:
            raise ValueError("source/target owner must not be empty")
        counts = {}
        with transaction() as conn:
            with conn.cursor() as cur:
                for table in ("sessions", "pipeline_tasks", "sandbox_records", "saved_items"):
                    cur.execute(
                        f"UPDATE {table} SET owner = %s WHERE owner = %s",
                        (target, source),
                    )
                    counts[table] = cur.rowcount
                cur.execute(
                    "UPDATE artifact_files SET owner = %s WHERE owner = %s",
                    (target, source),
                )
                counts["artifact_files"] = cur.rowcount
        return counts
