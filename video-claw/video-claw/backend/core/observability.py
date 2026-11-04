"""轻量调用观测（D1/OP-05）。

设计约束：
- 仅记录模型调用与校验重试，不记录进度事件；
- JSONL，每进程每日一个文件，单行不超过 4KB；
- 不记录 prompt/响应正文、URL、密钥、owner 身份；
- 写盘失败只计数，不阻断主流程；
- 聚合/清理由 tools/observability_report.py 完成。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 4096
DEFAULT_MAX_FILE_BYTES = 1_000_000_000

_CALL_CTX: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "observability_call_ctx", default={},
)
_WRITE_LOCK = threading.Lock()
_DROPPED = 0
_FILE_SIZES: Dict[str, int] = {}


def _base_dir() -> Path:
    raw = os.getenv("VC_OBSERVABILITY_DIR", "").strip()
    return Path(raw) if raw else Path("data/observability")


def _file_path(now: Optional[datetime] = None, *, dropped: bool = False) -> Path:
    now = now or datetime.now(timezone.utc)
    prefix = "dropped" if dropped else "calls"
    return _base_dir() / f"{prefix}-{now:%Y%m%d}-{os.getpid()}.jsonl"


@contextlib.contextmanager
def call_context(**fields: Any) -> Iterator[None]:
    """为当前异步/线程上下文附加 stage/session/execution 等字段。"""
    token = _CALL_CTX.set({**_CALL_CTX.get(), **fields})
    try:
        yield
    finally:
        _CALL_CTX.reset(token)


def prompt_hash(text: Any) -> str:
    raw = str(text or "").encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]


def content_version(text: Any) -> str:
    return prompt_hash(text)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|authorization|signature|secret)=([^\s&]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+")
_AUTH_HEADER_RE = re.compile(r"(?i)\bAuthorization\s*:\s*[^\r\n]+")
_PROMPT_KV_RE = re.compile(r"(?i)\b(prompt|messages|content|body)\s*[:=]\s*\S+")
_JSON_KV_RE = re.compile(r'(?i)"(prompt|messages|content|body)"\s*:\s*"[^"]*"')
_LONG_QUOTE_RE = re.compile(r"""["“'][^"”']{20,}["”']""")


def _sanitize_error(text: Any) -> str:
    """错误摘要脱敏：URL/签名/凭据/请求体/长引号内容一律移除。"""
    cleaned = str(text or "")
    cleaned = _BEARER_RE.sub("Bearer <redacted>", cleaned)
    cleaned = _AUTH_HEADER_RE.sub("Authorization: <redacted>", cleaned)
    cleaned = _SECRET_RE.sub(r"\1=<redacted>", cleaned)
    cleaned = _JSON_KV_RE.sub(lambda m: f'"{m.group(1)}": "<redacted>"', cleaned)
    cleaned = _PROMPT_KV_RE.sub(r"\1=<redacted>", cleaned)
    cleaned = _LONG_QUOTE_RE.sub("<redacted>", cleaned)
    cleaned = _URL_RE.sub("<url>", cleaned)
    return cleaned[:200]


def _provider_for_model(model: str) -> str:
    name = str(model or "").lower()
    for provider in ("deepseek", "gemini", "gpt", "qwen", "dashscope", "seedream", "kling", "seedance", "custom"):
        if provider in name:
            return provider
    return ""


def _write_record(record: Dict[str, Any], *, dropped: bool = False) -> bool:
    global _DROPPED
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        record = {
            "schema_version": SCHEMA_VERSION,
            "event_type": record.get("event_type", "model_call"),
            "timestamp": record.get("timestamp"),
            "pid": record.get("pid"),
            "call_kind": record.get("call_kind"),
            "model": str(record.get("model") or "")[:80],
            "stage": str(record.get("stage") or "")[:80],
            "session_id": str(record.get("session_id") or "")[:100],
            "execution_id": str(record.get("execution_id") or "")[:100],
            "status": record.get("status"),
            "truncated": True,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path = _file_path(dropped=dropped)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = _FILE_SIZES.get(str(path), path.stat().st_size if path.exists() else 0)
        if not dropped:
            max_bytes = int(os.getenv("VC_OBSERVABILITY_MAX_BYTES", DEFAULT_MAX_FILE_BYTES))
            if current + len(line.encode("utf-8")) > max_bytes:
                _record_dropped(1)
                return False
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        _FILE_SIZES[str(path)] = current + len(line.encode("utf-8")) + 1
        return True
    except Exception:
        _DROPPED += 1
        return False


def _record_dropped(count: int) -> None:
    global _DROPPED
    _DROPPED += count
    record = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "observability_dropped",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "dropped_events": _DROPPED,
    }
    try:
        path = _file_path(dropped=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def record_call(
    *,
    call_kind: str,
    model: str,
    provider: str = "",
    status: str = "ok",
    event_type: str = "model_call",
    session_id: str = "",
    operation: str = "",
    stage: str = "",
    execution_id: str = "",
    parent_execution_id: str = "",
    template_name: str = "",
    template_version: str = "",
    mode: str = "",
    policy_version: str = "",
    cache_hit: bool = False,
    cache_key_kind: str = "",
    attempt: int = 1,
    retry_reason: str = "",
    validation_retries: int = 0,
    latency_ms: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    usage_source: str = "",
    estimated_cost: Optional[float] = None,
    error_type: str = "",
    error: str = "",
    http_status: Optional[int] = None,
    prompt_hash: str = "",
    image_count: Optional[int] = None,
    output_count: Optional[int] = None,
    video_duration: Optional[int] = None,
) -> bool:
    """记录一次模型调用/重试事件；失败返回 False，不抛异常。"""
    ctx = _CALL_CTX.get()
    tokens_known = any(value is not None for value in (input_tokens, output_tokens, total_tokens))
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "call_kind": call_kind,
        "model": str(model or ""),
        "provider": provider or _provider_for_model(model),
        "status": status,
        "session_id": str(session_id or ctx.get("session_id") or ""),
        "operation": str(operation or ctx.get("operation") or ""),
        "stage": str(stage or ctx.get("stage") or ""),
        "execution_id": str(execution_id or ctx.get("execution_id") or ""),
        "parent_execution_id": str(parent_execution_id or ctx.get("parent_execution_id") or ""),
        "template_name": str(template_name or ctx.get("template_name") or ""),
        "template_version": str(template_version or ctx.get("template_version") or ""),
        "mode": str(mode or ctx.get("mode") or ""),
        "policy_version": str(policy_version or ctx.get("policy_version") or ""),
        "cache_hit": bool(cache_hit),
        "cache_key_kind": str(cache_key_kind or ""),
        "attempt": int(attempt or 1),
        "retry_reason": str(retry_reason or ""),
        "validation_retries": int(validation_retries or 0),
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "usage_source": usage_source or ("provider" if tokens_known else "unknown"),
        "estimated_cost": estimated_cost if isinstance(estimated_cost, (int, float)) else None,
        "error_type": str(error_type or ""),
        "error": _sanitize_error(error),
        "http_status": http_status,
        "prompt_hash": str(prompt_hash or ""),
        "image_count": image_count,
        "output_count": output_count,
        "video_duration": video_duration,
    }
    return _write_record(record)


def dropped_events() -> int:
    return _DROPPED


def summarize_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 stage/model/template/cache_hit 汇总；供工具与测试共用。"""
    def _bucket() -> Dict[str, Any]:
        return {
            "calls": 0, "ok": 0, "failed": 0, "cache_hits": 0,
            "retries": 0, "latencies": [], "tokens": 0, "tokens_unknown": 0,
        }

    by_stage: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    by_template: Dict[str, Dict[str, Any]] = {}
    by_kind: Dict[str, Dict[str, Any]] = {}
    dropped = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("event_type") == "observability_dropped":
            dropped += int(record.get("dropped_events") or 0)
            continue
        is_model_call = record.get("event_type", "model_call") == "model_call"
        kind = str(record.get("call_kind") or "unknown")
        kind_bucket = by_kind.setdefault(kind, _bucket())
        if is_model_call:
            kind_bucket["calls"] += 1
            if record.get("status") == "ok":
                kind_bucket["ok"] += 1
            elif record.get("status") == "failed":
                kind_bucket["failed"] += 1
            if record.get("cache_hit"):
                kind_bucket["cache_hits"] += 1
            if isinstance(record.get("latency_ms"), (int, float)):
                kind_bucket["latencies"].append(record["latency_ms"])
            tokens = record.get("total_tokens")
            if isinstance(tokens, int):
                kind_bucket["tokens"] += tokens
            else:
                kind_bucket["tokens_unknown"] += 1
        for key, bucket in (
            ("stage", by_stage), ("model", by_model), ("template", by_template),
        ):
            value = str(record.get(key) or "unknown")
            item = bucket.setdefault(value, _bucket())
            if is_model_call:
                item["calls"] += 1
                if record.get("status") == "ok":
                    item["ok"] += 1
                elif record.get("status") == "failed":
                    item["failed"] += 1
                if record.get("cache_hit"):
                    item["cache_hits"] += 1
                if isinstance(record.get("latency_ms"), (int, float)):
                    item["latencies"].append(record["latency_ms"])
                tokens = record.get("total_tokens")
                if isinstance(tokens, int):
                    item["tokens"] += tokens
                else:
                    item["tokens_unknown"] += 1
            if record.get("event_type") == "validation_retry":
                item["retries"] += 1

    def _finalize(bucket: Dict[str, Any]) -> Dict[str, Any]:
        latencies = sorted(bucket.pop("latencies", []))
        def _pct(p: float) -> Optional[float]:
            if not latencies:
                return None
            index = min(len(latencies) - 1, max(0, int(round((len(latencies) - 1) * p))))
            return latencies[index]
        bucket["p50_ms"] = _pct(0.5)
        bucket["p95_ms"] = _pct(0.95)
        return bucket

    return {
        "by_stage": {key: _finalize(value) for key, value in by_stage.items()},
        "by_model": {key: _finalize(value) for key, value in by_model.items()},
        "by_template": {key: _finalize(value) for key, value in by_template.items()},
        "by_kind": {key: _finalize(value) for key, value in by_kind.items()},
        "dropped_events": dropped,
    }
