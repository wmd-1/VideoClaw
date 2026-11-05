from typing import Any, Dict, Optional

from pydantic import BaseModel, field_validator

from core.language import normalize_language


class ProjectStartRequest(BaseModel):
    idea: str
    file_path: Optional[str] = None
    # 剧本来源：idea=按灵感生成（默认）；upload=保真导入上传剧本
    script_source: Optional[str] = "idea"
    # 导入剧本时是否执行质检；默认关闭，长文（>=2 万字符）会自动触发
    run_quality_check: Optional[bool] = False
    # 显式语言（zh/en）；缺省时由会话在首次阶段执行前统一推断并落库
    language: Optional[str] = None
    style: Optional[str] = None
    video_ratio: Optional[str] = "9:16"
    video_resolution: Optional[str] = "720P"
    video_duration: Optional[int] = None
    video_fps: Optional[int] = None
    video_short_edge: Optional[int] = None
    # 音频参考（可选）：透传至自定义视频模型 ref2va（audio_reference.audio_url）；
    # 未提供时行为与现状完全一致
    audio_reference_url: Optional[str] = None
    expand_idea: Optional[bool] = True
    llm_model: Optional[str] = None
    vlm_model: Optional[str] = None
    image_t2i_model: Optional[str] = None
    image_it2i_model: Optional[str] = None
    video_model: Optional[str] = None
    video_first_frame_model: Optional[str] = None
    video_start_end_model: Optional[str] = None
    video_reference_model: Optional[str] = None
    video_generation_mode: Optional[str] = "first_frame"
    enable_concurrency: Optional[bool] = True
    web_search: Optional[bool] = False
    episodes: Optional[int] = None

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        normalized = normalize_language(value)
        if not normalized:
            raise ValueError("language 必须是 zh 或 en")
        return normalized


class InterventionRequest(BaseModel):
    stage: str
    modifications: Dict[str, Any]
