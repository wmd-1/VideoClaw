"""自定义 LLM/VLM 客户端：OpenAI 兼容 `chat/completions`（openai / vllm-omni / sglang 通用）。"""

import logging
import os
import sys
from typing import Any, Dict, List, Optional

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import httpx

from models.custom_common import build_custom_client_kwargs, connection_hint, make_http_client, to_data_url

logger = logging.getLogger(__name__)


class CustomChatClient:
    """LLM/VLM 统一走 chat/completions；图片以 data URL 传递（VLM），web_search 参数忽略。"""

    def __init__(self, meta: Dict[str, Any], timeout: float = 300.0):
        self._meta = meta
        self._model = str(meta.get("request_model") or meta.get("id") or "")
        self._base_url = str(meta.get("base_url") or "")
        self._client = make_http_client(**build_custom_client_kwargs(meta, timeout=timeout))

    def query(
        self,
        prompt: str,
        image_urls: Optional[List[str]] = None,
        model: str = "",
        web_search: bool = False,
    ) -> str:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in image_urls or []:
            content.append({"type": "image_url", "image_url": {"url": to_data_url(image)}})
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": content}],
        }
        try:
            response = self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"自定义模型 {self._model} 对话失败（目标 {self._base_url}/chat/completions）: {exc}"
                f"{connection_hint(exc, self._base_url)}"
            ) from exc

        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"自定义模型 {self._model} 响应格式异常: {response.text[:200]}"
            ) from exc

        content_value = message.get("content")
        if isinstance(content_value, list):
            # 部分服务返回分段内容（text parts）
            text = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content_value
            )
        else:
            text = str(content_value or "")
        return text

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - 关闭失败无需上抛
            pass