from fastapi import APIRouter

from config import Config

router = APIRouter(tags=["Stages"])

# 全量阶段定义；prompt_rewrite 仅在 design_agent.enable=true 时返回
_ALL_STAGES = [
    {"id": "script_generation", "name": "剧本生成", "order": 1, "description": "将灵感转化为结构化剧本"},
    {"id": "character_design", "name": "角色/场景设计", "order": 2, "description": "生成角色设计图和场景背景"},
    {"id": "storyboard", "name": "分镜设计", "order": 3, "description": "设计镜头语言和分镜脚本"},
    {"id": "reference_generation", "name": "参考图生成", "order": 4, "description": "生成高精度参考图"},
    {"id": "prompt_rewrite", "name": "提示词改写", "order": 5, "description": "将分镜描述改写为 MiniMax H3 规范提示词（Design Agent Platform）"},
    {"id": "video_generation", "name": "视频生成", "order": 6, "description": "将参考图/分镜图生成视频"},
    {"id": "post_production", "name": "后期剪辑", "order": 7, "description": "拼接视频片段为最终成片"},
]
_PROMPT_REWRITE_ID = "prompt_rewrite"


def _enabled_stages() -> list:
    """按 design_agent.enable 过滤并重排 order：禁用时返回原六阶段。"""
    stages = [dict(stage) for stage in _ALL_STAGES if stage["id"] != _PROMPT_REWRITE_ID or Config.DESIGN_AGENT_ENABLED]
    for index, stage in enumerate(stages, start=1):
        stage["order"] = index
    return stages


@router.get("/api/stages")
async def list_stages():
    return {"stages": _enabled_stages(), "prompt_rewrite_enabled": bool(Config.DESIGN_AGENT_ENABLED)}
