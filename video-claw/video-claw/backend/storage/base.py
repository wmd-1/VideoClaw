"""Object storage interface. Storage code must not touch the database."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, BinaryIO, Dict, Optional


class StorageError(RuntimeError):
    pass


class ObjectMissing(StorageError):
    pass


class ObjectStorage(ABC):
    @abstractmethod
    def ensure_bucket(self) -> None:
        ...

    @abstractmethod
    def upload(
        self,
        local_path: str,
        object_key: str,
        media_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...

    @abstractmethod
    def download(self, object_key: str, local_path: str, bucket: Optional[str] = None) -> str:
        ...

    @abstractmethod
    def stat(self, object_key: str, bucket: Optional[str] = None) -> Optional[Dict[str, Any]]:
        ...

    @abstractmethod
    def delete(self, object_key: str, bucket: Optional[str] = None) -> None:
        ...

    @abstractmethod
    def copy(self, source_bucket: str, source_key: str, target_bucket: str, target_key: str) -> None:
        ...

    @abstractmethod
    def presign(
        self,
        object_key: str,
        *,
        ttl: Optional[int] = None,
        filename: Optional[str] = None,
        disposition: Optional[str] = None,
        bucket: Optional[str] = None,
    ) -> str:
        ...

    @abstractmethod
    def open_stream(
        self,
        object_key: str,
        *,
        offset: int = 0,
        length: Optional[int] = None,
        bucket: Optional[str] = None,
    ) -> BinaryIO:
        ...
