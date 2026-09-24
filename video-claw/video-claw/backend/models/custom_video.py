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
    video_dimensions,
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
        adjustments: Optional[List[str]] = None,
    ):
        self._meta = meta
        self._model = str(meta.get("request_model") or meta.get("id") or "")
        self._protocol = str(meta.get("protocol") or "")
        self._base_url = str(meta.get("base_url") or "")
        self._poll_interval = poll_interval
        # 生成与轮询超时：默认取 Config.TIMEOUT_VIDEO（.env VC_TIMEOUT_VIDEO，缺省 3 小时）
        resolved_timeout = float(timeout if timeout is not None else Config.TIMEOUT_VIDEO)
        self._poll_timeout = float(poll_timeout if poll_timeout is not None else Config.TIMEOUT_VIDEO)
        # 参数夹取/忽略记录：外部传入列表时同步写入（供 API 响应透出），否则仅内部收集
        self._adjustments = adjustments if adjustments is not None else []
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
        audio_reference_url: Optional[str] = None,
        audio_path: Optional[str] = None,
        negative_prompt: Optional[str] = None,
        prompt_extend: Optional[bool] = None,
        watermark: Optional[bool] = None,
        seed: Optional[int] = None,
        mode: str = "pro",
        cfg_scale: float = 0.5,
        generate_audio: Optional[bool] = None,
        audio: Optional[bool] = None,
        fps: Optional[int] = None,
        short_edge: Optional[int] = None,
        **_ignored: Any,
    ) -> str:
        """生成视频：写入 save_path 并返回远端标识（视频 URL/任务下载地址）。

        统一参数：duration（秒）、video_ratio、resolution、fps（可选）、short_edge（可选）。
        fps/short_edge 为高级参数：仅在显式提供或模型能力声明触发时注入协议字段；
        缺省时请求与既有实现完全一致（向后兼容）。
        """
        duration = self._clamp_duration(duration)
        size = video_size(video_ratio, resolution)
        # 音频参考 → vllm-omni 的 audio_reference 表单字段（URL 形态，D1）
        audio_reference = self._audio_reference_field(audio_reference_url)
        # 统一参数 → 协议字段（单点映射构造器，防多链路漂移）
        protocol_fields, json_fields, drop_size = self._map_protocol_params(
            duration=duration,
            video_ratio=video_ratio,
            resolution=resolution,
            fps=fps,
            short_edge=short_edge,
        )
        # vllm-omni：优先 /videos/sync（一次请求直出视频字节，官方 recipes 主形态）；
        # 端点不可用（404/405/非视频响应）时回退异步三段式。
        if self._protocol == "vllm-omni":
            sync_error = self._try_sync_generate(
                prompt=prompt,
                image_path=image_path,
                last_image_path=last_image_path,
                reference_image_paths=reference_image_paths,
                reference_video_paths=reference_video_paths,
                audio_reference=audio_reference,
                size=size,
                duration=duration,
                negative_prompt=negative_prompt,
                seed=seed,
                save_path=save_path,
                protocol_fields=protocol_fields,
                drop_size=drop_size,
            )
            if sync_error is None:
                return f"{self._base_url}/videos/sync"
            logger.info("vllm-omni sync unavailable (%s); falling back to async job flow", sync_error)
        job = self._create_job(
            prompt=prompt,
            image_path=image_path,
            last_image_path=last_image_path,
            reference_image_paths=reference_image_paths,
            reference_video_paths=reference_video_paths,
            audio_reference=audio_reference,
            size=size,
            duration=duration,
            negative_prompt=negative_prompt,
            seed=seed,
            protocol_fields=protocol_fields,
            json_fields=json_fields,
            drop_size=drop_size,
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

    def _capabilities(self) -> Dict[str, Any]:
        caps = self._meta.get("capabilities")
        return caps if isinstance(caps, dict) else {}

    def _record_adjustment(self, message: str) -> None:
        """记录参数夹取/忽略事实：写入日志与 adjustments 列表（可用时随响应透出）。"""
        logger.info("Custom video param adjusted: model=%s %s", self._model, message)
        if message not in self._adjustments:
            self._adjustments.append(message)

    def _clamp_duration(self, duration: int) -> int:
        """按模型 capabilities.duration{min,max} 夹取时长（统一入口，单点实现）。"""
        contract = self._capabilities().get("duration") or {}
        try:
            minimum = int(contract.get("min", 1))
            maximum = int(contract.get("max", duration))
        except (TypeError, ValueError):
            minimum, maximum = 1, duration
        if maximum < minimum:
            maximum = minimum
        clamped = min(max(int(duration), minimum), maximum)
        if clamped != int(duration):
            self._record_adjustment(
                f"duration {duration}s -> {clamped}s（模型能力范围 {minimum}-{maximum}s）"
            )
        return clamped

    def _resolve_fps(self, fps: Optional[int]) -> Optional[int]:
        """解析 fps 注入值：模型声明 fps 支持列表时夹取到最近支持值；未声明则不注入。"""
        if fps is None:
            return None
        try:
            requested = int(fps)
        except (TypeError, ValueError):
            return None
        supported_raw = self._capabilities().get("fps")
        if not isinstance(supported_raw, (list, tuple)) or not supported_raw:
            # 模型未声明 fps 支持：沿用服务端默认，不注入字段（记录忽略事实）
            self._record_adjustment(f"fps {requested} 已忽略（模型未声明 fps 支持）")
            return None
        supported: List[int] = []
        for value in supported_raw:
            try:
                supported.append(int(value))
            except (TypeError, ValueError):
                continue
        if not supported:
            self._record_adjustment(f"fps {requested} 已忽略（模型 fps 声明无效）")
            return None
        if requested in supported:
            return requested
        nearest = min(supported, key=lambda value: (abs(value - requested), value))
        self._record_adjustment(f"fps {requested} -> {nearest}（模型支持 {supported}）")
        return nearest

    def _short_edge_value(self, short_edge: Optional[int]) -> Optional[int]:
        """解析 short_edge 注入值：优先模型声明值，用户显式值仅在未声明时生效；均无则 None。"""
        declared_raw = self._capabilities().get("short_edge")
        try:
            declared = int(declared_raw) if declared_raw else None
        except (TypeError, ValueError):
            declared = None
        try:
            requested = int(short_edge) if short_edge is not None else None
        except (TypeError, ValueError):
            requested = None
        if declared is not None:
            if requested is not None and requested != declared:
                self._record_adjustment(
                    f"short_edge {requested} -> {declared}（模型声明值）"
                )
            return declared
        return requested

    def _map_protocol_params(
        self,
        duration: int,
        video_ratio: str,
        resolution: Optional[str],
        fps: Optional[int],
        short_edge: Optional[int],
    ) -> Tuple[Dict[str, str], Dict[str, Any], bool]:
        """统一参数 → 协议字段（单点映射构造器，D1 映射矩阵）。

        返回 (protocol_fields, json_fields, drop_size)：
        - protocol_fields：标量字段（multipart / JSON 编码通用）；
        - json_fields：结构化字段（JSON 编码原样下发；multipart 序列化为 JSON 字符串）；
        - drop_size：True 时不下发 size，改用协议标准宽高字段避免冲突（D2）。
        兼容策略（D5）：未显式提供 fps/short_edge 且模型未声明 short_edge 时返回空，
        请求与既有实现完全一致。时长由同一 clamped 值派生（extra_params.duration /
        seconds / target.duration_seconds 一致，D3）。
        """
        protocol_fields: Dict[str, str] = {}
        json_fields: Dict[str, Any] = {}
        drop_size = False
        if self._protocol == "vllm-omni":
            fps_value = self._resolve_fps(fps)
            short_edge_value = self._short_edge_value(short_edge)
            if short_edge_value is not None:
                # 模型声明 short_edge（或用户显式指定）→ short_edge + aspect_ratio
                protocol_fields["short_edge"] = str(short_edge_value)
                protocol_fields["aspect_ratio"] = video_ratio or "16:9"
                drop_size = True
            elif fps_value is not None or short_edge is not None:
                # 用户显式设置高级参数 → 由 ratio+resolution 推导 width/height
                width, height = video_dimensions(video_ratio, resolution)
                protocol_fields["width"] = str(width)
                protocol_fields["height"] = str(height)
                drop_size = True
            if fps_value is not None:
                protocol_fields["fps"] = str(fps_value)
        elif self._protocol == "sglang":
            target = self._sglang_target(duration, video_ratio, short_edge)
            if target is not None:
                json_fields["target"] = target
        # openai 兼容：沿用 size/seconds，不注入新字段
        return protocol_fields, json_fields, drop_size

    def _sglang_target(
        self,
        duration: int,
        video_ratio: str,
        short_edge: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """sglang 官方 cookbook 的 target 结构：short_edge/aspect_ratio/duration_seconds。

        仅在 short_edge 可用（模型声明或用户显式指定）时注入；duration_seconds 与
        顶层 seconds 由同一 clamped duration 派生，保证一致（D3）。
        """
        short_edge_value = self._short_edge_value(short_edge)
        if short_edge_value is None:
            return None
        return {
            "short_edge": int(short_edge_value),
            "aspect_ratio": video_ratio or "16:9",
            "duration_seconds": int(duration),
        }

    def _audio_reference_field(self, audio_reference_url: Optional[str]) -> Optional[str]:
        """音频参考 → vllm-omni 的 audio_reference 表单字段（JSON 字符串，含 audio_url）。

        audio_url 接受 HTTP(S) URL 或 data: URL（MiniMax-H3 recipes）；
        非 vllm-omni 协议不支持媒体参考字段：显式记录忽略事实，绝不静默丢弃（D4）。
        """
        if not audio_reference_url:
            return None
        if self._protocol != "vllm-omni":
            self._record_adjustment("audio_reference 已忽略（协议不支持音频参考）")
            return None
        return json.dumps({"audio_url": audio_reference_url})

    def _media_reference_paths(
        self,
        reference_image_paths: Optional[List[str]],
        reference_video_paths: Optional[List[str]],
    ) -> List[str]:
        """媒体参考列表：图片参考在前、视频参考在后，按传入顺序合并（D2）。"""
        images = [path for path in (reference_image_paths or []) if path]
        videos = [path for path in (reference_video_paths or []) if path]
        return images + videos

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
        reference_video_paths: Optional[List[str]] = None,
    ) -> List[List[Tuple[str, str]]]:
        """构造文件字段候选方案：每种方案为 [(字段名, 媒体路径), ...] 列表。

        按协议给出主形态优先、失败回退的候选：
        - vllm-omni（MiniMax-H3 recipes）：单文件 `input_reference`，多文件 `input_references` 重复；
          视频参考（mp4/quicktime/webm 等）与图片参考按顺序合并到同一序列（D2）
        - sglang（MiniMax-H3 cookbook）：顺序双帧用同名 `input_reference` 重复
        - openai 兼容：沿用常见字段命名回退
        """
        refs = self._media_reference_paths(reference_image_paths, reference_video_paths)
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
        reference_video_paths: Optional[List[str]] = None,
        audio_reference: Optional[str] = None,
        protocol_fields: Optional[Dict[str, str]] = None,
        drop_size: bool = False,
    ) -> Optional[str]:
        """尝试 vllm-omni 的 /videos/sync 同步端点：成功落盘返回 None，否则返回错误信息（供回退异步）。"""
        refs = self._media_reference_paths(reference_image_paths, reference_video_paths)
        task = self._task_for_input(image_path, last_image_path, refs)
        tail_only = bool(last_image_path) and not image_path
        last_error = ""
        for field_spec in self._image_file_specs(image_path, last_image_path, reference_image_paths, reference_video_paths):
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
                    prompt, field_spec, size, duration, negative_prompt, seed, form_extra,
                    audio_reference=audio_reference,
                    protocol_fields=protocol_fields,
                    drop_size=drop_size,
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
        audio_reference: Optional[str] = None,
        protocol_fields: Optional[Dict[str, str]] = None,
        drop_size: bool = False,
    ) -> httpx.Response:
        base_fields: Dict[str, Any] = {"prompt": prompt, "model": self._model}
        if not drop_size:
            base_fields["size"] = size
        if protocol_fields:
            base_fields.update(protocol_fields)
        if negative_prompt:
            base_fields["negative_prompt"] = negative_prompt
        if seed is not None:
            base_fields["seed"] = str(seed)
        if audio_reference:
            base_fields["audio_reference"] = audio_reference
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
        reference_video_paths: Optional[List[str]] = None,
        audio_reference: Optional[str] = None,
        protocol_fields: Optional[Dict[str, str]] = None,
        json_fields: Optional[Dict[str, Any]] = None,
        drop_size: bool = False,
    ) -> Dict[str, Any]:
        refs = self._media_reference_paths(reference_image_paths, reference_video_paths)
        task = self._task_for_input(image_path, last_image_path, refs)
        tail_only = bool(last_image_path) and not image_path
        variants = self._create_variants(
            self._image_file_specs(image_path, last_image_path, reference_image_paths, reference_video_paths),
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
                    encoding, prompt, field_spec, size, duration, negative_prompt, seed, form_extra,
                    audio_reference=audio_reference,
                    protocol_fields=protocol_fields,
                    json_fields=json_fields,
                    drop_size=drop_size,
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
        audio_reference: Optional[str] = None,
        protocol_fields: Optional[Dict[str, str]] = None,
        json_fields: Optional[Dict[str, Any]] = None,
        drop_size: bool = False,
    ) -> httpx.Response:
        base_fields: Dict[str, Any] = {"prompt": prompt, "model": self._model}
        if not drop_size:
            base_fields["size"] = size
        if protocol_fields:
            base_fields.update(protocol_fields)
        if negative_prompt:
            base_fields["negative_prompt"] = negative_prompt
        if seed is not None:
            base_fields["seed"] = str(seed)
        if audio_reference:
            base_fields["audio_reference"] = audio_reference

        if encoding == "json":
            fields = dict(base_fields)
            fields["seconds"] = str(duration)
            for key, value in (json_fields or {}).items():
                fields[key] = value
            if form_extra:
                fields.update(form_extra)
            return self._client.post("/videos", json=fields)

        data = dict(base_fields)
        data["seconds"] = str(duration)
        for key, value in (json_fields or {}).items():
            # multipart 表单仅承载标量：结构化字段（如 sglang target）序列化为 JSON 字符串
            data[key] = value if isinstance(value, str) else json.dumps(value)
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