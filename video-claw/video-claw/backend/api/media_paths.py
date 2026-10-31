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
from core.media_refs import ResolvedMedia

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


def guard_media_input(value: str, field: str, owner_key: str | None = None) -> str:
    """媒体入参统一守卫：远程形态原样透传，本地路径限定允许根白名单。

    ``file://`` 是本地读取形态，剥前缀后与裸路径同口径走白名单；相对路径按
    CODE_DIR 与 CODE_DIR/result 解析（与既有 sandbox 行为一致）。
    """
    if isinstance(value, ResolvedMedia) and value.trusted:
        return str(value)
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
    if owner_key:
        temp_root = os.path.realpath(settings.TEMP_DIR)
        if safe == temp_root or safe.startswith(temp_root + os.sep):
            rel = os.path.relpath(safe, temp_root).replace(os.sep, "/")
            if rel.startswith("users/") and not rel.startswith(f"users/{owner_key}/"):
                raise ValueError(f"{field} 不属于当前用户")
    return safe


def guard_media_inputs(
    values: Optional[List[str]], field: str, owner_key: str | None = None
) -> List[str]:
    """逐条套用统一守卫，保留输入顺序；空值项直接丢弃。"""
    return [
        guard_media_input(item, field, owner_key)
        for item in (values or [])
        if (item or "").strip()
    ]


def guard_uploaded_document_name(value: str) -> str:
    """上传文档引用只接受 TEMP_DIR 内、users/<owner_key>/ 下的相对路径。

    兼容 legacy 裸文件名（无分隔符）；调用方仍需 join TEMP_DIR 后 realpath
    复核包含性并断言 owner_key。
    """
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("file_path 不能为空")
    if os.path.isabs(raw) or ".." in raw.split("/"):
        raise ValueError("file_path 仅接受 /api/upload_file 返回的相对文件名")
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in (".", "") for part in parts):
        raise ValueError("file_path 仅接受 /api/upload_file 返回的相对文件名")
    return "/".join(parts)


def resolve_uploaded_document(file_path: str, owner_prefix: Optional[str] = None) -> str:
    """上传文档引用的统一入口：相对名 → TEMP_DIR 内绝对路径，并校验归属。

    创建（project start）与消费（剧本导入）两个入口 MUST 共用本函数，避免只在一
    侧校验。``owner_prefix`` 为空时不比归属，仅供无法取得会话归属的旧调用；正常
    业务路径必须传入 ``CurrentUser.owner_key`` 或会话 owner 派生的 owner_key。
    """
    safe_name = guard_uploaded_document_name(file_path)
    if owner_prefix and not safe_name.startswith(f"users/{owner_prefix}/"):
        # legacy 裸文件名仅在 single 模式按既定兼容规则放行
        legacy_bare = "/" not in safe_name and str(getattr(settings, "USERS_MODE", "guip")) == "single"
        if not legacy_bare:
            raise ValueError("file_path 不属于当前用户")
    temp_root = os.path.realpath(settings.TEMP_DIR)
    full_path = os.path.realpath(os.path.join(temp_root, safe_name))
    if full_path != temp_root and not full_path.startswith(temp_root + os.sep):
        raise ValueError("file_path 路径越界（仅允许上传目录内的文件）")
    return full_path
