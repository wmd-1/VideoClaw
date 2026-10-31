"""Media reference value object shared by model adapters.

Must not import db/storage/repositories: adapters only consume what core prepared.
Protocol selection filters by URI scheme so an adapter never receives a form it
cannot handle (e.g. `data:` or `oss:` handed to an HTTP-only endpoint).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence
from urllib.parse import urlparse

HTTP_SCHEMES = ("http", "https")
REMOTE_SCHEMES = ("http", "https", "oss", "s3", "data")


class UnsupportedReference(ValueError):
    """The adapter protocol cannot consume the prepared reference."""


PROTOCOLS = ("file_or_multipart", "http_uri", "remote_uri")


def scheme_of(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    return (parsed.scheme or "").lower()


@dataclass(frozen=True)
class MediaReference:
    public_path: str = ""
    local_path: str = ""
    remote_uri: str = ""
    uri_candidates: Sequence[str] = ()
    # 只在内存中存在；每次重试取新候选，绝不序列化进 DB
    presign_factory: Optional[Callable[[], List[str]]] = field(
        default=None, compare=False, repr=False
    )

    def candidates(self) -> List[str]:
        if self.remote_uri:
            return [self.remote_uri]
        if self.presign_factory is not None:
            return list(self.presign_factory())
        return list(self.uri_candidates)


def select_for_protocol(protocol: str, ref: MediaReference) -> dict:
    """按适配器协议选择可用形态；不可用即失败，不做静默降级。"""
    if protocol == "remote_uri":
        if not ref.remote_uri:
            raise UnsupportedReference("该协议只接受远程 URI，当前引用是本地/受管对象")
        if scheme_of(ref.remote_uri) not in REMOTE_SCHEMES:
            raise UnsupportedReference(f"不支持的远程 scheme: {ref.remote_uri}")
        return {"remote_uri": ref.remote_uri}
    if protocol == "http_uri":
        uris = [uri for uri in ref.candidates() if scheme_of(uri) in HTTP_SCHEMES]
        if not uris:
            raise UnsupportedReference("无可用 HTTP(S) URI（预签名失败或被 scheme 过滤）")
        return {"http_uri": uris[0], "uri_candidates": uris}
    if protocol == "file_or_multipart":
        if not ref.local_path:
            raise UnsupportedReference("需要本地文件（multipart/data URL）但未物化")
        return {"local_path": ref.local_path}
    raise UnsupportedReference(f"未知媒体协议: {protocol}")
