from typing import List, Optional

from pydantic import BaseModel


class SandboxLLMRequest(BaseModel):
    model: str
    prompt: str
    temperature: Optional[float] = 0.7
    web_search: Optional[bool] = False


class SandboxVLMRequest(BaseModel):
    model: str
    prompt: str
    images: List[str]


class SandboxT2IRequest(BaseModel):
    model: str
    prompt: str
    style: Optional[str] = "anime"
    ratio: Optional[str] = "16:9"


class SandboxI2IRequest(BaseModel):
    model: str
    prompt: str
    image: str
    ratio: Optional[str] = "16:9"


class SandboxVideoRequest(BaseModel):
    model: str
    prompt: str
    image: Optional[str] = None
    ratio: Optional[str] = "16:9"
    resolution: Optional[str] = "720P"
    duration: Optional[int] = 5
    fps: Optional[int] = None
    short_edge: Optional[int] = None
    # 音频参考：HTTP(S)/data: URL 或本地已上传文件路径（后者转换为服务端可访问 URL）
    audio_url: Optional[str] = None
    # 参考视频：已上传媒体文件路径（以 input_references multipart 上传）
    reference_videos: Optional[List[str]] = None
