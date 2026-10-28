"""Pipeline task metadata repository."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import transaction
from db.repositories._common import jsonb, jsonb_list, now_ms


class TaskRepo:
    def upsert(self, task: Dict[str, Any]) -> None:
        now = now_ms()
        row = {
            "task_id": str(task["task_id"]),
            "owner": str(task.get("owner") or ""),
            "pipeline": str(task.get("pipeline") or ""),
            "status": str(task.get("status") or "pending"),
            "progress": int(task.get("progress") or 0),
            "message": task.get("message"),
            "input_json": jsonb(task.get("input")),
            "output_json": jsonb(task.get("output")),
            "artifacts_json": jsonb_list(task.get("artifacts")),
            "error": task.get("error"),
            "created_at": task.get("created_at_ms") or now,
            "updated_at": task.get("updated_at_ms") or now,
            "started_at": task.get("started_at_ms"),
            "completed_at": task.get("completed_at_ms"),
            "duration_seconds": task.get("duration_seconds"),
            "output_dir": task.get("output_dir"),
            "boot_id": task.get("boot_id"),
        }
        if not row["owner"]:
            raise ValueError("task owner must not be empty")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_tasks
                      (task_id, owner, pipeline, status, progress, message,
                       input_json, output_json, artifacts_json, error,
                       created_at, updated_at, started_at, completed_at,
                       duration_seconds, output_dir, boot_id)
                    VALUES
                      (%(task_id)s, %(owner)s, %(pipeline)s, %(status)s, %(progress)s,
                       %(message)s, %(input_json)s, %(output_json)s, %(artifacts_json)s,
                       %(error)s, %(created_at)s, %(updated_at)s, %(started_at)s,
                       %(completed_at)s, %(duration_seconds)s, %(output_dir)s, %(boot_id)s)
                    ON CONFLICT (task_id) DO UPDATE SET
                      owner = EXCLUDED.owner,
                      pipeline = EXCLUDED.pipeline,
                      status = EXCLUDED.status,
                      progress = EXCLUDED.progress,
                      message = EXCLUDED.message,
                      input_json = EXCLUDED.input_json,
                      output_json = EXCLUDED.output_json,
                      artifacts_json = EXCLUDED.artifacts_json,
                      error = EXCLUDED.error,
                      updated_at = EXCLUDED.updated_at,
                      started_at = EXCLUDED.started_at,
                      completed_at = EXCLUDED.completed_at,
                      duration_seconds = EXCLUDED.duration_seconds,
                      output_dir = EXCLUDED.output_dir,
                      boot_id = EXCLUDED.boot_id
                    """,
                    row,
                )

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM pipeline_tasks WHERE task_id = %s", (task_id,))
                return cur.fetchone()

    def list(self, owner: str, limit: int = 100) -> List[Dict[str, Any]]:
        if not owner:
            raise ValueError("owner is required")
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM pipeline_tasks WHERE owner = %s
                       ORDER BY created_at DESC LIMIT %s""",
                    (owner, limit),
                )
                return list(cur.fetchall())

    def owner_of(self, task_id: str) -> Optional[str]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT owner FROM pipeline_tasks WHERE task_id = %s", (task_id,))
                row = cur.fetchone()
                return row["owner"] if row else None

    def delete(self, task_id: str) -> bool:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pipeline_tasks WHERE task_id = %s", (task_id,))
                return cur.rowcount > 0

    def sweep_stale_running(self, current_boot_id: str) -> int:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE pipeline_tasks
                       SET status = 'interrupted', updated_at = %s
                       WHERE status IN ('pending', 'running')
                         AND (boot_id IS NULL OR boot_id <> %s)""",
                    (now_ms(), current_boot_id),
                )
                return cur.rowcount
