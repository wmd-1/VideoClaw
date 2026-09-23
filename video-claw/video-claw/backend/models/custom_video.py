"""自定义视频客户端：OpenAI Videos 风格异步任务（openai / vllm-omni / sglang）。

三段式时序：POST 创建任务（multipart 为主形态，多图字段按候选回退）→ 轮询
`GET /v1/videos/{id}`（失败回退列表）→ `GET /v1/videos/{id}/content` 下载为 mp4。
支持文生视频 / 首帧生视频（input_reference）/ 首尾帧（last_frame 等候选字段）/ 参考图
（多图字段候选）；与内置 VideoClient 约定一致：内容写入 `save_path` 并返回远端标识；
轮询有超时上限；失败时尽力取消任务。
"""

import json
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

from config import Config
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
        poll_timeout: Optional[float] = None,
        timeout: Optional[float] = None,
    ):
        self._meta = meta
        self._model = str(meta.get("request_model") or meta.get("id") or "")
        self._protocol = str(meta.get("protocol") or "")
        self._base_url = str(meta.get("base_url") or "")
        self._poll_interval = poll_interval
        # 生成与轮询超时：默认取 Config.TIMEOUT_VIDEO（.env VC_TIMEOUT_VIDEO，缺省 3 小时）
        resolved_timeout = float(timeout if timeout is not None else Config.TIMEOUT_VIDEO)
        self._poll_timeout = float(poll_timeout if poll_timeout is not None else Config.TIMEOUT_VIDEO)
        self._client = make_http_client(**build_custom_client_kwargs(meta, timeout=resolved_timeout))

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
        # vllm-omni：优先 /videos/sync（一次请求直出视频字节，官方 recipes 主形态）；
        # 端点不可用（404/405/非视频响应）时回退异步三段式。
        if self._protocol == "vllm-omni":
            sync_error = self._try_sync_generate(
                prompt=prompt,
                image_path=image_path,
                last_image_path=last_image_path,
                reference_image_paths=reference_image_paths,
                size=size,
                duration=duration,
                negative_prompt=negative_prompt,
                seed=seed,
                save_path=save_path,
            )
            if sync_error is None:
                return f"{self._base_url}/videos/sync"
            logger.info("vllm-omni sync unavailable (%s); falling back to async job flow", sync_error)
        job = self._create_job(
            prompt=prompt,
            image_path=image_path,
            last_image_path=last_image_path,
            reference_image_paths=reference_image_paths,
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

    def _task_for_input(
        self,
        image_path: Optional[str],
        last_image_path: Optional[str],
        refs: List[str],
    ) -> str:
        """按输入推断 MiniMax-H3 的任务类型（t2va / fl2va / ref2va）。"""
        if refs:
            return "ref2va"
        if image_path or last_image_path:
            return "fl2va"  # 首帧 / 尾帧 / 首尾帧
        return "t2va"

    def _extra_params(self, task: str, duration: int, frame_indices: Optional[List[int]] = None) -> str:
        """vllm-omni 的 extra_params 表单字段（JSON 字符串）。"""
        payload: Dict[str, Any] = {"task": task, "duration": float(duration)}
        if frame_indices:
            payload["frame_indices"] = frame_indices
        return json.dumps(payload)

    def _file_uri(self, path: str) -> str:
        """将本地路径转为 file:// URI（sglang conditions 的服务端可见引用形态）。"""
        if path.startswith("file://"):
            return path
        return "file://" + os.path.abspath(path)

    def _sglang_conditions_variant(
        self,
        image_path: Optional[str],
        last_image_path: Optional[str],
        refs: List[str],
        task: str,
    ) -> Tuple[str, List[Tuple[str, str]], Dict[str, Any]]:
        """sglang 官方 cookbook 形态（JSON + conditions，file:// 引用服务端可见路径）。"""
        conditions: List[Dict[str, Any]] = []
        if refs:
            conditions = [
                {"type": "image", "uri": self._file_uri(path), "role": "reference"} for path in refs
            ]
        elif image_path and last_image_path:
            conditions = [
                {"type": "image", "uri": self._file_uri(image_path), "role": "keyframe", "frame_index": 0},
                {"type": "image", "uri": self._file_uri(last_image_path), "role": "keyframe", "frame_index": -1},
            ]
        elif last_image_path:
            conditions = [
                {"type": "image", "uri": self._file_uri(last_image_path), "role": "keyframe", "frame_index": -1}
            ]
        else:
            conditions = [
                {"type": "image", "uri": self._file_uri(image_path or ""), "role": "keyframe", "frame_index": 0}
            ]
        return ("json", [], {"task": task, "conditions": conditions})

    def _image_file_specs(
        self,
        image_path: Optional[str],
        last_image_path: Optional[str],
        reference_image_paths: Optional[List[str]],
    ) -> List[List[Tuple[str, str]]]:
        """构造文件字段候选方案：每种方案为 [(字段名, 图片路径), ...] 列表。

        按协议给出主形态优先、失败回退的候选：
        - vllm-omni（MiniMax-H3 recipes）：单图 `input_reference`，多图 `input_references` 重复
        - sglang（MiniMax-H3 cookbook）：顺序双帧用同名 `input_reference` 重复
        - openai 兼容：沿用常见字段命名回退
        """
        refs = [path for path in (reference_image_paths or []) if path]
        if refs:
            specs: List[List[Tuple[str, str]]] = []
            if self._protocol == "vllm-omni":
                if len(refs) == 1:
                    specs.append([("input_reference", refs[0])])
                else:
                    specs.append([("input_references", path) for path in refs])
            specs.append([("input_reference", path) for path in refs])
            specs.append([("reference_images", path) for path in refs])
            specs.append([("image[]", path) for path in refs])
            return specs
        if image_path and last_image_path:
            specs = []
            if self._protocol == "vllm-omni":
                # H3 首尾帧：同名 input_references 重复 + extra_params.frame_indices=[0,-1]
                specs.append([("input_references", image_path), ("input_references", last_image_path)])
            if self._protocol == "sglang":
                # H3 顺序双帧：同名 input_reference 按序重复
                specs.append([("input_reference", image_path), ("input_reference", last_image_path)])
            specs.append([("input_reference", image_path), ("last_frame", last_image_path)])
            specs.append([("input_reference", image_path), ("last_image", last_image_path)])
            specs.append([("input_reference", image_path), ("tail_image", last_image_path)])
            specs.append([("input_reference", image_path), ("input_reference", last_image_path)])
            return specs
        if last_image_path and not image_path:
            # 仅尾帧（frame_index=-1）：单 input_reference + extra_params.frame_indices=[-1]
            return [[("input_reference", last_image_path)]]
        if image_path:
            return [[("input_reference", image_path)]]
        return [[]]

    def _create_variants(
        self,
        image_specs: List[List[Tuple[str, str]]],
        task: str,
        duration: int,
        tail_only: bool = False,
    ) -> List[Tuple[str, List[Tuple[str, str]], Dict[str, Any]]]:
        """返回 (编码, 文件字段方案, 附加表单字段) 尝试序列。

        协议差异：
        - vllm-omni：extra_params（task/duration/frame_indices）为首选形态，失败回退旧形态（保障兼容）
        - sglang：task 为 MiniMax-H3 必填（t2va/fl2va/ref2va）
        - openai：保持原始形态（无附加字段）
        - 含文件仅 multipart（JSON 无法携带文件，避免退化为“无图生成”）
        """
        variants: List[Tuple[str, List[Tuple[str, str]], Dict[str, Any]]] = []
        for field_spec in image_specs:
            if field_spec:
                if self._protocol == "vllm-omni":
                    if task != "fl2va":
                        frame_indices = None
                    elif tail_only:
                        frame_indices = [-1]
                    elif len(field_spec) == 2 and all(name == "input_references" for name, _ in field_spec):
                        frame_indices = [0, -1]
                    else:
                        frame_indices = None
                    variants.append(("multipart", field_spec, {"extra_params": self._extra_params(task, duration, frame_indices)}))
                    variants.append(("multipart", field_spec, {}))
                elif self._protocol == "sglang":
                    variants.append(("multipart", field_spec, {"task": task}))
                else:
                    variants.append(("multipart", field_spec, {}))
            elif self._protocol == "sglang":
                variants.extend([("json", [], {"task": task, "conditions": []}), ("multipart", [], {"task": task})])
            elif self._protocol == "vllm-omni":
                variants.extend([
                    ("multipart", [], {"extra_params": self._extra_params(task, duration)}),
                    ("multipart", [], {}),
                    ("json", [], {}),
                ])
            else:
                variants.extend([("multipart", [], {}), ("json", [], {})])
        # 去重（多个候选方案可能与纯文本序列重复）
        seen = set()
        unique: List[Tuple[str, List[Tuple[str, str]], Dict[str, str]]] = []
        for item in variants:
            marker = (item[0], tuple(item[1]), json.dumps(item[2], sort_keys=True, default=str))
            if marker not in seen:
                seen.add(marker)
                unique.append(item)
        return unique

    def _try_sync_generate(
        self,
        prompt: str,
        image_path: Optional[str],
        last_image_path: Optional[str],
        reference_image_paths: Optional[List[str]],
        size: str,
        duration: int,
        negative_prompt: Optional[str],
        seed: Optional[int],
        save_path: str,
    ) -> Optional[str]:
        """尝试 vllm-omni 的 /videos/sync 同步端点：成功落盘返回 None，否则返回错误信息（供回退异步）。"""
        refs = [path for path in (reference_image_paths or []) if path]
        task = self._task_for_input(image_path, last_image_path, refs)
        tail_only = bool(last_image_path) and not image_path
        last_error = ""
        for field_spec in self._image_file_specs(image_path, last_image_path, reference_image_paths):
            if task != "fl2va":
                frame_indices = None
            elif tail_only:
                frame_indices = [-1]
            elif len(field_spec) == 2 and all(name == "input_references" for name, _ in field_spec):
                frame_indices = [0, -1]
            else:
                frame_indices = None
            form_extra = {"extra_params": self._extra_params(task, duration, frame_indices)}
            try:
                response = self._post_sync(
                    prompt, field_spec, size, duration, negative_prompt, seed, form_extra
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code in (404, 405):
                    return last_error  # 端点不存在：快速回退异步
                continue
            content_type = response.headers.get("content-type", "")
            if "video" in content_type or "octet-stream" in content_type:
                with open(save_path, "wb") as handle:
                    handle.write(response.content)
                return None
            last_error = f"unexpected content-type: {content_type or 'empty'}"
        return last_error or "sync endpoint unavailable"

    def _post_sync(
        self,
        prompt: str,
        field_spec: List[Tuple[str, str]],
        size: str,
        duration: int,
        negative_prompt: Optional[str],
        seed: Optional[int],
        form_extra: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        base_fields: Dict[str, Any] = {"prompt": prompt, "model": self._model, "size": size}
        if negative_prompt:
            base_fields["negative_prompt"] = negative_prompt
        if seed is not None:
            base_fields["seed"] = str(seed)
        data = dict(base_fields)
        data["seconds"] = str(duration)
        if form_extra:
            data.update({key: value for key, value in form_extra.items() if isinstance(value, (str, int, float))})
        if field_spec:
            opened: List[Any] = []
            try:
                files = []
                for field_name, path in field_spec:
                    handle = open(path, "rb")
                    opened.append(handle)
                    files.append((field_name, (os.path.basename(path), handle, guess_mime_type(path))))
                return self._client.post("/videos/sync", data=data, files=files)
            finally:
                for handle in opened:
                    handle.close()
        return self._client.post("/videos/sync", files={key: (None, str(value)) for key, value in data.items()})

    def _create_job(
        self,
        prompt: str,
        image_path: Optional[str],
        last_image_path: Optional[str],
        reference_image_paths: Optional[List[str]],
        size: str,
        duration: int,
        negative_prompt: Optional[str],
        seed: Optional[int],
    ) -> Dict[str, Any]:
        refs = [path for path in (reference_image_paths or []) if path]
        task = self._task_for_input(image_path, last_image_path, refs)
        tail_only = bool(last_image_path) and not image_path
        variants = self._create_variants(
            self._image_file_specs(image_path, last_image_path, reference_image_paths),
            task,
            duration,
            tail_only=tail_only,
        )
        if self._protocol == "sglang" and (refs or image_path or last_image_path):
            # H3 cookbook 官方形态（JSON + conditions，file:// 引用服务端可见路径）：作为最后回退
            variants.append(self._sglang_conditions_variant(image_path, last_image_path, refs, task))
        last_error = ""
        last_exc: Optional[Exception] = None
        for encoding, field_spec, form_extra in variants:
            try:
                response = self._post_create(
                    encoding, prompt, field_spec, size, duration, negative_prompt, seed, form_extra
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
                    "Custom video create via %s%s failed (%s), trying fallback",
                    encoding,
                    "+params" if form_extra else "",
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
        field_spec: List[Tuple[str, str]],
        size: str,
        duration: int,
        negative_prompt: Optional[str],
        seed: Optional[int],
        form_extra: Optional[Dict[str, str]] = None,
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
            if form_extra:
                fields.update(form_extra)
            return self._client.post("/videos", json=fields)

        data = dict(base_fields)
        data["seconds"] = str(duration)
        if form_extra:
            # 仅标量字段可由 multipart 表单承载（如 task/extra_params；conditions 等结构化字段仅 JSON）
            data.update({key: value for key, value in form_extra.items() if isinstance(value, (str, int, float))})
        if field_spec:
            opened: List[Any] = []
            try:
                files = []
                for field_name, path in field_spec:
                    handle = open(path, "rb")
                    opened.append(handle)
                    files.append((field_name, (os.path.basename(path), handle, guess_mime_type(path))))
                return self._client.post("/videos", data=data, files=files)
            finally:
                for handle in opened:
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