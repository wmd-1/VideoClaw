"""In-process use leases (pins) and delete claims for local files and objects.

A single lock guards every map so a reader cannot pin a target between the
cleaner's "not pinned" check and the physical delete:
- readers/uploaders `pin()` before touching a target and must honour a False result;
- the cleaner `claim()`s the exact target before deleting and `release()`s afterwards;
- an execution work root is removed only under `claim_prefix()`, which fails while any
  file below it is pinned and blocks new pins below it until released.

Identities: local files use their realpath; objects use ``s3://<bucket>/<key>``.
This module has no DB/storage dependency so media_refs, publisher and the cleaner
can all import it without cycles.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Dict, Iterable, Iterator, List


class TargetBeingDeleted(RuntimeError):
    """The target is currently claimed for deletion; the caller must not use it."""


def local_identity(path: str) -> str:
    return os.path.realpath(str(path))


def object_identity(bucket: str, object_key: str) -> str:
    return f"s3://{bucket}/{object_key}"


def _under(identity: str, prefix: str) -> bool:
    return identity == prefix or identity.startswith(prefix.rstrip(os.sep) + os.sep)


class ResourceClaims:
    def __init__(self) -> None:
        self._pins: Dict[str, int] = {}
        self._deleting: set[str] = set()
        self._deleting_prefixes: set[str] = set()
        self._lock = threading.Lock()

    def _blocked(self, identity: str) -> bool:
        return identity in self._deleting or any(_under(identity, p) for p in self._deleting_prefixes)

    def pin(self, identity: str) -> bool:
        with self._lock:
            if self._blocked(identity):
                return False
            self._pins[identity] = self._pins.get(identity, 0) + 1
            return True

    def unpin(self, identity: str) -> None:
        with self._lock:
            count = self._pins.get(identity, 0) - 1
            if count > 0:
                self._pins[identity] = count
            else:
                self._pins.pop(identity, None)

    def is_pinned(self, identity: str) -> bool:
        with self._lock:
            return identity in self._pins

    def claim(self, identity: str) -> bool:
        return self.claim_all([identity])

    def claim_all(self, identities: Iterable[str]) -> bool:
        """Claim every identity or none (all-or-nothing under one lock)."""
        wanted = list(dict.fromkeys(identities))
        with self._lock:
            if any(self._pins.get(item) or self._blocked(item) for item in wanted):
                return False
            self._deleting.update(wanted)
            return True

    def release(self, identity: str) -> None:
        self.release_all([identity])

    def release_all(self, identities: Iterable[str]) -> None:
        with self._lock:
            for identity in identities:
                self._deleting.discard(identity)

    def claim_prefix(self, prefix: str) -> bool:
        """Claim a directory subtree: fails while anything below it is pinned or claimed."""
        with self._lock:
            if any(_under(item, prefix) for item in self._pins):
                return False
            if any(_under(item, prefix) for item in self._deleting):
                return False
            if any(_under(prefix, p) or _under(p, prefix) for p in self._deleting_prefixes):
                return False
            self._deleting_prefixes.add(prefix)
            return True

    def release_prefix(self, prefix: str) -> None:
        with self._lock:
            self._deleting_prefixes.discard(prefix)

    @contextmanager
    def pinned(self, identities: Iterable[str]) -> Iterator[List[str]]:
        """Pin all identities for the block; raise if any is being deleted."""
        acquired: List[str] = []
        try:
            for identity in dict.fromkeys(identities):
                if not self.pin(identity):
                    raise TargetBeingDeleted(identity)
                acquired.append(identity)
            yield acquired
        finally:
            for identity in acquired:
                self.unpin(identity)


CLAIMS = ResourceClaims()

__all__ = [
    "CLAIMS",
    "ResourceClaims",
    "TargetBeingDeleted",
    "local_identity",
    "object_identity",
]
