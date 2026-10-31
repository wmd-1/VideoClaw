"""Publication context for pipeline tasks and sandbox records (design D11/D16).

Each task / sandbox request is one execution (execution_id == resource id; a retry is
a new resource), so its work root is
`CODE/work/executions/<owner_key>/<domain>/<resource_id>/<resource_id>/`.

- outputs are trusted only inside that root; materialized inputs map back to their
  canonical value through `runtime_map`;
- the same local file is uploaded once: a successful commit records local → public,
  later artifacts / the final output reuse it (`committed`);
- `publish()` never touches the DB rows of the resource itself: the caller commits the
  returned file rows + operation ids in its own transaction and then calls
  `mark_committed`, or `abort` on failure (compensates only this batch).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core import media_refs
from core.artifact_publisher import (
    PublicationManifest,
    abort_publication,
    assert_plain_work_root,
    build_file_rows,
    execution_work_root,
    publish,
)

logger = logging.getLogger(__name__)


def runtime_map_of(canonical: Any, runtime: Any) -> Dict[str, str]:
    """同构遍历 canonical 与物化副本：物化值 → canonical（只收集真正被替换的叶子）。"""
    mapping: Dict[str, str] = {}

    def walk(left: Any, right: Any) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key, value in left.items():
                if key in right:
                    walk(value, right[key])
        elif isinstance(left, list) and isinstance(right, list):
            for a, b in zip(left, right):
                walk(a, b)
        elif isinstance(left, str) and right is not None and str(right) != left and left.strip():
            mapping[str(right)] = left

    walk(canonical, runtime)
    return mapping


class ResourcePublisher:
    def __init__(
        self,
        *,
        owner: str,
        domain: str,
        resource_id: str,
        stage: str,
        boot_id: str = "",
        runtime_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.owner = owner
        self.owner_key = media_refs.owner_key(owner)
        self.domain = domain
        self.resource_id = resource_id
        self.execution_id = resource_id
        self.stage = stage
        self.boot_id = boot_id
        self.runtime_map: Dict[str, str] = dict(runtime_map or {})
        self.work_root = execution_work_root(self.owner_key, domain, resource_id, self.execution_id)
        self.committed: Dict[str, str] = {}
        self._lock = threading.RLock()

    def ensure_root(self) -> str:
        assert_plain_work_root(self.work_root)
        os.makedirs(self.work_root, exist_ok=True)
        assert_plain_work_root(self.work_root)
        return self.work_root

    def _inside_root(self, path: str) -> bool:
        real = os.path.realpath(path)
        return real != self.work_root and real.startswith(self.work_root + os.sep)

    def _within_root(self, path: str) -> bool:
        real = os.path.realpath(path)
        return real == self.work_root or real.startswith(self.work_root + os.sep)

    def is_output(self, value: Any) -> bool:
        text = str(value or "")
        return bool(text) and os.path.isabs(text) and self._inside_root(text)

    def resolve(self, value: Any) -> Optional[str]:
        """已知值的公开形态：已提交的工作文件 → public；物化输入 → canonical；其余 None。"""
        text = str(value or "")
        if not text:
            return None
        if os.path.isabs(text):
            known = self.committed.get(os.path.realpath(text))
            if known:
                return known
        if text in self.runtime_map:
            return self.runtime_map[text]
        if isinstance(value, media_refs.ResolvedMedia) and str(getattr(value, "public_path", "") or "").startswith(media_refs.PUBLIC_PREFIX):
            return str(value.public_path)
        return None

    def publish(self, refs: Iterable[Tuple[Any, str]]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
        """发布尚未提交的工作文件（及受信输入）；返回 (发布结果, 文件行, 原值→public 映射)。"""
        with self._lock:
            manifest = PublicationManifest(
                owner=self.owner,
                owner_key=self.owner_key,
                resource_kind=self.domain,
                resource_id=self.resource_id,
                stage=self.stage,
                execution_id=self.execution_id,
                work_root=self.work_root,
                runtime_map=self.runtime_map,
            )
            keys: Dict[str, str] = {}
            for ref, kind in refs:
                text = str(ref or "")
                if not text or self.resolve(ref) is not None or text in keys:
                    continue
                keys[text] = manifest.add(ref, kind=kind)
            if not any(not entry.get("remote") for entry in manifest.entries):
                return None, [], {text: key for text, key in keys.items()}
            result = publish(manifest, boot_id=self.boot_id)
            rows = build_file_rows(
                result, owner=self.owner, resource_kind=self.domain,
                resource_id=self.resource_id, stage=self.stage,
            )
            canonical_map = result.get("canonical_map") or {}
            mapping = {text: str(canonical_map.get(key, key)) for text, key in keys.items()}
            return result, rows, mapping

    def mark_committed(self, result: Optional[Dict[str, Any]]) -> None:
        with self._lock:
            for item in (result or {}).get("published") or []:
                local = str(item.get("local_path") or "")
                if local and not item.get("reused"):
                    self.committed[os.path.realpath(local)] = str(item["public_path"])

    @staticmethod
    def abort(result: Optional[Dict[str, Any]]) -> None:
        if not result:
            return
        try:
            issues = abort_publication(result)
        except Exception:  # 补偿失败不掩盖原因；残留仍有操作日志定位
            logger.exception("publication compensation raised")
            return
        if issues:
            logger.warning("publication compensation deferred: %s", issues)

    def map_output(self, value: Any) -> Tuple[Any, List[Tuple[str, str]]]:
        """输出结构里的路径叶子 → 公开值；返回 (映射后的结构, 需要发布的工作文件)。

        工作根内非文件（目录/不存在）不外露；根外绝对路径保留原样并告警（不猜测发布）。
        """
        pending: List[Tuple[str, str]] = []

        def walk(node: Any) -> Any:
            if isinstance(node, dict):
                return {key: walk(item) for key, item in node.items()}
            if isinstance(node, list):
                return [walk(item) for item in node]
            if isinstance(node, str) and node:
                known = self.resolve(node)
                if known is not None:
                    return known
                if self.is_output(node) and os.path.isfile(node):
                    pending.append((node, "output"))
                    return node
                if os.path.isabs(node) and self._within_root(node):
                    return ""
                if os.path.isabs(node):
                    logger.warning("output keeps a local path outside the work root: %s", node)
            return node

        return walk(value), pending

    def cleanup_root(self) -> bool:
        from core.object_operations import cleanup_execution_root

        try:
            return cleanup_execution_root(self.owner_key, self.domain, self.resource_id, self.execution_id)
        except Exception:
            logger.warning("execution root cleanup failed: %s/%s", self.domain, self.resource_id, exc_info=True)
            return False


__all__ = ["ResourcePublisher", "runtime_map_of"]
