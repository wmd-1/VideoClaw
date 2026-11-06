"""自定义图像客户端：`images/generations` 与 `images/edits`（openai / vllm-omni / sglang）。

响应支持 b64_json、绝对/相对 URL（相对路径按 base_url 解析并携带鉴权头）、二进制文件三种形态；
图生图 multipart 字段名按协议适配并做一次回退尝试（openai: image[]→image；vllm-omni: image→image[]／url；
sglang: image→url(data URL)）。

能力声明驱动的参数注入（openspec: custom-image-adapter-fidelity）：模型可在 capabilities 声明
num_inference_steps/true_cfg_scale/seed/sigmas/max_reference_images/size_alignment/turbo，
未声明时请求字段与旧行为逐键等价。
"""

import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import httpx  # noqa: E402 - sys.path 引导后导入

from config import Config  # noqa: E402 - sys.path 引导后导入
from models.custom_common import (  # noqa: E402 - sys.path 引导后导入
    RETRYABLE_STATUS_CODES,
    build_custom_client_kwargs,
    connection_hint,
    guess_mime_type,
    image_size,
    make_http_client,
    parse_image_decl,
    to_data_url,
)

logger = logging.getLogger(__name__)

# 远程参考图落盘上限：与官方 file part 无硬限制、url 字段 1024KB/part 的口径对比后取安全上限
MAX_REF_BYTES = 32 * 1024 * 1024
# 远程参考图下载超时（与生成超时区分：只读一个图片文件，不应占用 3 小时额度）
REF_FETCH_TIMEOUT = 60.0
REF_FETCH_CHUNK = 256 * 1024
# size_alignment 取整后的尺寸下限（低于此值属“参数合法但结果非法”，发送前拒绝）
MIN_ALIGNED_DIMENSION = 64
_IMAGE_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


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
        # 能力声明只保留合法项（非法/越界已在此记录并丢弃，等同未声明）
        self._decl = parse_image_decl(meta.get("capabilities"))
        self._temp_refs: List[str] = []

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
        size = self._resolve_size(video_ratio, resolution)
        if not save_dir:
            save_dir = os.path.join(Config.RESULT_DIR, "image", "custom", str(session_id or "default"))
        os.makedirs(save_dir, exist_ok=True)

        try:
            if image_paths:
                items = self._edit(prompt, image_paths, size, session_id)
            else:
                items = self._generate(prompt, size)
        finally:
            self._cleanup_temp_refs()
        return self._save_items(items, save_dir)

    # ── 能力声明驱动的请求字段 ──

    def _resolve_size(self, video_ratio: str, resolution: str) -> str:
        """档位尺寸；声明 size_alignment 时向下取整到粒度倍数并记录比例偏移。"""
        size = image_size(video_ratio, resolution)
        alignment = self._decl.get("size_alignment")
        if not alignment:
            return size
        width_str, height_str = size.split("*")
        width, height = int(width_str), int(height_str)
        aligned_w = (width // alignment) * alignment
        aligned_h = (height // alignment) * alignment
        if aligned_w < MIN_ALIGNED_DIMENSION or aligned_h < MIN_ALIGNED_DIMENSION:
            # 声明合法但结果非法：发送前拒绝，不发出必然失败的请求
            raise RuntimeError(
                f"自定义图像模型 {self._model} 拒绝发送：size_alignment={alignment} 取整后尺寸为 "
                f"{aligned_w}x{aligned_h}，低于下限 {MIN_ALIGNED_DIMENSION}（请求档位 {width}x{height}）"
            )
        if (aligned_w, aligned_h) != (width, height):
            before = width / height
            after = aligned_w / aligned_h
            logger.warning(
                "image size aligned by size_alignment=%s: %sx%s -> %sx%s (ratio %.4f -> %.4f, deviation %.2f%%)",
                alignment, width, height, aligned_w, aligned_h, before, after, abs(after - before) / before * 100,
            )
        return f"{aligned_w}*{aligned_h}"

    def _sampling_fields(self) -> Dict[str, Any]:
        """按声明生成采样字段；turbo 权重不下发步数，显式 sigmas 优先于步数。"""
        fields: Dict[str, Any] = {}
        if "sigmas" in self._decl:
            fields["sigmas"] = self._decl["sigmas"]
            if "num_inference_steps" in self._decl:
                logger.warning(
                    "capabilities.num_inference_steps ignored for %s: explicit sigmas schedule wins",
                    self._model,
                )
        elif self._decl.get("turbo"):
            if "num_inference_steps" in self._decl:
                logger.warning(
                    "capabilities.num_inference_steps ignored for %s: turbo checkpoint ships its own schedule",
                    self._model,
                )
        elif "num_inference_steps" in self._decl:
            fields["num_inference_steps"] = self._decl["num_inference_steps"]
        if "true_cfg_scale" in self._decl:
            fields["true_cfg_scale"] = self._decl["true_cfg_scale"]
        if "seed" in self._decl:
            fields["seed"] = self._decl["seed"]
        return fields

    # ── 文生图 ──

    def _generate(self, prompt: str, size: str) -> List[Any]:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": size.replace("*", "x"),
            "response_format": "b64_json",
        }
        # 未声明任何采样参数时，payload 键集合与旧行为逐键等价
        payload.update(self._sampling_fields())
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

    def _edit(self, prompt: str, image_paths: List[str], size: str,
              session_id: Optional[str] = None) -> List[Any]:
        last_error = ""
        saw_400 = False
        # 按声明上限收敛（未声明时不裁剪），再把远程形态落盘为本地文件
        paths = self._trim_reference_images(image_paths)
        paths = self._materialize_refs(paths, session_id)
        variants = self._edit_field_variants()
        for field_name, mode in variants:
            try:
                response = self._post_edit(prompt, paths, size, field_name, mode)
                if response.status_code < 400:
                    return self._extract_items(response)
                saw_400 = saw_400 or response.status_code == 400
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "Custom image edit with field '%s' failed (%s), trying fallback",
                    field_name,
                    response.status_code,
                )
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
            except (httpx.HTTPError, OSError) as exc:
                # OSError 兜底：file 模式对不存在/不可读路径 open() 抛 FileNotFoundError，
                # 需落入回退而不是直接冒泡终结请求（评审 I-1）
                last_error = str(exc)
        raise RuntimeError(
            f"自定义图生图模型 {self._model} 调用失败（目标 {self._base_url}/images/edits）: {last_error}"
            + self._limit_hint(saw_400, len(paths))
        )

    def _limit_hint(self, saw_400: bool, count: int) -> str:
        """未声明上限且服务端因超限拒绝时，把静默失败变成可诊断失败（tasks 3.3）。"""
        if saw_400 and count > 1 and "max_reference_images" not in self._decl:
            return ("（服务端拒绝对多图请求；可在模型 capabilities 声明 max_reference_images，"
                    "适配层将按上限收敛）")
        return ""

    def _trim_reference_images(self, image_paths: List[str]) -> List[str]:
        """按声明上限顺序保留参考图；未声明时不裁剪（与现状等价）。"""
        paths = list(image_paths)
        limit = self._decl.get("max_reference_images")
        if not limit or len(paths) <= limit:
            return paths
        kept = paths[:limit]
        logger.warning(
            "reference images trimmed for %s: input=%d sent=%d dropped=%d limit=max_reference_images:%d "
            "reason=backend reference limit",
            self._model, len(paths), len(kept), len(paths) - len(kept), limit,
        )
        return kept

    def _materialize_refs(self, image_paths: List[str], session_id: Optional[str]) -> List[str]:
        """远程形态（http(s)/data:）先落盘为本地文件，以文件 part 送达。

        官方对 `url` 表单字段限 1024KB/part（实测 1024×1024 PNG 的 data URL 会超限），
        而 file part 无该限制；落盘失败时发送前即拒绝，不伪造不存在的文件路径。
        """
        result: List[str] = []
        for index, path in enumerate(image_paths):
            text = str(path or "")
            if text.startswith("data:"):
                result.append(self._write_data_url_ref(text, index, session_id))
            elif text.startswith(("http://", "https://")):
                result.append(self._download_ref(text, index, session_id))
            else:
                result.append(text)
        return result

    def _refs_cache_dir(self, session_id: Optional[str] = None) -> str:
        """会话级临时目录：只取安全字符的会话名，避免 session_id 带 `../` 时越出临时根。"""
        token = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "default"))[:64].strip(".") or "default"
        dir_path = os.path.join(Config.TEMP_DIR, "image_refs", token, uuid.uuid4().hex[:8])
        os.makedirs(dir_path, exist_ok=True)
        return dir_path

    def _write_data_url_ref(self, data_url: str, index: int, session_id: Optional[str] = None) -> str:
        header, _, payload = data_url.partition(",")
        mime = header.split(":")[-1].split(";")[0] if ":" in header else "image/png"
        try:
            content = base64.b64decode(payload)
        except Exception as exc:  # noqa: BLE001 - 必须回为可读错误
            raise RuntimeError(
                f"自定义图像模型 {self._model} 参考图落盘失败（第 {index + 1} 张 data URL）: {exc}"
            ) from exc
        if len(content) > MAX_REF_BYTES:
            raise RuntimeError(
                f"自定义图像模型 {self._model} 参考图落盘失败（第 {index + 1} 张 data URL）: "
                f"体积 {len(content)} 字节 > 上限 {MAX_REF_BYTES} 字节"
            )
        path = os.path.join(self._refs_cache_dir(session_id), f"ref_{index + 1}{_IMAGE_EXT_BY_MIME.get(mime, '.png')}")
        with open(path, "wb") as handle:
            handle.write(content)
        self._temp_refs.append(path)
        return path

    def _download_ref(self, url: str, index: int, session_id: Optional[str]) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(
                f"自定义图像模型 {self._model} 参考图落盘失败（第 {index + 1} 张 {url}）: 非法的远程地址"
            )
        try:
            content, content_type = self._fetch_remote_bytes(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"自定义图像模型 {self._model} 参考图落盘失败（第 {index + 1} 张 {url}）: {exc}"
            ) from exc
        ext = _IMAGE_EXT_BY_MIME.get(content_type, os.path.splitext(parsed.path)[1] or ".png")
        name = hashlib.sha256(url.encode()).hexdigest()[:16] + (ext if ext.startswith(".") else ".png")
        path = os.path.join(self._refs_cache_dir(session_id), name)
        with open(path, "wb") as handle:
            handle.write(content)
        self._temp_refs.append(path)
        return path

    def _ref_http_client(self) -> httpx.Client:
        """远程参考图下载客户端：沿用供应商代理配置，但**不携带模型服务鉴权头**。

        参考图主机与推理服务不同主机，带上 api_key 会把凭据发给任意 URL；
        代理沿用 Config.provider_proxy，保证与其余请求一致的出网路径。
        """
        provider_key = str(self._meta.get("provider") or "")
        proxy = Config.provider_proxy(provider_key) if provider_key else ""
        return make_http_client(base_url="", api_key="", timeout=REF_FETCH_TIMEOUT, proxy=proxy)

    def _fetch_remote_bytes(self, url: str) -> Tuple[bytes, str]:
        """流式拉取远程参考图：先按 content-length 预检，再逐块累计并在超限时立即中断。

        非图片 content-type 与超限都在读取阶段拒绝（不先吃满内存再判），错误信息含模型 id
        与原因；SSRF 风险（可访问内网/元数据地址）已在 design 风险表登记。
        """
        with self._ref_http_client() as client:
            with client.stream("GET", url, follow_redirects=True) as response:
                response.raise_for_status()
                content_type = str(response.headers.get("content-type", "")).split(";")[0].strip()
                if not content_type.startswith("image/"):
                    raise RuntimeError(
                        f"自定义图像模型 {self._model} 参考图落盘失败（{url}）: "
                        f"响应不是图片（content-type={content_type or '未知'}）"
                    )
                declared = str(response.headers.get("content-length", "")).strip()
                if declared.isdigit() and int(declared) > MAX_REF_BYTES:
                    raise RuntimeError(
                        f"自定义图像模型 {self._model} 参考图落盘失败（{url}）: 声明体积 "
                        f"{int(declared)} 字节 > 上限 {MAX_REF_BYTES} 字节"
                    )
                chunks: List[bytes] = []
                total = 0
                for chunk in response.iter_bytes(chunk_size=REF_FETCH_CHUNK):
                    total += len(chunk)
                    if total > MAX_REF_BYTES:
                        raise RuntimeError(
                            f"自定义图像模型 {self._model} 参考图落盘失败（{url}）: 实际体积超过上限 "
                            f"{MAX_REF_BYTES} 字节，已中断读取"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), content_type

    def _cleanup_temp_refs(self) -> None:
        """任务结束后清理本次生成的临时副本（包括其空目录）。"""
        for path in self._temp_refs:
            try:
                os.remove(path)
                parent = os.path.dirname(path)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                logger.debug("临时参考图清理失败（忽略）: %s", path)
        self._temp_refs.clear()

    def _edit_field_variants(self) -> List[Tuple[str, str]]:
        if self._protocol == "openai":
            # 远程输入已先落盘，因此 file 变体为主形态；url(data URL) 仅作显式回退（服务端接受性未取证）
            return [("image[]", "file"), ("image", "file"), ("url", "dataurl")]
        if self._protocol == "sglang":
            return [("image", "file"), ("url", "dataurl")]
        # vllm-omni（默认）：文件 part 为主形态（image → image[]），url data URL 仅作显式回退
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
        # 与文生图共用同一组声明值；列表型字段（sigmas）在表单字段里按 JSON 编码下发
        for key, value in self._sampling_fields().items():
            data[key] = json.dumps(value) if isinstance(value, (list, tuple)) else value
        if mode == "dataurl":
            # 全部图片转为 data URL；同名多值用 list-of-tuples（httpx 0.28 不接受 dict 值传 list）
            values = [to_data_url(path) for path in image_paths]
            files = [(field_name, (None, value)) for value in values]
            return self._client.post("/images/edits", data=data, files=files)

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