from copy import deepcopy
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.logging_config import apply_access_log_setting, apply_log_level_setting
from config import CONFIG_PATH, Config
from core.identity import CurrentUser, get_current_user, require_admin

router = APIRouter(tags=["Configuration"])


class ConfigUpdateRequest(BaseModel):
    values: Dict[str, Any] = Field(default_factory=dict)


def _redact_config(config: Dict[str, Any], is_admin: bool) -> Dict[str, Any]:
    """Never return credentials to non-admins; admins still need edit access."""
    data = deepcopy(config)
    if is_admin:
        return data
    sensitive = {"api_key", "secret_key", "access_key", "dsn"}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in sensitive and value:
                    node[key] = "***"
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return data


@router.get("/api/config")
async def get_config(user: CurrentUser = Depends(get_current_user)):
    return {
        "config": _redact_config(Config.as_dict(), user.is_admin),
        "path": str(CONFIG_PATH),
        # env 覆盖信息：fields 为被环境变量覆盖的点路径，providers/models 为仅 env 引入的条目
        "env_overrides": Config.ENV_OVERRIDES,
    }


@router.put("/api/config")
async def update_config(req: ConfigUpdateRequest, _: CurrentUser = Depends(require_admin)):
    try:
        config = Config.update_config(req.values)
    except ValueError as exc:
        # 硬校验失败：返回 400 与具体原因，不落盘
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    apply_log_level_setting()
    apply_access_log_setting()
    return {
        "config": _redact_config(config, True),
        "path": str(CONFIG_PATH),
        "env_overrides": Config.ENV_OVERRIDES,
    }
