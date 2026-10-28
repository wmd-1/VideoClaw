"""Stage checkpoint repository. Keeping this file the only SQL owner for stage rows."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import transaction
from db.repositories._common import jsonb, now_ms
from db.repositories.files import _upsert_many_conn


_SESSION_COLUMNS = {
    "status",
    "current_stage",
    "status_json",
    "stage_progress_json",
    "boot_id",
}


def _update_session(cur: Any, sid: str, patch: Optional[Dict[str, Any]]) -> None:
    if not patch:
        return
    fields = []
    values: Dict[str, Any] = {"sid": sid, "updated_at": now_ms()}
    for key, value in patch.items():
        if key not in _SESSION_COLUMNS:
            continue
        fields.append(f"{key} = %({key})s")
        values[key] = jsonb(value) if key in ("status_json", "stage_progress_json") else value
    if not fields:
        return
    fields.append("updated_at = %(updated_at)s")
    cur.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE session_id = %(sid)s", values)


class StageArtifactRepo:
    def mark_running(
        self,
        sid: str,
        stage: str,
        boot_id: str,
        session_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO session_stage_artifacts
                      (session_id, stage, status, boot_id, started_at, updated_at)
                    VALUES (%s, %s, 'running', %s, %s, %s)
                    ON CONFLICT (session_id, stage) DO UPDATE SET
                      status = 'running',
                      boot_id = EXCLUDED.boot_id,
                      started_at = EXCLUDED.started_at,
                      updated_at = EXCLUDED.updated_at
                    """,
                    (sid, stage, boot_id, ts, ts),
                )
                _update_session(cur, sid, session_patch)

    def complete(
        self,
        sid: str,
        stage: str,
        payload: Dict[str, Any],
        files: List[Dict[str, Any]],
        session_patch: Dict[str, Any],
    ) -> None:
        ts = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO session_stage_artifacts
                      (session_id, stage, status, payload_json, version, completed_at, updated_at)
                    VALUES (%s, %s, 'completed', %s, 1, %s, %s)
                    ON CONFLICT (session_id, stage) DO UPDATE SET
                      status = 'completed',
                      payload_json = EXCLUDED.payload_json,
                      error = NULL,
                      version = session_stage_artifacts.version + 1,
                      completed_at = EXCLUDED.completed_at,
                      updated_at = EXCLUDED.updated_at
                    """,
                    (sid, stage, jsonb(payload), ts, ts),
                )
                _upsert_many_conn(cur, files)
                _update_session(cur, sid, session_patch)

    def import_checkpoint(self, sid: str, stage: str, payload: Dict[str, Any]) -> None:
        """Idempotent legacy import: never bump an existing checkpoint on re-run."""
        ts = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO session_stage_artifacts
                         (session_id, stage, status, payload_json, version, completed_at, updated_at)
                       VALUES (%s, %s, 'completed', %s, 1, %s, %s)
                       ON CONFLICT (session_id, stage) DO NOTHING""",
                    (sid, stage, jsonb(payload), ts, ts),
                )

    def fail(
        self,
        sid: str,
        stage: str,
        error: str,
        status: str = "failed",
        session_patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = now_ms()
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE session_stage_artifacts
                       SET status = %s, error = %s, updated_at = %s
                       WHERE session_id = %s AND stage = %s""",
                    (status, error, ts, sid, stage),
                )
                _update_session(cur, sid, session_patch)

    def list_checkpoints(self, sid: str) -> Dict[str, Any]:
        """Return last completed payloads, independent of current execution status."""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT stage, payload_json FROM session_stage_artifacts
                       WHERE session_id = %s AND version > 0
                       ORDER BY stage""",
                    (sid,),
                )
                return {row["stage"]: row["payload_json"] for row in cur.fetchall()}

    def list(self, sid: str) -> Dict[str, Any]:
        return self.list_checkpoints(sid)

    def list_all(self, sid: str) -> List[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM session_stage_artifacts WHERE session_id = %s ORDER BY stage",
                    (sid,),
                )
                return list(cur.fetchall())

    def get(self, sid: str, stage: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM session_stage_artifacts WHERE session_id = %s AND stage = %s",
                    (sid, stage),
                )
                return cur.fetchone()

    def sweep_stale_running(self, current_boot_id: str) -> int:
        ts = now_ms()
        changed = 0
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT session_id, stage FROM session_stage_artifacts
                       WHERE status = 'running'
                         AND (boot_id IS NULL OR boot_id <> %s)""",
                    (current_boot_id,),
                )
                for row in cur.fetchall():
                    cur.execute(
                        """UPDATE session_stage_artifacts
                           SET status = 'interrupted', updated_at = %s
                           WHERE session_id = %s AND stage = %s""",
                        (ts, row["session_id"], row["stage"]),
                    )
                    cur.execute(
                        """UPDATE sessions
                           SET status_json = jsonb_set(
                                 COALESCE(status_json, '{}'::jsonb),
                                 ARRAY[%s],
                                 '"interrupted"'::jsonb,
                                 true
                               ),
                               updated_at = %s
                           WHERE session_id = %s""",
                        (row["stage"], ts, row["session_id"]),
                    )
                    changed += 1
        return changed

    def delete(self, sid: str) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM session_stage_artifacts WHERE session_id = %s", (sid,))
