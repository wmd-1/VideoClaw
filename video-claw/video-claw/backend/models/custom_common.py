"""自定义模型协议适配共享工具（openai / vllm-omni / sglang）。

包含：协议常量、尺寸映射、base_url 规范化、httpx 客户端工厂（零鉴权兼容）、
本地文件 → data URL、模型级错误分类。
"""

import base64
import logging
import mimetypes
import os
import re
import sys
from typing import Any, Dict, Optional

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import httpx

from config import Config

logger = logging.getLogger(__name__)

PROTOCOLS = ("openai", "vllm-omni", "sglang")
DEFAULT_HTTP_TIMEOUT = 300.0
DEFAULT_VIDEO_POLL_INTERVAL = 5.0
DEFAULT_VIDEO_POLL_TIMEOUT = 1800.0
VIDEO_JOB_SUCCESS_STATES = {"completed", "succeeded", "success", "done"}
VIDEO_JOB_FAILURE_STATES = {"failed", "error", "cancelled", "canceled"}

# 图生图 multipart 字段名按协议适配时使用的回退状态码（这些码尝试下一个字段名/编码）
RETRYABLE_STATUS_CODES = {400, 404, 405, 415, 422}


class ModelNotRegisteredError(RuntimeError):
    """模型既不在内置注册表、也不在自定义模型列表。"""

    def __init__(self, model: str):
        super().__init__(f"模型 {model} 未注册（可在设置页注册为自定义模型，或检查模型名称）")
        self.model = model


_IMAGE_SIZE_MAP: Dict[str, Dict[str, str]] = {
    "16:9": {"720P": "1280*720", "1080P": "1920*1080", "2K": "2560*1440", "4K": "3840*2160"},
    "9:16": {"720P": "720*1280", "1080P": "1080*1920", "2K": "1440*2560", "4K": "2160*3840"},
    "4:3": {"720P": "960*720", "1080P": "1440*1080", "2K": "2560*1920", "4K": "3840*2880"},
    "3:4": {"720P": "720*960", "1080P": "1080*1440", "2K": "1920*2560", "4K": "2880*3840"},
    "1:1": {"720P": "720*720", "1080P": "1080*1080", "2K": "2560*2560", "4K": "3840*3840"},
}

_VIDEO_SIZE_MAP: Dict[str, Dict[str, str]] = {
    "16:9": {"720P": "1280x720", "1080P": "1920x1080", "2K": "2560x1440", "4K": "3840x2160"},
    "9:16": {"720P": "720x1280", "1080P": "1080x1920", "2K": "1440x2560", "4K": "2160x3840"},
    "1:1": {"720P": "720x720", "1080P": "1080x1080"},
    "4:3": {"720P": "960x720", "1080P": "1440x1080"},
    "3:4": {"720P": "720x960", "1080P": "1080x1440"},
    "21:9": {"720P": "1680x720", "1080P": "2520x1080"},
}

_SIZE_PATTERN = re.compile(r"^\d+[x*]\d+$")

# 模型级错误标记：鉴权失败 / 模型不存在 / 连接失败 / 超时等
_MODEL_UNAVAILABLE_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "authentication",
    "api key",
    "invalid key",
    "no permission",
    "insufficient",
    "认证",
    "鉴权",
    "密钥",
    "model not found",
    "model_not_found",
    "model does not exist",
    "unknown model",
    "模型不存在",
    "connection",
    "connect error",
    "refused",
    "timed out",
    "timeout",
    "time out",
    "name resolution",
    "无法连接",
    "连接失败",
    "连接超时",
    "连接被拒",
)


def image_size(video_ratio: str, resolution: str) -> str:
    """ratio + resolution → 图像尺寸（`W*H`）；支持直接传入 `WxH`/`W*H`。"""
    if isinstance(resolution, str) and _SIZE_PATTERN.match(resolution):
        return resolution.replace("x", "*")
    ratio_map = _IMAGE_SIZE_MAP.get(video_ratio or "", _IMAGE_SIZE_MAP["16:9"])
    return ratio_map.get(resolution or "", "1920*1080")


def video_size(video_ratio: str, resolution: Optional[str]) -> str:
    """ratio + resolution → 视频尺寸（`WxH`）；支持直接传入 `WxH`/`W*H`。"""
    if isinstance(resolution, str) and _SIZE_PATTERN.match(resolution):
        return resolution.replace("*", "x")
    ratio_map = _VIDEO_SIZE_MAP.get(video_ratio or "", _VIDEO_SIZE_MAP["16:9"])
    return ratio_map.get((resolution or "720P").upper(), "1280x720")


def video_dimensions(video_ratio: str, resolution: Optional[str]) -> tuple:
    """ratio + resolution → (width, height) 数值；供需要独立宽高字段的协议使用。"""
    width_str, height_str = video_size(video_ratio, resolution).split("x", 1)
    return int(width_str), int(height_str)


def normalize_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def make_http_client(
    base_url: str,
    api_key: str = "",
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    proxy: str = "",
) -> httpx.Client:
    """创建 httpx 客户端；`api_key` 为空时不发送 Authorization 头（兼容零鉴权本地服务）。"""
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # 连接阶段独立短超时：目标不可达（地址写错/服务未启动等）时快速失败，
    # 避免连接挂起占满整个读取超时（生成类服务常见 600s）而长期阻塞工作流。
    connect_timeout = min(15.0, float(timeout))
    kwargs: Dict[str, Any] = {
        "base_url": normalize_base_url(base_url),
        "headers": headers,
        "timeout": httpx.Timeout(timeout, connect=connect_timeout),
    }
    if proxy:
        try:
            return httpx.Client(proxy=proxy, **kwargs)
        except TypeError:  # 兼容旧版 httpx
            kwargs["proxies"] = proxy
    return httpx.Client(**kwargs)


def guess_mime_type(path: str) -> str:
    mime, _ = mimetypes.guess_type(path or "")
    return mime or "application/octet-stream"


def to_data_url(path_or_url: str, default_mime: str = "image/png") -> str:
    """本地路径 → data URL；已是 http(s)/data URL 则原样返回。"""
    value = str(path_or_url or "")
    if value.startswith(("http://", "https://", "data:")):
        return value
    local_path = value
    if local_path.startswith("file://"):
        local_path = local_path[len("file://"):]
        if re.match(r"^/[A-Za-z]:", local_path):
            local_path = local_path[1:]
    with open(local_path, "rb") as f:
        payload = base64.b64encode(f.read()).decode("ascii")
    return f"data:{guess_mime_type(local_path) or default_mime};base64,{payload}"


def resolve_custom_meta(model: str) -> Dict[str, Any]:
    """解析自定义模型条目；非自定义（内置/未注册）时抛出 ModelNotRegisteredError。"""
    from models.config_model import resolve_model_entry

    kind, metadata = resolve_model_entry(model)
    if kind != "custom":
        raise ModelNotRegisteredError(model)
    return metadata


def classify_model_unavailable(exc: Exception) -> bool:
    """模型级错误识别：鉴权失败 / 模型不存在 / 连接失败 / 超时 → True。"""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else 0
        if status in (401, 403):
            return True
        if status == 404 and "model" in str(exc).lower():
            return True
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.TimeoutException,
            httpx.ProxyError,
        ),
    ):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _MODEL_UNAVAILABLE_MARKERS)


def connection_hint(exc: Exception, base_url: str) -> str:
    """连接类失败且目标为回环地址时，提示 Docker 容器网络的典型陷阱。"""
    if not isinstance(
        exc,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TimeoutException, httpx.ProxyError),
    ):
        return ""
    lowered = str(base_url or "").lower()
    if any(host in lowered for host in ("127.0.0.1", "localhost", "::1")):
        return "（提示：后端运行在 Docker 容器中时，容器内的 127.0.0.1/localhost 指向容器自身，请改用宿主机局域网 IP）"
    return ""


def build_custom_client_kwargs(meta: Dict[str, Any], timeout: float = DEFAULT_HTTP_TIMEOUT) -> Dict[str, Any]:
    """根据注册表条目组装 httpx 客户端参数（含供应商代理）。"""
    provider_key = str(meta.get("provider") or "")
    return {
        "base_url": meta.get("base_url", ""),
        "api_key": meta.get("api_key", ""),
        "timeout": timeout,
        "proxy": Config.provider_proxy(provider_key) if provider_key else "",
    }