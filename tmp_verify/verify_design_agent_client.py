# 任务 1.2 验证：design_agent_client 错误分类与调用链（mock httpx 客户端）
import sys

sys.path.insert(0, "/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


import httpx

from models import design_agent_client as dac
from models.design_agent_client import (
    DesignAgentClient,
    DesignAgentError,
    DesignAgentQuotaExceeded,
    DesignAgentSessionGone,
    DesignAgentUnavailable,
)


class FakeTransport(httpx.BaseTransport):
    """按路径返回固定响应；记录请求供断言。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def make_client(handler, **kwargs):
    transport = FakeTransport(handler)
    client = DesignAgentClient(
        base_url="http://fake:8001",
        login_name="videoclaw",
        api_key="",
        timeout=5,
        **kwargs,
    )
    client._client = httpx.Client(transport=transport, base_url=client._root, timeout=5)
    client._transport = transport
    return client


# 场景 1：create_session 成功（201），路径与前缀正确，带身份头
def h1(request):
    assert request.url.path == "/design_agent/v1/sessions", request.url.path
    assert request.headers.get("X-User-Login-Name") == "videoclaw"
    import json as _json
    body = _json.loads(request.content)
    assert body["session_type"] == "minimax-h3-prompt-writing"
    return httpx.Response(201, json={"id": "abc-123", "status": "live"})


c1 = make_client(h1)
sid = c1.create_session()
check("1 create_session 成功返回 id", sid == "abc-123")
c1.close()

# 场景 2：submit_turn 成功（200）返回 assistant_text
def h2(request):
    if request.url.path.endswith("/turns"):
        return httpx.Response(200, json={"assistant_text": "PROMPT TEXT", "status": "completed"})
    return httpx.Response(500)


c2 = make_client(h2)
text = c2.submit_turn("abc-123", "改写这段")
check("2 submit_turn 返回 assistant_text", text == "PROMPT TEXT")
c2.close()

# 场景 3：404 → DesignAgentSessionGone
c3 = make_client(lambda request: httpx.Response(404, json={"detail": "会话未找到"}))
try:
    c3.get_session("gone")
    check("3 404 抛 SessionGone", False)
except DesignAgentSessionGone as exc:
    check("3 404 抛 SessionGone", exc.code == "session_gone" and exc.status == 404)
c3.close()

# 场景 4：429 → DesignAgentQuotaExceeded
c4 = make_client(lambda request: httpx.Response(429, json={"detail": "并发会话配额已用尽"}))
try:
    c4.submit_turn("abc", "x")
    check("4 429 抛 QuotaExceeded", False)
except DesignAgentQuotaExceeded as exc:
    check("4 429 抛 QuotaExceeded", exc.code == "quota")
c4.close()

# 场景 5：连接错误 → Unavailable，submit_turn 自动重试一次后抛出
flaky = {"n": 0}


def h5(request):
    flaky["n"] += 1
    raise httpx.ConnectError("refused")


c5 = make_client(h5)
try:
    c5.submit_turn("abc", "x")
    check("5 连接错误重试后抛 Unavailable", False)
except DesignAgentUnavailable:
    check("5 连接错误重试后抛 Unavailable", flaky["n"] == 2, f"attempts={flaky['n']}")
c5.close()

# 场景 6：第一次网络错误、第二次成功 → 重试生效
flaky2 = {"n": 0}


def h6(request):
    flaky2["n"] += 1
    if flaky2["n"] == 1:
        raise httpx.ReadTimeout("timed out")
    return httpx.Response(200, json={"assistant_text": "OK"})


c6 = make_client(h6)
text6 = c6.submit_turn("abc", "x")
check("6 网络错误重试一次后成功", text6 == "OK" and flaky2["n"] == 2)
c6.close()

# 场景 7：502 → turn_failed；409 → session_conflict
def h7(request):
    import json as _json
    body = _json.loads(request.content)
    return httpx.Response(502 if body["text"] == "bad" else 409, json={"detail": "err"})


c7 = make_client(h7)
try:
    c7.submit_turn("abc", "bad")
    check("7a 502 抛 turn_failed", False)
except DesignAgentError as exc:
    check("7a 502 抛 turn_failed", exc.code == "turn_failed")
try:
    c7.submit_turn("abc", "conflict")
    check("7b 409 抛 session_conflict", False)
except DesignAgentError as exc:
    check("7b 409 抛 session_conflict", exc.code == "session_conflict")
c7.close()

# 场景 8：超时 → Unavailable
def h8(request):
    raise httpx.TimeoutException("read timeout")


c8 = make_client(h8)
try:
    c8.submit_turn("abc", "x")
    check("8 超时抛 Unavailable", False)
except DesignAgentUnavailable:
    check("8 超时抛 Unavailable", True)
c8.close()

# 场景 9：base_url 已带 /design_agent 前缀时不重复拼接
from models.design_agent_client import _api_root
check("9a 自动拼接前缀", _api_root("http://h:8001") == "http://h:8001/design_agent/v1")
check("9b 已带前缀不重复", _api_root("http://h:8001/design_agent/") == "http://h:8001/design_agent/v1")

# 场景 10：base_url 为空 → not_configured
try:
    DesignAgentClient(base_url="", login_name="u", timeout=5)
    check("10 空 base_url 抛 not_configured", False)
except DesignAgentError as exc:
    check("10 空 base_url 抛 not_configured", exc.code == "not_configured")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
