"""自定义视频客户端：OpenAI Videos 风格异步任务（openai / vllm-omni / sglang）。

三段式时序：POST 创建任务（multipart 为主形态，失败按协议回退其它编码）→ 轮询
`GET /v1/videos/{id}`（失败回退列表）→ `GET /v1/videos/{id}/content` 下载为 mp4。
与内置 VideoClient 约定一致：内容写入 `save_path` 并返回远端标识；轮询有超时上限；
失败时尽力取消任务。
"""

import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import httpx

from models.custom_common import (
    DEFAULT_VIDEO_POLL_INTERVAL,
    DEFAULT_VIDEO_POLL_TIMEOUT,
    RETRYABLE_STATUS_CODES,
    VIDEO_JOB_FAILURE_STATES,
    VIDEO_JOB_SUCCESS_STATES,
    build_custom_client_kwargs,
    connection_hint,
    guess_mime_type,
    make_http_client,
    video_size,
)

logger = logging.getLogger(__name__)


class CustomVideoClient:
    """自定义文生视频/图生视频客户端（协议差异在内部适配）。"""

    def __init__(
        self,
        meta: Dict[str, Any],
        poll_interval: float = DEFAULT_VIDEO_POLL_INTERVAL,
        poll_timeout: float = DEFAULT_VIDEO_POLL_TIMEOUT,
        timeout: float = 600.0,
    ):
        self._meta = meta
        self._model = str(meta.get("request_model") or meta.get("id") or "")
        self._protocol = str(meta.get("protocol") or "")
        self._base_url = str(meta.get("base_url") or "")
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._client = make_http_client(**build_custom_client_kwargs(meta, timeout=timeout))

    # 与内置 VideoClient.generate_video 保持签名/约定一致（调用方零改动）
    def generate_video(
        self,
        prompt: str,
        image_path: Optional[str],
        save_path: str,
        model: str = "",
        duration: int = 5,
        shot_type: str = "multi",
        sound: str = "",
        video_ratio: str = "16:9",
        resolution: Optional[str] = None,
        last_image_path: Optional[str] = None,
        first_clip_path: Optional[str] = None,
        reference_image_path: Optional[str] = None,
        reference_image_paths: Optional[List[str]] = None,
        reference_video_paths: Optional[List[str]] = None,
        reference_audio_path: Optional[str] = None,
        audio_path: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        prompt_extend: Optional[bool] = None,
        watermark: Optional[bool] = None,
        seed: Optional[int] = None,
        mode: str = "pro",
        cfg_scale: float = 0.5,
        generate_audio: Optional[bool] = None,
        audio: Optional[bool] = None,
        **_ignored: Any,
    ) -> str:
        """生成视频：写入 save_path 并返回远端标识（视频 URL/任务下载地址）。"""
        duration = self._clamp_duration(duration)
        size = video_size(video_ratio, resolution)
        job = self._create_job(
            prompt=prompt,
            image_path=image_path,
            size=size,
            duration=duration,
            negative_prompt=negative_prompt,
            seed=seed,
        )
        job_id = self._extract_job_id(job)
        logger.info(
            "Custom video job created: protocol=%s model=%s job=%s size=%s duration=%ss",
            self._protocol,
            self._model,
            job_id,
            size,
            duration,
        )
        try:
            final = self._wait_for_job(job_id)
        except Exception:
            self._cancel_job(job_id)  # 尽力取消，避免遗留任务
            raise
        remote_url = self._extract_remote_url(job_id, final) or self._content_endpoint(job_id)
        self._download_content(job_id, save_path, remote_url)
        return remote_url

    # ── 参数与创建 ──

    def _clamp_duration(self, duration: int) -> int:
        contract = (self._meta.get("capabilities") or {}).get("duration") or {}
        try:
            minimum = int(contract.get("min", 1))
            maximum = int(contract.get("max", duration))
        except (TypeError, ValueError):
            minimum, maximum = 1, duration
        if maximum < minimum:
            maximum = minimum
        return min(max(int(duration), minimum), maximum)

    def _create_variants(self, has_image: bool) -> List[str]:
        if self._protocol == "sglang":
            # sglang：t2v 文档形态为 JSON；含文件（i2v）走 multipart
            return ["multipart", "json"] if has_image else ["json", "multipart"]
        # openai / vllm-omni：均以 multipart 为主形态（Sora 形态），失败回退 JSON
        return ["multipart", "json"]

    def _create_job(
        self,
        prompt: str,
        image_path: Optional[str],
        size: str,
        duration: int,
        negative_prompt: Optional[str],
        seed: Optional[int],
    ) -> Dict[str, Any]:
        last_error = ""
        last_exc: Optional[Exception] = None
        for encoding in self._create_variants(bool(image_path)):
            try:
                response = self._post_create(
                    encoding, prompt, image_path, size, duration, negative_prompt, seed
                )
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise RuntimeError(
                            f"自定义视频接口返回非 JSON 内容: {response.text[:200]}"
                        ) from exc
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(
                    "Custom video create via %s failed (%s), trying fallback",
                    encoding,
                    response.status_code,
                )
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = str(exc)
                last_exc = exc
        raise RuntimeError(
            f"自定义视频模型 {self._model} 创建任务失败（目标 {self._base_url}/videos）: {last_error}"
            f"{connection_hint(last_exc or Exception(last_error), self._base_url)}"
        )

    def _post_create(
        self,
        encoding: str,
        prompt: str,
        image_path: Optional[str],
        size: str,
        duration: int,
        negative_prompt: Optional[str],
        seed: Optional[int],
    ) -> httpx.Response:
        base_fields: Dict[str, Any] = {
            "prompt": prompt,
            "model": self._model,
            "size": size,
        }
        if negative_prompt:
            base_fields["negative_prompt"] = negative_prompt
        if seed is not None:
            base_fields["seed"] = str(seed)

        if encoding == "json":
            fields = dict(base_fields)
            fields["seconds"] = str(duration)
            return self._client.post("/videos", json=fields)

        data = dict(base_fields)
        data["seconds"] = str(duration)
        if image_path:
            handle = open(image_path, "rb")
            try:
                files = {
                    "input_reference": (os.path.basename(image_path), handle, guess_mime_type(image_path))
                }
                return self._client.post("/videos", data=data, files=files)
            finally:
                handle.close()
        # 无文件时也强制 multipart：以文本字段（filename=None）走 files 通道
        return self._client.post("/videos", files={key: (None, str(value)) for key, value in data.items()})

    @staticmethod
    def _extract_job_id(job: Dict[str, Any]) -> str:
        for key in ("id", "video_id", "task_id", "job_id"):
            value = job.get(key)
            if value:
                return str(value)
        raise RuntimeError(f"自定义视频接口未返回任务 id: {str(job)[:200]}")

    # ── 轮询 ──

    def _get_job(self, job_id: str) -> Dict[str, Any]:
        response = self._client.get(f"/videos/{job_id}")
        if response.status_code < 400:
            try:
                data = response.json()
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
        # 回退：列表查询（部分实现只提供 GET /v1/videos）
        list_response = self._client.get("/videos")
        list_response.raise_for_status()
        try:
            payload = list_response.json()
        except ValueError as exc:
            raise RuntimeError(f"自定义视频列表接口返回非 JSON: {list_response.text[:200]}") from exc
        items = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("id")) == job_id:
                    return item
        raise RuntimeError(f"自定义视频任务 {job_id} 查询不到状态")

    def _wait_for_job(self, job_id: str) -> Dict[str, Any]:
        deadline = time.time() + self._poll_timeout
        while time.time() < deadline:
            data = self._get_job(job_id)
            status = str(data.get("status") or "").strip().lower()
            if status in VIDEO_JOB_SUCCESS_STATES:
                return data
            if status in VIDEO_JOB_FAILURE_STATES:
                reason = (
                    data.get("error")
                    or data.get("detail")
                    or data.get("message")
                    or status
                )
                raise RuntimeError(f"自定义视频任务 {job_id} 失败: {reason}")
            time.sleep(self._poll_interval)
        raise RuntimeError(
            f"自定义视频任务 {job_id} 轮询超时（上限 {int(self._poll_timeout)}s）"
        )

    @staticmethod
    def _extract_remote_url(job_id: str, job_data: Dict[str, Any]) -> str:
        for key in ("url", "content_url", "video_url", "output_url"):
            value = job_data.get(key)
            if value:
                return str(value)
        output = job_data.get("output")
        if isinstance(output, dict):
            for key in ("url", "video_url", "content_url"):
                if output.get(key):
                    return str(output[key])
        return ""

    def _content_endpoint(self, job_id: str) -> str:
        base = str(self._client.base_url)
        if not base.endswith("/"):
            base += "/"
        return urljoin(base, f"videos/{job_id}/content")

    def _cancel_job(self, job_id: str) -> None:
        """尽力取消任务（vllm-omni 支持 DELETE；不支持时忽略）。"""
        try:
            self._client.delete(f"/videos/{job_id}")
        except Exception:  # pragma: no cover - 取消失败不影响主流程
            logger.debug("Best-effort cancel failed for job %s", job_id, exc_info=True)

    # ── 下载 ─

    def _download_content(self, job_id: str, save_path: str, remote_url: str) -> None:
        content: Optional[bytes] = None
        try:
            response = self._client.get(f"videos/{job_id}/content")
            if response.status_code < 400:
                content = response.content
        except httpx.HTTPError:
            content = None
        if content is None and remote_url.startswith("http"):
            try:
                response = self._client.get(remote_url)
                response.raise_for_status()
                content = response.content
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f"自定义视频模型 {self._model} 下载结果失败（目标 {self._base_url}）: {remote_url} -> {exc}"
                    f"{connection_hint(exc, self._base_url)}"
                ) from exc
        if content is None:
            raise RuntimeError(f"自定义视频模型 {self._model} 无法下载任务 {job_id} 的内容")

        target_dir = os.path.dirname(os.path.abspath(save_path))
        os.makedirs(target_dir, exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(content)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover
            pass