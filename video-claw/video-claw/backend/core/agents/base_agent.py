# -*- coding: utf-8 -*-
"""
智能体基类 - 所有阶段智能体的抽象接口
"""

import asyncio
import glob
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.language import language_from_meta, resolve_language

logger = logging.getLogger(__name__)


def heartbeat_ascent(current: float, cap: float, ratio: float = 0.12) -> float:
    """心跳的单次渐近推进值：向 cap 逼近，至少前进 1；各阶段共用同一份算法。"""
    return min(cap, current + max(1.0, (cap - current) * ratio))


SESSION_PARAM_KEYS = [
    "idea", "user_textbox_input", "style", "video_ratio", "video_resolution",
    "video_duration", "video_fps", "video_short_edge", "audio_reference_url",
    "llm_model", "vlm_model",
    "image_t2i_model", "image_it2i_model", "video_model",
    "video_first_frame_model", "video_start_end_model", "video_reference_model",
    "video_generation_mode",
    "language", "language_source",
    "video_style", "expand_idea", "enable_concurrency", "web_search", "episodes"
]


class AgentInterface(ABC):
    """所有智能体必须实现的接口"""

    def __init__(self, name: str = ""):
        self.name = name
        self.cancellation_check: Optional[Callable] = None
        self.progress_callback: Optional[Callable] = None
        # 由编排器设置：本次执行的受信工作根（新输出只写这里）与 DB 视图中的历史版本。
        # 未设置时（独立使用 Agent）沿用旧的结果目录。
        self.execution_root: Optional[str] = None
        self.version_history: Dict[str, List[str]] = {}

    def _merge_session_params(self, input_data: Any) -> Dict:
        """从编排器注入的 session 快照补齐缺失参数。"""
        if not isinstance(input_data, dict):
            return {}

        session_meta = self._session_meta(input_data)
        merged_data = input_data.copy()
        for key in SESSION_PARAM_KEYS:
            if key not in merged_data or not merged_data[key]:
                if key in session_meta and session_meta[key] is not None:
                    merged_data[key] = session_meta[key]
        return merged_data

    def _session_meta(self, input_data: Dict) -> Dict:
        meta = input_data.get("_session_meta") if isinstance(input_data, dict) else {}
        return meta if isinstance(meta, dict) else {}

    def _session_language(self, input_data: Dict, *, body: str = "", title: str = "",
                          instruction: str = "") -> str:
        """会话语言：已有字段优先；缺失时用同一解析器但不写库（回填由编排层负责）。"""
        meta = self._session_meta(input_data)
        stored = language_from_meta(meta)
        if stored:
            return stored
        resolved = resolve_language(
            instruction=instruction or meta.get("user_textbox_input") or "",
            body=body, title=title,
        )
        return resolved["language"]

    def _session_artifacts(self, input_data: Dict) -> Dict:
        artifacts = input_data.get("_session_artifacts") if isinstance(input_data, dict) else {}
        return artifacts if isinstance(artifacts, dict) else {}

    def _session_artifact(self, input_data: Dict, stage: str) -> Dict:
        artifact = self._session_artifacts(input_data).get(stage, {})
        return artifact if isinstance(artifact, dict) else {}

    def set_cancellation_check(self, fn: Callable):
        self.cancellation_check = fn

    def set_progress_callback(self, fn: Callable):
        self.progress_callback = fn

    # -------- 执行输出与版本历史 --------

    def _output_dir(self, legacy_dir: str, *parts: str) -> str:
        """新产物目录：编排器提供执行根时写入执行根下的子目录，否则用旧结果目录。"""
        if self.execution_root:
            return os.path.join(self.execution_root, *parts)
        return legacy_dir

    def _history_versions(self, container: str, item_id: str) -> List[str]:
        return list(self.version_history.get(f"{container}:{item_id}") or [])

    @staticmethod
    def _local_files(directory: str, patterns: List[str], exclude_suffix: str = "") -> List[str]:
        files: List[str] = []
        for pattern in patterns:
            files.extend(glob.glob(os.path.join(directory, pattern)))
        files = [f for f in set(files) if not (exclude_suffix and f.endswith(exclude_suffix))]
        return sorted(files, key=os.path.getmtime)

    def _merge_history(self, container: str, item_id: str, local: List[str]) -> List[str]:
        """历史版本来自 DB 视图（canonical），本地 glob 只覆盖本次执行的新输出。"""
        if not self.execution_root:
            return local
        merged = self._history_versions(container, item_id)
        for path in local:
            if path not in merged:
                merged.append(path)
        return merged

    def _report_progress(self, phase: str, step_desc: str, percent: float, data: dict = None):
        if self.progress_callback:
            self.progress_callback(phase, step_desc, percent, data)

    async def _await_with_heartbeat(
        self,
        awaitable: Awaitable,
        *,
        start: float,
        cap: float,
        message: str,
        log: Callable[[float, str], None],
        interval: float = 8.0,
    ):
        """等待一个可能长耗时的任务，期间按固定间隔推进进度，避免进度条停在同一个数值上。

        模型接口本身是单次阻塞调用、没有 token 级回调，这里用渐近推进把调用期可视化；
        cap 留出解析/校验阶段的空间，保证进度单调递增。推进值只作建议，最终取值由
        `log` 的实现方（各阶段的单一进度源）裁决，进度状态不在这里二次记账。
        """
        task = asyncio.ensure_future(awaitable)
        percent = start
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    break
                percent = heartbeat_ascent(percent, cap)
                log(percent, message)
        finally:
            # 外层被取消时不留无人认领的任务（否则其异常再也取不回）。
            # 注意：cancel 只能回收 Task，to_thread 里的真实请求/线程仍会跑完，
            # 模型调用的中断仍由 _cancellable_query 的前后检查兜底。
            if not task.done():
                task.cancel()
        return task.result()

    def _check_cancel(self):
        if self.cancellation_check and self.cancellation_check():
            raise RuntimeError(f"Agent [{self.name}] cancelled by user")

    def _require_input(self, input_data: Dict, key: str) -> str:
        value = input_data.get(key)
        if not value:
            raise ValueError(f"Missing required model configuration: {key}")
        return str(value)

    def _cancellable_query(self, llm, prompt: str, image_urls=[], model="gemini-3-flash-preview", safe_content=True, task_id=None, web_search=False, timeout=None):
        """在 LLM 调用前后检查取消状态；timeout 透传给自定义模型客户端。"""
        self._check_cancel()
        # 将位置参数映射给 llm.query
        if timeout is None:
            result = llm.query(prompt, image_urls, model, safe_content, task_id, web_search)
        else:
            result = llm.query(
                prompt, image_urls, model, safe_content, task_id, web_search, timeout=timeout,
            )
        self._check_cancel()
        return result

    def _get_style_prompt(self, style_name: str) -> str:
        """从 prompts/style/{style_name}.txt 读取对应的视觉提示词"""
        import os
        style_file = os.path.join('prompts', 'style', f"{style_name}.txt")
        if os.path.exists(style_file):
            with open(style_file, 'r', encoding='utf-8') as f:
                return f.read().strip()
        # Fallback to English style name if file doesn't exist
        return style_name + " style"

    # -------- 抽象方法 --------

    @abstractmethod
    async def process(self, input_data: Any, intervention: Optional[Dict] = None) -> Dict:
        """
        核心处理逻辑

        Args:
            input_data: 来自上一阶段的输入数据
            intervention: 用户介入修改内容

        Returns:
            dict: { "payload": ..., "requires_intervention": bool }
        """
        pass
