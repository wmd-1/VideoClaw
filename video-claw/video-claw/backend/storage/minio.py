"""MinIO object storage adapter."""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any, BinaryIO, Dict, Optional
from urllib.parse import urlparse

from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from storage.base import ObjectMissing, ObjectStorage, StorageError

logger = logging.getLogger(__name__)


def _split_endpoint(value: str, secure: bool) -> tuple:
    """MinIO's client takes host:port; public_endpoint may be an http(s) URL."""
    raw = str(value or "").strip().rstrip("/")
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.path not in ("", "/"):
            raise StorageError(f"MinIO endpoint must not contain a path: {value}")
        return parsed.netloc, parsed.scheme == "https"
    return raw, secure


class MinioStorage(ObjectStorage):
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = False,
        public_endpoint: str = "",
        presign_ttl: int = 3600,
        saved_bucket: str = "videoclaw-saved",
    ) -> None:
        if not access_key or not secret_key:
            raise StorageError("MinIO access_key/secret_key is empty")
        internal_endpoint, internal_secure = _split_endpoint(endpoint, secure)
        self.endpoint = internal_endpoint
        self.bucket = bucket
        self.saved_bucket = saved_bucket
        self.presign_ttl = int(presign_ttl)
        self._client = Minio(
            internal_endpoint, access_key=access_key, secret_key=secret_key, secure=internal_secure
        )
        # SigV4 includes Host; browser media is streamed by the backend, but inference
        # presigning must use the address reachable by the inference service.
        public_endpoint, public_secure = _split_endpoint(public_endpoint or endpoint, internal_secure)
        self._public_client = (
            self._client
            if public_endpoint == internal_endpoint and public_secure == internal_secure
            else Minio(
                public_endpoint, access_key=access_key, secret_key=secret_key, secure=public_secure
            )
        )

    def ensure_bucket(self) -> None:
        for bucket in (self.bucket, self.saved_bucket):
            if not self._client.bucket_exists(bucket):
                self._client.make_bucket(bucket)
                logger.info("Created MinIO bucket %s", bucket)

    def upload(
        self,
        local_path: str,
        object_key: str,
        media_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not os.path.isfile(local_path):
            raise ObjectMissing(f"local file missing: {local_path}")
        try:
            self._client.fput_object(
                bucket or self.bucket,
                object_key,
                local_path,
                content_type=media_type or "application/octet-stream",
            )
        except S3Error as exc:
            raise StorageError(f"MinIO upload failed: {exc}") from exc
        return {"bucket": bucket or self.bucket, "object_key": object_key}

    def download(self, object_key: str, local_path: str, bucket: Optional[str] = None) -> str:
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        try:
            self._client.fget_object(bucket or self.bucket, object_key, local_path)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchBucket"):
                raise ObjectMissing(object_key) from exc
            raise StorageError(f"MinIO download failed: {exc}") from exc
        return local_path

    def stat(self, object_key: str, bucket: Optional[str] = None) -> Optional[Dict[str, Any]]:
        try:
            obj = self._client.stat_object(bucket or self.bucket, object_key)
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchBucket", "NoSuchObject"):
                return None
            raise StorageError(f"MinIO stat failed: {exc}") from exc
        return {
            "size": obj.size,
            "etag": obj.etag,
            "content_type": obj.content_type,
            "last_modified": obj.last_modified,
        }

    def delete(self, object_key: str, bucket: Optional[str] = None) -> None:
        try:
            self._client.remove_object(bucket or self.bucket, object_key)
        except S3Error as exc:
            logger.warning("MinIO delete failed for %s: %s", object_key, exc)

    def copy(self, source_bucket: str, source_key: str, target_bucket: str, target_key: str) -> None:
        try:
            self._client.copy_object(
                target_bucket,
                target_key,
                CopySource(source_bucket, source_key),
            )
        except S3Error as exc:
            raise StorageError(f"MinIO copy failed: {exc}") from exc

    def presign(
        self,
        object_key: str,
        *,
        ttl: Optional[int] = None,
        filename: Optional[str] = None,
        disposition: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> str:
        headers = {}
        if filename or disposition:
            mode = disposition or "inline"
            value = f'{mode}; filename="{filename}"' if filename else mode
            headers["response-content-disposition"] = value
        return self._public_client.presigned_get_object(
            bucket or self.bucket,
            object_key,
            expires=timedelta(seconds=int(ttl or self.presign_ttl)),
            response_headers=headers or None,
        )

    def open_stream(
        self,
        object_key: str,
        *,
        offset: int = 0,
        length: Optional[int] = None,
        bucket: Optional[str] = None,
    ) -> BinaryIO:
        try:
            return self._client.get_object(
                bucket or self.bucket, object_key, offset=offset, length=length
            )
        except S3Error as exc:
            if exc.code in ("NoSuchKey", "NoSuchBucket", "NoSuchObject"):
                raise ObjectMissing(object_key) from exc
            raise StorageError(f"MinIO stream failed: {exc}") from exc
