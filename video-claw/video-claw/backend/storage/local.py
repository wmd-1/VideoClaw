"""Legacy local-file storage adapter (read/import only)."""
from __future__ import annotations

import os
import shutil
from typing import Any, BinaryIO, Dict, Optional

from config import settings
from storage.base import ObjectMissing, ObjectStorage, StorageError


class LocalStorage(ObjectStorage):
    def __init__(self, root: str = "") -> None:
        self.bucket = "local"
        self.root = os.path.realpath(root or settings.RESULT_DIR)

    def _path(self, object_key: str) -> str:
        path = os.path.realpath(os.path.join(self.root, object_key))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise StorageError("local object key escaped result root")
        return path

    def ensure_bucket(self) -> None:
        os.makedirs(self.root, exist_ok=True)

    def upload(
        self,
        local_path: str,
        object_key: str,
        media_type: str = "application/octet-stream",
        bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        target = self._path(object_key)
        os.makedirs(os.path.dirname(target) or self.root, exist_ok=True)
        shutil.copy2(local_path, target)
        return {"bucket": bucket or self.bucket, "object_key": object_key}

    def download(self, object_key: str, local_path: str, bucket: Optional[str] = None) -> str:
        source = self._path(object_key)
        if not os.path.isfile(source):
            raise ObjectMissing(object_key)
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        shutil.copy2(source, local_path)
        return local_path

    def stat(self, object_key: str, bucket: Optional[str] = None) -> Optional[Dict[str, Any]]:
        path = self._path(object_key)
        if not os.path.isfile(path):
            return None
        stat = os.stat(path)
        return {"size": stat.st_size, "etag": "", "content_type": None, "last_modified": stat.st_mtime}

    def delete(self, object_key: str, bucket: Optional[str] = None) -> None:
        path = self._path(object_key)
        if os.path.isfile(path):
            os.remove(path)

    def copy(self, source_bucket: str, source_key: str, target_bucket: str, target_key: str) -> None:
        source = self._path(source_key)
        if not os.path.isfile(source):
            raise ObjectMissing(source_key)
        target = self._path(target_key)
        os.makedirs(os.path.dirname(target) or self.root, exist_ok=True)
        shutil.copy2(source, target)

    def presign(self, object_key: str, **_: Any) -> str:
        raise StorageError("local storage does not support presigned URLs")

    def open_stream(
        self,
        object_key: str,
        *,
        offset: int = 0,
        length: Optional[int] = None,
        bucket: Optional[str] = None,
    ) -> BinaryIO:
        path = self._path(object_key)
        if not os.path.isfile(path):
            raise ObjectMissing(object_key)
        handle = open(path, "rb")
        if offset:
            handle.seek(offset)
        return handle
