"""自定义图像客户端：`images/generations` 与 `images/edits`（openai / vllm-omni / sglang）。

响应支持 b64_json、绝对/相对 URL（相对路径按 base_url 解析并携带鉴权头）、二进制文件三种形态；
图生图 multipart 字段名按协议适配并做一次回退尝试（openai: image[]→image；vllm-omni: image→image[]／url；
sglang: image→url(data URL)）。
"""

import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import httpx

from config import Config
from models.custom_common import (
    RETRYABLE_STATUS_CODES,
    build_custom_client_kwargs,
    connection_hint,
    guess_mime_type,
    image_size,
    make_http_client,
    to_data_url,
)

logger = logging.getLogger(__name__)


class CustomImageClient:
    """自定义文生图/图生图客户端（协议差异在内部适配）。"""

    def __init__(self, meta: Dict[str, Any], timeout: Optional[float] = None):
        self._meta = meta
        self._model = str(meta.get("request_model") or meta.get("id") or "")
        self._protocol = str(meta.get("protocol") or "")
        self._base_url = str(meta.get("base_url") or "")
        # 生成超时：默认取 Config.TIMEOUT_IMAGE（.env VC_TIMEOUT_IMAGE，缺省 3 小时）
        resolved_timeout = float(timeout if timeout is not None else Config.TIMEOUT_IMAGE)
        self._client = make_http_client(**build_custom_client_kwargs(meta, timeout=resolved_timeout))

    def generate_image(
        self,
        prompt: str,
        image_paths: Optional[List[str]] = None,
        save_dir: Optional[str] = None,
        session_id: Optional[str] = None,
        video_ratio: str = "16:9",
        resolution: str = "2K",
    ) -> List[str]:
        """生成图片并返回本地文件路径列表（与内置 ImageClient 约定一致）。"""
        size = image_size(video_ratio, resolution)
        if not save_dir:
            save_dir = os.path.join(Config.RESULT_DIR, "image", "custom", str(session_id or "default"))
        os.makedirs(save_dir, exist_ok=True)

        if image_paths:
            items = self._edit(prompt, image_paths, size)
        else:
            items = self._generate(prompt, size)
        return self._save_items(items, save_dir)

    # ── 文生图 ──

    def _generate(self, prompt: str, size: str) -> List[Any]:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": size.replace("*", "x"),
            "response_format": "b64_json",
        }
        try:
            response = self._client.post("/images/generations", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"自定义文生图模型 {self._model} 调用失败（目标 {self._base_url}/images/generations）: {exc}"
                f"{connection_hint(exc, self._base_url)}"
            ) from exc
        return self._extract_items(response)

    # ── 图生图 ──

    def _edit(self, prompt: str, image_paths: List[str], size: str) -> List[Any]:
        last_error = ""
        for field_name, mode in self._edit_field_variants():
            try:
                response = self._post_edit(prompt, image_paths, size, field_name, mode)
                if response.status_code < 400:
                    return self._extract_items(response)
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "Custom image edit with field '%s' failed (%s), trying fallback",
                    field_name,
                    response.status_code,
                )
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = str(exc)
        raise RuntimeError(
            f"自定义图生图模型 {self._model} 调用失败（目标 {self._base_url}/images/edits）: {last_error}"
        )

    def _edit_field_variants(self) -> List[Tuple[str, str]]:
        if self._protocol == "openai":
            return [("image[]", "file"), ("image", "file")]
        if self._protocol == "sglang":
            return [("image", "file"), ("url", "dataurl")]
        # vllm-omni（默认）：image 文件 → image[] 文件 → url data URL
        return [("image", "file"), ("image[]", "file"), ("url", "dataurl")]

    def _post_edit(
        self,
        prompt: str,
        image_paths: List[str],
        size: str,
        field_name: str,
        mode: str,
    ) -> httpx.Response:
        data = {
            "model": self._model,
            "prompt": prompt,
            "size": size.replace("*", "x"),
            "response_format": "b64_json",
        }
        if mode == "dataurl":
            # url 字段按 multipart 表单字段发送（无文件名），值使用 data URL
            value = to_data_url(image_paths[0])
            return self._client.post("/images/edits", data=data, files={field_name: (None, value)})

        handles = []
        try:
            files = []
            for path in image_paths:
                handle = open(path, "rb")
                handles.append(handle)
                files.append((field_name, (os.path.basename(path), handle, guess_mime_type(path))))
            return self._client.post("/images/edits", data=data, files=files)
        finally:
            for handle in handles:
                handle.close()

    # ── 响应解析与落盘 ─

    @staticmethod
    def _extract_items(response: httpx.Response) -> List[Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"自定义图像接口返回非 JSON 内容: {response.text[:200]}") from exc
        items = body.get("data")
        if not isinstance(items, list):
            items = body.get("images")
        return items if isinstance(items, list) else []

    def _save_items(self, items: List[Any], save_dir: str) -> List[str]:
        saved: List[str] = []
        for index, item in enumerate(items):
            path: Optional[str] = None
            if isinstance(item, dict):
                if item.get("b64_json"):
                    path = self._save_b64(str(item["b64_json"]), save_dir, index)
                elif item.get("url"):
                    path = self._download_url(str(item["url"]), save_dir, index)
                elif item.get("image_url"):
                    path = self._download_url(str(item["image_url"]), save_dir, index)
            elif isinstance(item, str):
                path = self._download_url(item, save_dir, index)
            if path:
                saved.append(path)
        if not saved:
            raise RuntimeError(f"自定义图像模型 {self._model} 未返回可保存的图片")
        return saved

    @staticmethod
    def _next_file_path(save_dir: str, index: int, ext: str) -> str:
        return os.path.join(
            save_dir,
            f"custom_{int(time.time())}_{uuid.uuid4().hex[:6]}_{index}{ext}",
        )

    def _save_b64(self, payload: str, save_dir: str, index: int) -> str:
        import base64

        data = payload.split(",", 1)[-1] if payload.startswith("data:") else payload
        try:
            content = base64.b64decode(data)
        except Exception as exc:
            raise RuntimeError(f"自定义图像模型 {self._model} 返回的 b64_json 无法解码: {exc}") from exc
        path = self._next_file_path(save_dir, index, ".png")
        with open(path, "wb") as f:
            f.write(content)
        return path

    def _download_url(self, url: str, save_dir: str, index: int) -> str:
        """下载结果图：绝对 URL 直接请求；以 `/` 开头的相对路径按域名根解析
        （如 sglang 返回的 `/v1/images/<id>/content`）；其余相对路径按 base_url 合并。
        """
        target = url
        if url.startswith("/"):
            base = str(self._client.base_url)
            if not base.endswith("/"):
                base += "/"
            target = urljoin(base, url)
        try:
            response = self._client.get(target)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"自定义图像模型 {self._model} 下载结果失败（目标 {self._base_url}）: {url} -> {exc}"
                f"{connection_hint(exc, self._base_url)}"
            ) from exc
        content_type = response.headers.get("content-type", "")
        ext = ".png"
        if "jpeg" in content_type or "jpg" in content_type:
            ext = ".jpg"
        elif "webp" in content_type:
            ext = ".webp"
        path = self._next_file_path(save_dir, index, ext)
        with open(path, "wb") as f:
            f.write(response.content)
        return path

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover
            pass