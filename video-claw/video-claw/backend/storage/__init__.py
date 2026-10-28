"""Storage factory. Browser media is streamed by the backend; presign is inference-only."""
from __future__ import annotations

import threading
from typing import Optional

from config import settings
from storage.base import ObjectMissing, ObjectStorage, StorageError
from storage.local import LocalStorage
from storage.minio import MinioStorage

_STORAGE: Optional[ObjectStorage] = None
_LOCK = threading.Lock()


def get_storage() -> ObjectStorage:
    global _STORAGE
    if _STORAGE is not None:
        return _STORAGE
    with _LOCK:
        if _STORAGE is None:
            kind = str(getattr(settings, "STORAGE_KIND", "minio") or "minio").lower()
            if kind == "local":
                _STORAGE = LocalStorage()
            else:
                _STORAGE = MinioStorage(
                    settings.STORAGE_ENDPOINT,
                    settings.STORAGE_ACCESS_KEY,
                    settings.STORAGE_SECRET_KEY,
                    settings.STORAGE_BUCKET,
                    secure=bool(settings.STORAGE_SECURE),
                    public_endpoint=settings.STORAGE_PUBLIC_ENDPOINT,
                    presign_ttl=int(settings.STORAGE_PRESIGN_TTL),
                    saved_bucket=settings.STORAGE_SAVED_BUCKET,
                )
    return _STORAGE


def reset_storage() -> None:
    global _STORAGE
    _STORAGE = None


__all__ = ["ObjectMissing", "ObjectStorage", "StorageError", "get_storage", "reset_storage"]
