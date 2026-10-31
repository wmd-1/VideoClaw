"""Artifact file metadata repository."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import transaction
from db.repositories._common import now_ms


def _file_row(file: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file_id": str(file["file_id"]),
        "owner": str(file.get("owner") or ""),
        "session_id": file.get("session_id"),
        "task_id": file.get("task_id"),
        "record_id": file.get("record_id"),
        "stage": file.get("stage"),
        "kind": str(file.get("kind") or "file"),
        "storage_kind": str(file.get("storage_kind") or "minio"),
        "bucket": file.get("bucket"),
        "object_key": str(file.get("object_key") or ""),
        "public_path": str(file.get("public_path") or ""),
        "local_path": file.get("local_path"),
        "media_type": file.get("media_type"),
        "size_bytes": file.get("size_bytes"),
        "missing_at": file.get("missing_at"),
        "created_at": int(file.get("created_at") or now_ms()),
    }


def _upsert_many_conn(cur: Any, files: List[Dict[str, Any]]) -> None:
    for file in files:
        row = _file_row(file)
        if not row["owner"]:
            raise ValueError("artifact owner must not be empty")
        cur.execute(
            """
            INSERT INTO artifact_files
              (file_id, owner, session_id, task_id, record_id, stage, kind,
               storage_kind, bucket, object_key, public_path, local_path,
               media_type, size_bytes, missing_at, created_at)
            VALUES
              (%(file_id)s, %(owner)s, %(session_id)s, %(task_id)s, %(record_id)s,
               %(stage)s, %(kind)s, %(storage_kind)s, %(bucket)s, %(object_key)s,
               %(public_path)s, %(local_path)s, %(media_type)s, %(size_bytes)s,
               %(missing_at)s, %(created_at)s)
            ON CONFLICT (file_id) DO UPDATE SET
              owner = EXCLUDED.owner,
              session_id = EXCLUDED.session_id,
              task_id = EXCLUDED.task_id,
              record_id = EXCLUDED.record_id,
              stage = EXCLUDED.stage,
              kind = EXCLUDED.kind,
              storage_kind = EXCLUDED.storage_kind,
              bucket = EXCLUDED.bucket,
              object_key = EXCLUDED.object_key,
              public_path = EXCLUDED.public_path,
              local_path = EXCLUDED.local_path,
              media_type = EXCLUDED.media_type,
              size_bytes = EXCLUDED.size_bytes,
              missing_at = EXCLUDED.missing_at
            """,
            row,
        )


class FileRepo:
    def upsert_many(self, files: List[Dict[str, Any]]) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                _upsert_many_conn(cur, files)

    def get_by_public_path(self, public_path: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM artifact_files WHERE public_path = %s", (public_path,))
                return cur.fetchone()

    def find_by_object(self, bucket: str, object_key: str, *, default_bucket: str = "") -> Optional[Dict[str, Any]]:
        """清理器判定用：仍有 MinIO 文件行精确指向该 bucket/key（历史行 bucket 为空时按默认桶计）。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT file_id, owner FROM artifact_files
                       WHERE storage_kind = 'minio' AND object_key = %s
                         AND (bucket = %s OR (bucket IS NULL AND %s = %s))
                       LIMIT 1""",
                    (object_key, bucket, bucket, default_bucket or ""),
                )
                return cur.fetchone()

    def find_local_reference(self, local_paths: List[str], public_path: str = "") -> Optional[Dict[str, Any]]:
        """清理器判定用：仍有 local 文件行精确指向这些本地路径（或同一 legacy public_path）。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT file_id, owner FROM artifact_files
                       WHERE storage_kind = 'local'
                         AND (local_path = ANY(%s) OR (%s <> '' AND public_path = %s))
                       LIMIT 1""",
                    (list(local_paths), public_path or "", public_path or ""),
                )
                return cur.fetchone()

    def get(self, file_id: str) -> Optional[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM artifact_files WHERE file_id = %s", (file_id,))
                return cur.fetchone()

    def set_local_path(self, file_id: str, local_path: str) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE artifact_files SET local_path = %s WHERE file_id = %s",
                    (local_path, file_id),
                )

    def update_storage(
        self,
        file_id: str,
        *,
        storage_kind: str,
        bucket: Optional[str],
        object_key: str,
        local_path: Optional[str],
        size_bytes: Optional[int],
    ) -> None:
        """迁移用：local → minio 转换时原位更新存储字段，不改 owner/父资源/public_path。"""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE artifact_files
                       SET storage_kind = %s, bucket = %s, object_key = %s,
                           local_path = %s, size_bytes = %s, missing_at = NULL
                       WHERE file_id = %s""",
                    (storage_kind, bucket, object_key, local_path, size_bytes, file_id),
                )

    def mark_missing(self, file_id: str, missing: bool = True) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE artifact_files SET missing_at = %s WHERE file_id = %s",
                    (now_ms() if missing else None, file_id),
                )

    def count_missing(self, session_id: str, stage: str) -> int:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) AS n FROM artifact_files
                       WHERE session_id = %s AND stage = %s AND missing_at IS NOT NULL""",
                    (session_id, stage),
                )
                row = cur.fetchone()
                return int(row["n"]) if row else 0

    def list_by_session(self, sid: str) -> List[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM artifact_files WHERE session_id = %s ORDER BY created_at",
                    (sid,),
                )
                return list(cur.fetchall())

    def list_by_task(self, task_id: str) -> List[Dict[str, Any]]:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM artifact_files WHERE task_id = %s ORDER BY created_at",
                    (task_id,),
                )
                return list(cur.fetchall())

    def delete_by_session(self, sid: str) -> None:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM artifact_files WHERE session_id = %s", (sid,))
