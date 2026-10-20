import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from api.logging_config import setup_concurrent_logging  # noqa: E402 - sys.path 引导后导入
from config import settings  # noqa: E402 - sys.path 引导后导入

setup_concurrent_logging()

logger = logging.getLogger(__name__)

from api.routers import (  # noqa: E402 - sys.path 引导后导入
    configuration_router,
    files_router,
    health_router,
    models_router,
    pipelines_router,
    sandbox_router,
    sessions_router,
    stages_router,
    workflow_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Video-Claw API")
    logger.info("Result directory mounted at /code/result: %s", settings.RESULT_DIR)
    yield
    logger.info("Video-Claw API shutdown complete")


app = FastAPI(title="Video-Claw", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS enabled for origins: %s", ["*"])

os.makedirs(settings.CODE_DIR, exist_ok=True)
os.makedirs(settings.RESULT_DIR, exist_ok=True)
app.mount("/code/result", StaticFiles(directory=settings.RESULT_DIR), name="code")

app.include_router(health_router)
app.include_router(files_router)
app.include_router(workflow_router)
app.include_router(sessions_router)
app.include_router(stages_router)
app.include_router(sandbox_router)
app.include_router(pipelines_router)
app.include_router(configuration_router)
app.include_router(models_router)
logger.info("API routers registered")


@app.get("/")
async def root():
    return {"service": "Video-Claw", "version": "2.0.0", "health": "/api/health"}
