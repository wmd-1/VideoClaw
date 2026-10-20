"""用户可控输入路径的统一守卫（openspec: user-input-path-guard）。

所有入口（sandbox、pipelines、project start）共用本模块：
- 远程形态按登记集合原样透传；
- 本地路径 realpath 后必须落在允许根（CODE_DIR/result 与 TEMP_DIR）；
- ``file://`` 剥前缀后按本地路径同口径校验；
- 上传文档只接受 TEMP_DIR 内的相对文件名。

允许根不含 ``CODE_DIR`` 本身与 ``CODE_DIR/data``：会话/任务元数据不得作为用户输入，
否则仍可经复制通道搬进公开的 ``result/`` 目录。
"""
import os
import re
from typing import List, Optional

from config import settings

# 远程媒体形态登记集合（判定大小写不敏感）。file:// 永不入集合：适配层会本地 open。
REMOTE_MEDIA_PREFIXES = ("http://", "https://", "data:", "oss://")
FILE_URI_PREFIX = "file://"
_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def get_allowed_media_roots() -> List[str]:
    """允许作为用户输入的根目录（均已 realpath）：媒体产物目录与临时上传目录。"""
    return [
        os.path.realpath(os.path.join(settings.CODE_DIR, "result")),
        os.path.realpath(settings.TEMP_DIR),
    ]


def resolve_within_allowed_dirs(path: str) -> Optional[str]:
    """将候选路径 realpath 规范化；落在允许根内返回规范化绝对路径，否则返回 None。"""
    if not path:
        return None
    real = os.path.realpath(path)
    for root in get_allowed_media_roots():
        if real == root or real.startswith(root + os.sep):
            return real
    return None


def remote_media_kind(value: str) -> Optional[str]:
    """返回命中的远程形态前缀（原登记写法）；非远程返回 None。大小写不敏感。"""
    lowered = str(value or "").strip().lower()
    for prefix in REMOTE_MEDIA_PREFIXES:
        if lowered.startswith(prefix):
            return prefix
    return None


def strip_file_uri(value: str) -> str:
    """file:// URI → 本地绝对路径（含 /C:/ Windows 形态，与 to_data_url 口径一致）。"""
    local = value[len(FILE_URI_PREFIX):]
    if re.match(r"^/[A-Za-z]:", local):
        local = local[1:]
    return local


def guard_media_input(value: str, field: str) -> str:
    """媒体入参统一守卫：远程形态原样透传，本地路径限定允许根白名单。

    ``file://`` 是本地读取形态，剥前缀后与裸路径同口径走白名单；相对路径按
    CODE_DIR 与 CODE_DIR/result 解析（与既有 sandbox 行为一致）。
    """
    value = (value or "").strip()
    if not value:
        return value
    if remote_media_kind(value):
        return value
    if value.startswith(FILE_URI_PREFIX):
        value = strip_file_uri(value)
    else:
        scheme = _SCHEME_PREFIX.match(value)
        if scheme:
            raise ValueError(
                f"{field} 不支持的协议前缀: {scheme.group(0)}"
                "（支持 http/https/data/oss 与 file://）"
            )
    candidates = [value] if os.path.isabs(value) else [
        os.path.join(os.path.realpath(settings.CODE_DIR), value),
        os.path.join(os.path.realpath(settings.CODE_DIR), "result", value),
    ]
    source = next((path for path in candidates if os.path.isfile(path)), None)
    if source is None:
        raise ValueError(f"{field} 文件不存在: {value}")
    safe = resolve_within_allowed_dirs(source)
    if not safe:
        raise ValueError(f"{field} 路径越界（仅允许 result/temp 目录内的文件）: {value}")
    return safe


def guard_media_inputs(values: Optional[List[str]], field: str) -> List[str]:
    """逐条套用统一守卫，保留输入顺序；空值项直接丢弃。"""
    return [guard_media_input(item, field) for item in (values or []) if (item or "").strip()]


def guard_uploaded_document_name(value: str) -> str:
    """上传文档引用只接受 TEMP_DIR 内的相对文件名（无分隔符、无 ..、非绝对路径）。

    只做形态校验；调用方仍需 join TEMP_DIR 后 realpath 复核包含性。
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("file_path 不能为空")
    if (
        os.path.isabs(raw)
        or os.path.basename(raw) != raw
        or "/" in raw
        or "\\" in raw
        or ".." in raw
    ):
        raise ValueError("file_path 仅接受 /api/upload_file 返回的相对文件名")
    return raw
