import time

from fastapi import APIRouter

from core.media_metrics import snapshot as media_metrics_snapshot

router = APIRouter(tags=["Health"])


@router.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "timestamp": time.time(),
        "media_streams": media_metrics_snapshot(),
    }
