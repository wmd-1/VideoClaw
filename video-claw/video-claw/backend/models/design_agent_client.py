"""Design Agent Platform 提示词改写服务客户端。

通过 Design Agent Platform 的通用会话 REST 链路调用
`session_type="minimax-h3-prompt-writing"` 的 H3 提示词改写能力：

1. `POST {base_url}/design_agent/v1/sessions` 创建改写会话；
2. `POST {base_url}/v1/sessions/{sid}/turns` 同步提交一轮改写（阻塞至该轮完成），
   响应体 `assistant_text` 即改写后的完整 H3 提示词；
3. `GET {base_url}/v1/sessions/{sid}` 查询会话（用于失效判断）。

错误按 `code` 分类（调用方按类恢复，不做文本匹配）：
- `unavailable`：连接失败/超时（网络层）；
- `session_gone`：会话不存在或不可恢复（404/410）；
- `session_conflict`：会话未活跃或单写者冲突（409）——可重建会话恢复；
- `quota`：并发/每日配额被拒（429）；
- `turn_failed`：该轮执行失败（502）；
- `http`：其他非 2xx 响应。
"""

import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

from config import Config
from models.custom_common import make_http_client, normalize_base_url

logger = logging.getLogger(__name__)

SESSION_TYPE = "minimax-h3-prompt-writing"
_PATH_PREFIX = "/design_agent"
_API_PREFIX = "/v1"


class DesignAgentError(Exception):
    """Design Agent Platform 调用错误基类；`code` 供调用方分类处理。"""

    def __init__(self, message: str, code: str = "error", status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class DesignAgentUnavailable(DesignAgentError):
    """连接失败 / 超时（网络层不可达）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message, code="unavailable", status=status)


class DesignAgentSessionGone(DesignAgentError):
    """会话不存在或已不可恢复（404/410）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message, code="session_gone", status=status)


class DesignAgentSessionConflict(DesignAgentError):
    """会话未活跃 / 单写者冲突（409）——可重建会话恢复。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message, code="session_conflict", status=status)


class DesignAgentQuotaExceeded(DesignAgentError):
    """配额被拒（429）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message, code="quota", status=status)


def _api_root(base_url: str) -> str:
    """{base_url} → {base_url}/design_agent/v1（自动补齐前缀，容忍已带前缀的写法）。"""
    root = normalize_base_url(base_url)
    if not root:
        raise DesignAgentError("design_agent.base_url 未配置", code="not_configured")
    if not root.endswith(_PATH_PREFIX):
        root = f"{root}{_PATH_PREFIX}"
    return f"{root}{_API_PREFIX}"


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return response.text[:200] or f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if detail:
        return str(detail)
    return str(body)[:200]


class DesignAgentClient:
    """Design Agent Platform 会话 REST 客户端（同步阻塞式）。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        login_name: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
        proxy: Optional[str] = None,
    ):
        self.base_url = base_url if base_url is not None else Config.DESIGN_AGENT_BASE_URL
        self.login_name = login_name if login_name is not None else Config.DESIGN_AGENT_LOGIN_NAME
        self.api_key = api_key if api_key is not None else Config.DESIGN_AGENT_API_KEY
        self.timeout = float(timeout if timeout is not None else Config.TIMEOUT_DESIGN_AGENT)
        self.proxy = proxy if proxy is not None else Config.provider_proxy("design_agent")
        self._root = _api_root(self.base_url)
        if not self.login_name:
            raise DesignAgentError("design_agent.login_name 未配置", code="not_configured")
        # 连接阶段独立短超时（快速失败），读取阶段用完整轮次超时
        self._client = make_http_client(
            base_url=self._root,
            api_key=self.api_key,
            timeout=self.timeout,
            proxy=self.proxy,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DesignAgentClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _headers(self) -> Dict[str, str]:
        return {"X-User-Login-Name": self.login_name}

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> httpx.Response:
        url = f"{self._root}{path}"
        try:
            return self._client.request(method, url, json=json_body, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise DesignAgentUnavailable(f"提示词改写服务请求超时：{method} {path}（上限 {self.timeout:g}s）") from exc
        except httpx.TransportError as exc:
            raise DesignAgentUnavailable(f"提示词改写服务不可达：{method} {path}（{exc.__class__.__name__}）") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response, action: str) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        detail = _extract_detail(response)
        if status in (404, 410):
            raise DesignAgentSessionGone(f"{action}失败（{status}）：{detail}", status=status)
        if status == 409:
            raise DesignAgentSessionConflict(f"{action}失败（409）：{detail}", status=status)
        if status == 429:
            raise DesignAgentQuotaExceeded(f"{action}失败（429）：{detail}", status=status)
        if status == 502:
            raise DesignAgentError(f"{action}失败（502）：{detail}", code="turn_failed", status=status)
        raise DesignAgentError(f"{action}失败（{status}）：{detail}", code="http", status=status)

    def create_session(self) -> str:
        """创建 H3 提示词改写会话，返回外部会话 ID（UUID）。"""
        response = self._request(
            "POST",
            "/sessions",
            {"session_type": SESSION_TYPE, "permission_policy": "full_auto"},
        )
        self._raise_for_status(response, "创建改写会话")
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise DesignAgentError("创建改写会话失败：响应不是 JSON", code="http", status=response.status_code) from exc
        session_id = str(body.get("id") or "").strip()
        if not session_id:
            raise DesignAgentError("创建改写会话失败：响应缺少 id", code="http", status=response.status_code)
        return session_id

    def submit_turn(self, sid: str, text: str) -> str:
        """同步提交一轮改写请求，返回该轮 `assistant_text`。

        网络类错误（不可达/超时）自动重试一次；重试仍失败按原错误抛出。
        """
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                response = self._request("POST", f"/sessions/{sid}/turns", {"text": text})
                self._raise_for_status(response, "提交改写请求")
                try:
                    body = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise DesignAgentError("提交改写请求失败：响应不是 JSON", code="http", status=response.status_code) from exc
                return str(body.get("assistant_text") or "")
            except DesignAgentUnavailable as exc:
                last_error = exc
                if attempt == 1:
                    logger.warning("Design Agent turn submit network error, retrying once: %s", exc)
                    time.sleep(1.0)
                    continue
                raise
        raise last_error  # pragma: no cover - 理论不可达

    def get_session(self, sid: str) -> Dict[str, Any]:
        """查询会话详情（失效判断用）；404/410 抛 DesignAgentSessionGone。"""
        response = self._request("GET", f"/sessions/{sid}")
        self._raise_for_status(response, "查询改写会话")
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise DesignAgentError("查询改写会话失败：响应不是 JSON", code="http", status=response.status_code) from exc
