import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from api.dependencies import workflow_engine  # noqa: E402 - sys.path 引导后导入
from api.logging_config import setup_concurrent_logging  # noqa: E402 - sys.path 引导后导入
from config import settings  # noqa: E402 - sys.path 引导后导入
from core.identity import UserIdentityMiddleware  # noqa: E402 - sys.path 引导后导入
from core.media_refs import prune_refs  # noqa: E402 - sys.path 引导后导入
from core.object_operations import prune_empty_execution_dirs, start_cleanup_loop  # noqa: E402 - sys.path 引导后导入
from db.database import close_pool, init_db  # noqa: E402 - sys.path 引导后导入
from db.repositories import StageArtifactRepo, TaskRepo  # noqa: E402 - sys.path 引导后导入
from storage import get_storage  # noqa: E402 - sys.path 引导后导入

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
    identity_router,
    media_router,
    models_router,
    pipelines_router,
    sandbox_router,
    sessions_router,
    space_router,
    stages_router,
    workflow_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Video-Claw API")
    logger.info("Result directory mounted at /code/result: %s", settings.RESULT_DIR)
    dsn = str(getattr(settings, "DATABASE_DSN", "") or "").strip()
    if dsn:
        # 生产契约：数据库不可用即启动失败，不做 SQLite/JSON 静默兜底。
        init_db()
        # MinIO may still be starting even though compose has started it; retry briefly.
        last_error = None
        for _ in range(10):
            try:
                get_storage().ensure_bucket()
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        if last_error is not None:
            raise RuntimeError(f"object storage bucket initialization failed: {last_error}") from last_error
        boot_id = str(getattr(workflow_engine, "_boot_id", ""))
        stages = StageArtifactRepo().sweep_stale_running(boot_id)
        tasks = TaskRepo().sweep_stale_running(boot_id)
        pruned = prune_refs()
        empty_dirs = prune_empty_execution_dirs()  # 启动期尚无执行：只移除空目录
        loaded = workflow_engine.load_sessions()
        # 对象操作日志清理器：启动后及每 60 秒扫描到期操作（DB 不可用时本轮跳过）
        cleanup_stop = start_cleanup_loop(boot_id)
        logger.info(
            "Startup recovery: boot_id=%s stages=%d tasks=%d refs_pruned=%d empty_dirs=%d sessions=%d",
            boot_id, stages, tasks, pruned, empty_dirs, loaded,
        )
    else:
        logger.warning("database.dsn is empty; startup recovery skipped (dev compatibility only)")
        cleanup_stop = None
    yield
    if cleanup_stop is not None:
        cleanup_stop.set()
    close_pool()
    logger.info("Video-Claw API shutdown complete")


app = FastAPI(title="Video-Claw", version="2.0.0", lifespan=lifespan)

app.add_middleware(UserIdentityMiddleware)
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
app.include_router(identity_router)
app.include_router(media_router)
app.include_router(health_router)
app.include_router(files_router)
app.include_router(workflow_router)
app.include_router(sessions_router)
app.include_router(space_router)
app.include_router(stages_router)
app.include_router(sandbox_router)
app.include_router(pipelines_router)
app.include_router(configuration_router)
app.include_router(models_router)
logger.info("API routers registered")


@app.get("/")
async def root():
    return {"service": "Video-Claw", "version": "2.0.0", "health": "/api/health"}
