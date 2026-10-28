"""API routers."""

from .configuration import router as configuration_router
from .files import router as files_router
from .health import router as health_router
from .identity import router as identity_router
from .media import router as media_router
from .models import router as models_router
from .pipelines import router as pipelines_router
from .sandbox import router as sandbox_router
from .sessions import router as sessions_router
from .space import router as space_router
from .stages import router as stages_router
from .workflow import router as workflow_router

__all__ = [
    "health_router",
    "identity_router",
    "files_router",
    "workflow_router",
    "sessions_router",
    "space_router",
    "stages_router",
    "sandbox_router",
    "pipelines_router",
    "configuration_router",
    "models_router",
    "media_router",
]
