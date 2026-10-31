# -*- coding: utf-8 -*-
"""
智能体基类 - 所有阶段智能体的抽象接口
"""

import glob
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SESSION_PARAM_KEYS = [
    "idea", "user_textbox_input", "style", "video_ratio", "video_resolution",
    "video_duration", "video_fps", "video_short_edge", "audio_reference_url",
    "llm_model", "vlm_model",
    "image_t2i_model", "image_it2i_model", "video_model",
    "video_first_frame_model", "video_start_end_model", "video_reference_model",
    "video_generation_mode",
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

    def _check_cancel(self):
        if self.cancellation_check and self.cancellation_check():
            raise RuntimeError(f"Agent [{self.name}] cancelled by user")

    def _require_input(self, input_data: Dict, key: str) -> str:
        value = input_data.get(key)
        if not value:
            raise ValueError(f"Missing required model configuration: {key}")
        return str(value)

    def _cancellable_query(self, llm, prompt: str, image_urls=[], model="gemini-3-flash-preview", safe_content=True, task_id=None, web_search=False):
        """在 LLM 调用前后检查取消状态"""
        self._check_cancel()
        # 将位置参数映射给 llm.query
        result = llm.query(prompt, image_urls, model, safe_content, task_id, web_search)
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
