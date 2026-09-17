from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.logging_config import apply_access_log_setting, apply_log_level_setting
from config import Config, CONFIG_PATH

router = APIRouter(tags=["Configuration"])


class ConfigUpdateRequest(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


@router.get("/api/config")
async def get_config():
    return {
        "config": Config.as_dict(),
        "path": str(CONFIG_PATH),
        # env 覆盖信息：fields 为被环境变量覆盖的点路径，providers/models 为仅 env 引入的条目
        "env_overrides": Config.ENV_OVERRIDES,
    }


@router.put("/api/config")
async def update_config(req: ConfigUpdateRequest):
    try:
        config = Config.update_config(req.values)
    except ValueError as exc:
        # 硬校验失败：返回 400 与具体原因，不落盘
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    apply_log_level_setting()
    apply_access_log_setting()
    return {
        "config": config,
        "path": str(CONFIG_PATH),
        "env_overrides": Config.ENV_OVERRIDES,
    }
