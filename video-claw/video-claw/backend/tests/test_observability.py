"""D1 观测：字段、隐私、聚合、写盘降级与客户端埋点。"""
from __future__ import annotations

import asyncio
import contextvars
import glob
import json
import re
from concurrent.futures import ThreadPoolExecutor

from core import observability as obs


def _read(directory: str) -> list:
    records = []
    for path in glob.glob(f"{directory}/calls-*.jsonl"):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    records.append(json.loads(line))
    return records


def test_usage_known_and_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    assert obs.record_call(call_kind="text", model="m", stage="script_generation")
    assert obs.record_call(
        call_kind="text", model="m", stage="script_generation",
        input_tokens=10, output_tokens=20, total_tokens=30,
    )
    records = _read(str(tmp_path))
    unknown, known = records[0], records[1]
    assert unknown["total_tokens"] is None
    assert unknown["usage_source"] == "unknown"
    assert known["total_tokens"] == 30
    assert known["usage_source"] == "provider"


def test_summary_separates_kinds_and_retries(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    obs.record_call(call_kind="text", model="m", stage="s", latency_ms=100)
    obs.record_call(call_kind="text", model="m", stage="s", cache_hit=True, latency_ms=10)
    obs.record_call(call_kind="vlm", model="m", stage="s", status="failed", latency_ms=300)
    obs.record_call(call_kind="image", model="m", stage="s", latency_ms=200)
    obs.record_call(call_kind="video", model="m", stage="s", latency_ms=400)
    obs.record_call(
        event_type="validation_retry", call_kind="text", model="m",
        stage="s", retry_reason="validation", validation_retries=1,
    )
    summary = obs.summarize_records(_read(str(tmp_path)))
    assert summary["by_kind"]["text"]["calls"] == 2
    assert summary["by_kind"]["text"]["cache_hits"] == 1
    assert summary["by_kind"]["vlm"]["calls"] == 1
    assert summary["by_kind"]["image"]["calls"] == 1
    assert summary["by_kind"]["video"]["calls"] == 1
    stage = summary["by_stage"]["s"]
    assert stage["calls"] == 5
    assert stage["retries"] == 1
    assert stage["cache_hits"] == 1
    assert stage["failed"] == 1
    assert stage["p50_ms"] is not None and stage["p95_ms"] is not None


def test_error_is_sanitized(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    obs.record_call(
        call_kind="text", model="m", status="failed",
        error="GET https://example.com/a?signature=abc&token=secret failed",
    )
    record = _read(str(tmp_path))[0]
    assert record["error_type"] == ""
    assert "https://" not in record["error"]
    assert "secret" not in record["error"]
    assert "<url>" in record["error"]


def test_bearer_and_prompt_body_are_redacted(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    obs.record_call(
        call_kind="text", model="m", status="failed",
        error='Authorization: Bearer abc.def.ghi prompt: 用户正文片段 "这里是一段很长的用户正文内容，不应该出现在观测里，长度超过阈值"',
    )
    record = _read(str(tmp_path))[0]
    for secret in ("abc.def.ghi", "用户正文片段", "这里是一段很长的用户正文"):
        assert secret not in record["error"]
    assert "<redacted>" in record["error"]


def test_basic_and_json_body_are_redacted(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    obs.record_call(
        call_kind="text", model="m", status="failed",
        error='Authorization: Basic dXNlcjpwYXNz {"prompt": "short body"}',
    )
    record = _read(str(tmp_path))[0]
    assert "dXNlcjpwYXNz" not in record["error"]
    assert "short body" not in record["error"]


def test_truncated_record_keeps_core_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    monkeypatch.setenv("VC_OBSERVABILITY_MAX_BYTES", "1000000")
    obs.record_call(
        call_kind="text", model="m", stage="script_generation", session_id="sid",
        execution_id="e1", template_version="x" * 5000,
    )
    record = _read(str(tmp_path))[0]
    assert record["truncated"] is True
    assert record["stage"] == "script_generation"
    assert record["session_id"] == "sid"
    assert record["call_kind"] == "text"


def test_record_has_no_prompt_url_or_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    obs.record_call(
        call_kind="text", model="m", session_id="session-1",
        prompt_hash=obs.prompt_hash("full prompt"), error="token=secret",
    )
    record = _read(str(tmp_path))[0]
    for forbidden in ("prompt", "url", "owner", "api_key", "authorization"):
        assert forbidden not in record
    assert record["session_id"] == "session-1"
    assert record["prompt_hash"] == obs.prompt_hash("full prompt")


def test_write_failure_is_non_blocking(monkeypatch):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", "/proc/no-such-observability")
    assert obs.record_call(call_kind="text", model="m") is False


def test_line_limit_and_dropped_event(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    monkeypatch.setenv("VC_OBSERVABILITY_MAX_BYTES", "120")
    assert obs.record_call(call_kind="text", model="m", error="x" * 5000) is False
    dropped = glob.glob(f"{tmp_path}/dropped-*.jsonl")
    assert dropped
    text = "".join(open(path, encoding="utf-8").read() for path in dropped)
    assert "observability_dropped" in text


def test_llm_and_vlm_clients_record(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    from models.llm_client import LLM
    from models.vlm_client import VLM

    monkeypatch.setattr(LLM, "_query_impl", lambda *a, **k: "ok")
    monkeypatch.setattr(VLM, "_query_impl", lambda *a, **k: "desc")
    assert LLM().query("hello", model="deepseek-v4.1-flash", task_id="s1") == "ok"
    assert VLM().query("look", image_paths=["a.png"], model="qwen-vl", session_id="s1") == "desc"
    records = _read(str(tmp_path))
    assert {record["call_kind"] for record in records} == {"text", "vlm"}
    assert all(record["session_id"] == "s1" for record in records)
    assert all(record["input_chars"] is not None for record in records)


def test_image_and_video_clients_record(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    from models.image_client import ImageClient
    from models.video_client import VideoClient

    monkeypatch.setattr(ImageClient, "_generate_image_impl", lambda *a, **k: ["/tmp/a.png"])
    monkeypatch.setattr(VideoClient, "_generate_video_impl", lambda *a, **k: "https://example/video.mp4")
    assert ImageClient().generate_image(prompt="p", model="wan2.7-image", session_id="s2") == ["/tmp/a.png"]
    assert VideoClient().generate_video(prompt="v", model="wan2.7-i2v", duration=5) == "https://example/video.mp4"
    records = _read(str(tmp_path))
    kinds = {record["call_kind"] for record in records}
    assert {"image", "video"} <= kinds
    image = next(record for record in records if record["call_kind"] == "image")
    assert image["output_count"] == 1
    video = next(record for record in records if record["call_kind"] == "video")
    assert video["video_duration"] == 5


def test_call_context_enriches_without_cross_leak(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    with obs.call_context(
        stage="prompt_rewrite", session_id="s1", execution_id="e1",
        template_name="rewrite_zh", template_version="v1",
    ):
        obs.record_call(call_kind="text", model="m")
    assert obs.record_call(call_kind="text", model="m")
    first, second = _read(str(tmp_path))
    assert first["stage"] == "prompt_rewrite"
    assert first["session_id"] == "s1"
    assert first["execution_id"] == "e1"
    assert first["template_name"] == "rewrite_zh"
    assert second["stage"] == ""
    assert second["session_id"] == ""


def test_context_survives_to_thread_and_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path))
    with obs.call_context(stage="prompt_rewrite", session_id="sid-ctx", execution_id="e-ctx"):
        asyncio.run(asyncio.to_thread(obs.record_call, call_kind="text", model="m"))
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(
                contextvars.copy_context().run, obs.record_call,
                call_kind="vlm", model="m",
            ).result()
    records = _read(str(tmp_path))
    assert [record["call_kind"] for record in records] == ["text", "vlm"]
    assert all(record["stage"] == "prompt_rewrite" for record in records)
    assert all(record["session_id"] == "sid-ctx" for record in records)
    assert all(record["execution_id"] == "e-ctx" for record in records)


def test_project_has_no_run_in_executor_context_loss():
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    if not (backend / "core").is_dir():
        backend = Path("/app")
    roots = [backend / name for name in ("core", "models", "api", "pipelines")]
    offending = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if re.search(r"\.run_in_executor\s*\(", path.read_text(encoding="utf-8", errors="ignore")):
                offending.append(str(path))
    assert offending == []


def test_summary_input_chars_and_execution():
    records = [
        {"event_type": "model_call", "call_kind": "text", "stage": "s", "template": "t",
         "execution_id": "e1", "input_chars": 100, "status": "ok"},
        {"event_type": "model_call", "call_kind": "text", "stage": "s", "template": "t",
         "execution_id": "e1", "input_chars": 200, "status": "ok"},
        {"event_type": "model_call", "call_kind": "vlm", "stage": "s", "template": "t",
         "execution_id": "e2", "input_chars": 50, "status": "ok"},
    ]
    summary = obs.summarize_records(
        records, input_warn_chars=150, input_hard_chars=250,
        execution_warn_chars=250,
    )
    stage = summary["by_stage"]["s"]
    assert stage["input_chars_p50"] == 100
    assert stage["input_chars_p95"] == 200
    assert stage["input_chars_max"] == 200
    assert stage["input_over_warn"] == 1
    assert stage["input_over_hard"] == 0
    assert summary["by_execution"]["e1"] == {
        "calls": 2, "input_chars_sum": 300, "input_chars_max": 200,
        "over_warn": 1, "over_hard": 0,
        "total_over_warn": True, "total_over_hard": False,
    }
    assert summary["input_hard_enabled"] is True


def test_invalid_threshold_env_falls_back(monkeypatch):
    monkeypatch.setenv("VC_OBSERVABILITY_INPUT_WARN_CHARS", "80k")
    monkeypatch.setenv("VC_OBSERVABILITY_INPUT_HARD_CHARS", "bad")
    monkeypatch.setenv("VC_OBSERVABILITY_EXECUTION_WARN_CHARS", "x")
    monkeypatch.setenv("VC_OBSERVABILITY_EXECUTION_HARD_CHARS", "-")
    summary = obs.summarize_records([])
    assert summary["input_warn_chars"] == 80000
    assert summary["input_hard_chars"] == 0
    assert summary["execution_warn_chars"] == 300000
    assert summary["execution_hard_chars"] == 0
    assert summary["input_hard_enabled"] is False
    assert summary["execution_hard_enabled"] is False


def test_twenty_segment_execution_aggregation():
    records = []
    for index in range(1, 21):
        records.append({
            "event_type": "model_call", "call_kind": "text",
            "stage": "prompt_rewrite", "template": "rewrite_zh",
            "execution_id": "exec-20", "input_chars": index * 100,
        })
    summary = obs.summarize_records(
        records, input_warn_chars=1000, input_hard_chars=0,
        execution_warn_chars=15000,
    )
    execution = summary["by_execution"]["exec-20"]
    assert execution["calls"] == 20
    assert execution["input_chars_sum"] == 21000
    assert execution["input_chars_max"] == 2000
    assert execution["over_warn"] == 10
    assert execution["over_hard"] == 0
    assert execution["total_over_warn"] is True
    assert summary["by_stage"]["prompt_rewrite"]["input_chars_max"] == 2000


def test_zero_warn_disables_warnings_consistently():
    records = [{
        "event_type": "model_call", "call_kind": "text", "stage": "s",
        "execution_id": "e", "input_chars": 10,
    }]
    summary = obs.summarize_records(
        records, input_warn_chars=0, input_hard_chars=0,
        execution_warn_chars=-1, execution_hard_chars=0,
    )
    assert summary["input_warn_enabled"] is False
    assert summary["execution_warn_enabled"] is False
    assert summary["by_stage"]["s"]["input_over_warn"] == 0
    assert summary["by_execution"]["e"]["over_warn"] == 0
    assert summary["by_execution"]["e"]["total_over_warn"] is False
