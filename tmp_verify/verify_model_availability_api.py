"""harden-model-selector-fallback 后端接口验证。

在既有 video-claw-backend 容器内运行：
  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - \
    < tmp_verify/verify_model_availability_api.py

覆盖：
- 模块级：注入四类可用性场景（未注册 / 供应商缺失 / 供应商残缺 / 可用），断言
  model_availability 的 code/available。
- API 级：TestClient 走 /api/models/availability，断言查询参数缺失 400；三类可用性
  code 与 available 与模块级一致；不改动 /api/models 契约（回归对照）。
"""
import sys

sys.path.insert(0, "/app")

import config as cfg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.app import app  # noqa: E402
from models.config_model import model_availability  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


# ── 注入合成场景（进程内，不写盘；不影响外部运行中的服务）──
providers = cfg.Config.CONFIG.setdefault("api_providers", {})
providers["verify-av-ok"] = {
    "protocol": "openai",
    "base_url": "http://127.0.0.1:1/v1",
    "api_key": "",
    "enable_proxy": False,
}
providers["verify-av-broken"] = {
    "protocol": "",  # 非法协议
    "base_url": "ftp://x",  # 非 http(s)
    "api_key": "",
    "enable_proxy": False,
}
customs = cfg.Config.CONFIG.setdefault("custom_models", [])
customs.extend([
    {"id": "verify-av-available", "provider": "verify-av-ok", "model": "x", "types": ["llm"], "abilities": []},
    {"id": "verify-av-missing-provider", "provider": "verify-av-ghost", "model": "x", "types": ["llm"], "abilities": []},
    {"id": "verify-av-incomplete-provider", "provider": "verify-av-broken", "model": "x", "types": ["llm"], "abilities": []},
])

try:
    # ── 模块级：四类 code ──
    av = model_availability("verify-av-nonexistent-xyz")
    check("模块级 not_registered", av["available"] is False and av["code"] == "not_registered", str(av))

    av = model_availability("verify-av-missing-provider")
    check("模块级 provider_missing", av["available"] is False and av["code"] == "provider_missing", str(av))

    av = model_availability("verify-av-incomplete-provider")
    check("模块级 provider_incomplete", av["available"] is False and av["code"] == "provider_incomplete", str(av))

    av = model_availability("verify-av-available")
    check("模块级 available", av["available"] is True and av["code"] == "", str(av))

    # ── API 级：TestClient ──
    api = TestClient(app)

    # 1) 查询参数缺失 → 400
    resp = api.get("/api/models/availability")
    check("API 缺失 model 参数返回 400", resp.status_code == 400, f"status={resp.status_code} body={resp.text[:80]}")

    # 2) 空白字符串 → 400（strip 后为空）
    resp = api.get("/api/models/availability?model=%20%20")
    check("API 空 model 返回 400", resp.status_code == 400, f"status={resp.status_code}")

    # 3) 未注册：available=False，code=not_registered，HTTP 200
    resp = api.get("/api/models/availability?model=verify-av-nonexistent-xyz")
    body = resp.json() if resp.status_code == 200 else {}
    check(
        "API not_registered 返回 200 且 code=not_registered",
        resp.status_code == 200 and body.get("available") is False and body.get("code") == "not_registered",
        f"status={resp.status_code} body={str(body)[:120]}",
    )
    check("API not_registered 带非空 reason", bool(body.get("reason")), f"reason={body.get('reason')}")

    # 4) provider_missing / provider_incomplete / available 三种 code 与模块级一致
    for mid, expect in (
        ("verify-av-missing-provider", ("provider_missing", False)),
        ("verify-av-incomplete-provider", ("provider_incomplete", False)),
        ("verify-av-available", ("", True)),
    ):
        resp = api.get(f"/api/models/availability?model={mid}")
        body = resp.json() if resp.status_code == 200 else {}
        code_ok, avail_ok = body.get("code") == expect[0], body.get("available") is expect[1]
        check(f"API {mid} → available={expect[1]} code={expect[0]!r}", resp.status_code == 200 and code_ok and avail_ok, str(body)[:120])

    # 5) /api/models 契约不受影响：既有按类型过滤仍工作，且新接口不改变它
    resp_type = api.get("/api/models?model_type=llm")
    check(
        "回归 /api/models?model_type=llm 仍返回 200 且结构含 models",
        resp_type.status_code == 200 and isinstance(resp_type.json().get("models"), list),
        f"status={resp_type.status_code}",
    )

    # 6) 动态断言：遍历运行时所有「供应商完整」的自定义模型，API 与模块级判定逐项一致
    raw_customs = cfg.Config.CONFIG.get("custom_models", []) or []
    provider_map = cfg.Config.CONFIG.get("api_providers", {}) or {}
    from config import CUSTOM_PROTOCOLS  # noqa: E402

    def provider_complete(item):
        p = provider_map.get(str(item.get("provider") or ""))
        if not isinstance(p, dict):
            return False
        if p.get("protocol") not in CUSTOM_PROTOCOLS:
            return False
        base = str(p.get("base_url") or "")
        return base.startswith(("http://", "https://"))

    drift = []
    checked = 0
    for item in raw_customs:
        mid = str(item.get("id") or "")
        if not mid:
            continue
        checked += 1
        mod = model_availability(mid)
        api_resp = api.get(f"/api/models/availability?model={mid}")
        if api_resp.status_code != 200:
            drift.append((mid, "api_status", api_resp.status_code))
            continue
        api_body = api_resp.json()
        if api_body.get("available") != mod.get("available") or api_body.get("code") != mod.get("code"):
            drift.append((mid, mod, api_body))
        # 供应商完整的自定义模型必须 available=True
        if provider_complete(item) and not mod.get("available"):
            drift.append((mid, "expected_available", mod))
    check(
        f"API 与模块级判定逐项一致（共 {checked} 条自定义）",
        not drift,
        str(drift[:2])[:200],
    )

finally:
    # ── 清理注入的合成分支（进程内），避免污染其他断言 ──
    cfg.Config.CONFIG["custom_models"] = [
        item for item in cfg.Config.CONFIG.get("custom_models", [])
        if not str(item.get("id") or "").startswith("verify-av-")
    ]
    for key in ("verify-av-ok", "verify-av-broken"):
        cfg.Config.CONFIG.get("api_providers", {}).pop(key, None)

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
