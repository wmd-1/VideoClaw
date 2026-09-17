"""模型连通性测试接口：接受自定义模型条目与其供应商（草稿或已保存均可），
按类型发起最小请求验证连通性；MUST NOT 持久化配置。"""

import base64
import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import CUSTOM_MODEL_TYPES, CUSTOM_PROTOCOLS, Config

router = APIRouter(tags=["Models"])
logger = logging.getLogger(__name__)

# 1×1 PNG：作为 VLM/图生图/视频连通测试的内置测试图
_TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_TEST_PNG_DATA_URL = f"data:image/png;base64,{_TEST_PNG_B64}"


class ModelTestRequest(BaseModel):
    model: Dict[str, Any]
    provider: Any = None  # dict（供应商草稿）或 str（已保存供应商名称）
    model_type: str


def _resolve_provider(req: ModelTestRequest) -> tuple:
    provider = req.provider
    if isinstance(provider, str) and provider:
        provider_config = (Config.CONFIG.get("api_providers") or {}).get(provider)
        if not isinstance(provider_config, dict):
            raise HTTPException(400, f"供应商不存在: {provider}")
        return provider, provider_config
    if isinstance(provider, dict):
        provider_key = str(provider.get("key") or (req.model or {}).get("provider") or "").strip()
        if not provider_key:
            raise HTTPException(400, "供应商缺少名称（key）")
        return provider_key, provider
    raise HTTPException(400, "provider 必须为供应商名称或供应商配置对象")


def _build_meta(req: ModelTestRequest) -> Dict[str, Any]:
    model_entry = req.model if isinstance(req.model, dict) else {}
    model_id = str(model_entry.get("id") or "").strip()
    if not model_id:
        raise HTTPException(400, "model.id 不能为空")
    if req.model_type not in CUSTOM_MODEL_TYPES:
        raise HTTPException(400, f"model_type 非法（可选：{'/'.join(CUSTOM_MODEL_TYPES)}）")
    provider_key, provider_config = _resolve_provider(req)
    protocol = provider_config.get("protocol")
    base_url = str(provider_config.get("base_url") or "")
    if protocol not in CUSTOM_PROTOCOLS or not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, f"供应商 {provider_key} 缺少 protocol 或 base_url")
    return {
        "id": model_id,
        "name": model_entry.get("name") or model_id,
        "provider": provider_key,
        "provider_label": provider_config.get("name") or provider_key,
        "family": "custom",
        "protocol": protocol,
        "base_url": base_url,
        "api_key": str(provider_config.get("api_key") or ""),
        "request_model": model_entry.get("model") or model_id,
        "type": [req.model_type],
        "abilities": list(model_entry.get("abilities") or []),
        "custom": True,
        # 连通测试使用最小时长参数
        "capabilities": {"duration": {"min": 1, "max": 60, "integer": True, "verified": True}},
    }


def _test_chat(meta: Dict[str, Any], model_type: str) -> str:
    from models.custom_llm import CustomChatClient

    client = CustomChatClient(meta, timeout=60.0)
    try:
        if model_type == "vlm":
            answer = client.query("请回复 OK", image_urls=[_TEST_PNG_DATA_URL])
        else:
            answer = client.query("请回复 OK")
        if not str(answer or "").strip():
            raise RuntimeError("模型返回空内容")
        return f"返回内容: {str(answer)[:80]}"
    finally:
        client.close()


def _test_image(meta: Dict[str, Any], model_type: str) -> str:
    from models.custom_image import CustomImageClient

    save_dir = os.path.join(Config.TEMP_DIR, "model_test")
    os.makedirs(save_dir, exist_ok=True)
    client = CustomImageClient(meta, timeout=180.0)
    try:
        image_paths = None
        if model_type == "i2i":
            test_image = os.path.join(save_dir, "test_input.png")
            with open(test_image, "wb") as f:
                f.write(base64.b64decode(_TEST_PNG_B64))
            image_paths = [test_image]
        paths = client.generate_image(
            "a simple red dot on white background",
            image_paths=image_paths,
            save_dir=save_dir,
            video_ratio="1:1",
            resolution="512x512",
        )
        return f"生成 {len(paths)} 张图片"
    finally:
        client.close()


def _test_video(meta: Dict[str, Any]) -> str:
    from models.custom_video import CustomVideoClient

    save_dir = os.path.join(Config.TEMP_DIR, "model_test")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, "test_video.mp4")
    client = CustomVideoClient(meta, poll_interval=3.0, poll_timeout=120.0, timeout=120.0)
    try:
        client.generate_video(
            prompt="a small red dot slowly moving on white background",
            image_path=None,
            save_path=save_path,
            duration=1,
            video_ratio="1:1",
            resolution="480x480",
        )
        return "任务完成并成功下载"
    except RuntimeError as exc:
        if "轮询超时" in str(exc):
            # 任务已成功创建但未在等待窗口内完成：连通性判定通过
            return "任务已创建但未在等待窗口内完成（连通性判定通过）"
        raise
    finally:
        client.close()


@router.post("/api/models/test")
async def test_model_connection(req: ModelTestRequest):
    meta = _build_meta(req)
    started = time.time()
    logger.info(
        "Model connectivity test: id=%s type=%s protocol=%s",
        meta["id"],
        req.model_type,
        meta["protocol"],
    )
    try:
        if req.model_type in ("llm", "vlm"):
            detail = await run_in_threadpool(_test_chat, meta, req.model_type)
        elif req.model_type in ("t2i", "i2i"):
            detail = await run_in_threadpool(_test_image, meta, req.model_type)
        else:
            detail = await run_in_threadpool(_test_video, meta)
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "success": True,
            "model_type": req.model_type,
            "elapsed_ms": elapsed_ms,
            "reason": "",
            "detail": detail,
        }
    except HTTPException:
        raise
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        logger.warning(
            "Model connectivity test failed: id=%s type=%s", meta["id"], req.model_type, exc_info=True
        )
        return {
            "success": False,
            "model_type": req.model_type,
            "elapsed_ms": elapsed_ms,
            "reason": str(exc)[:500],
            "detail": "",
        }