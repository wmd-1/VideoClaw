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


def assert_single_process_premise() -> None:
    """执行归属判据（boot_id）只支持单进程；多 worker 启动必须显式失败。

    覆盖 WEB_CONCURRENCY / UVICORN_WORKERS 与 sys.argv 中的 --workers/-w（含 = 形式）。
    外部进程管理器另行拉起多实例不在检测范围，需在部署文档声明不受支持。
    """
    violations = []
    for env_name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.environ.get(env_name, "").strip()
        if raw.isdigit() and int(raw) > 1:
            violations.append(f"{env_name}={raw}")
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if (
            arg in ("--workers", "-w")
            and index + 1 < len(argv)
            and argv[index + 1].isdigit()
            and int(argv[index + 1]) > 1
        ):
            violations.append(f"{arg} {argv[index + 1]}")
        elif arg.startswith("--workers="):
            value = arg.split("=", 1)[1].strip()
            if value.isdigit() and int(value) > 1:
                violations.append(arg)
    if violations:
        raise RuntimeError(
            "执行归属判据仅支持单进程，检测到多 worker 配置: "
            f"{', '.join(violations)}。"
            "请移除 WEB_CONCURRENCY/UVICORN_WORKERS 或 --workers/-w 参数后重启；"
            "多实例共享会话目录不受支持。"
        )


assert_single_process_premise()

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
