"""实例忙闲解析：vLLM 作业/分页与 SGLang diffusion 指标。"""
from __future__ import annotations

import json

from core.video_instance_probe import (
    ProbeScheduler,
    observe_sglang_metrics,
    parse_sglang_diffusion_metrics,
    parse_vllm_job_response,
    scan_vllm_jobs,
)

FIXED_NOW = "2026-11-20T12:00:00+00:00"


def _page(status: int, payload):
    return lambda _after: (status, payload)


def test_vllm_single_job_non_2xx_structured_failed_is_still_terminal():
    job = parse_vllm_job_response(
        500,
        {"id": "video_1", "status": "failed", "prompt": "secret prompt", "error": {"code": "x"}},
    )
    assert job.state == "failed"
    assert job.terminal is True
    assert job.job_id == "video_1"

    unknown = parse_vllm_job_response(404, {"detail": "not found"})
    assert unknown.state == "unknown"
    assert unknown.terminal is False


def test_vllm_scan_complete_busy_and_strips_prompt():
    pages = {
        None: (200, {
            "data": [
                {"id": "a", "status": "completed", "prompt": "p1"},
                {"id": "b", "status": "in_progress", "prompt": "p2"},
            ],
            "has_more": True,
            "last_id": "b",
        }),
        "b": (200, {
            "data": [{"id": "c", "status": "completed", "prompt": "p3"}],
            "has_more": False,
            "last_id": "c",
        }),
    }
    observation = scan_vllm_jobs(lambda after: pages[after], observed_at=FIXED_NOW)
    assert observation.state == "busy"
    assert observation.coverage == "complete"
    assert observation.observed_at == FIXED_NOW
    assert observation.detail["jobs"] == 3
    assert observation.detail["busy"] == 1
    assert observation.detail["terminal"] == 2
    assert "prompt" not in json.dumps(observation.detail, ensure_ascii=False)


def test_vllm_scan_missing_cursor_and_page_budget_are_unknown():
    missing = scan_vllm_jobs(
        _page(200, {"data": [{"id": "a", "status": "queued"}], "has_more": True, "last_id": None})
    )
    assert missing.state == "unknown"
    assert missing.coverage == "partial"
    assert missing.detail["reason"] == "missing_cursor"

    budget = scan_vllm_jobs(
        _page(200, {"data": [{"id": "a", "status": "queued"}], "has_more": True, "last_id": "a"}),
        max_pages=1,
    )
    assert budget.state == "unknown"
    assert budget.coverage == "partial"
    assert budget.detail["reason"] == "page_budget"


def test_vllm_scan_unknown_status_is_not_idle():
    observation = scan_vllm_jobs(
        _page(200, {"data": [{"id": "a", "status": "paused"}], "has_more": False, "last_id": "a"})
    )
    assert observation.state == "unknown"
    assert observation.coverage == "complete"


def test_vllm_scan_all_terminal_is_idle():
    observation = scan_vllm_jobs(
        _page(200, {
            "data": [{"id": "a", "status": "completed"}, {"id": "b", "status": "failed"}],
            "has_more": False,
            "last_id": "b",
        })
    )
    assert observation.state == "idle"
    assert observation.detail["terminal"] == 2


def test_sglang_metrics_parse_scientific_and_escaped_labels():
    text = (
        '# HELP sglang:diffusion_num_running_reqs running\n'
        'sglang:diffusion_num_running_reqs{role="monolithic",replica="a\\"b"} 1.2e+03\n'
        'sglang:diffusion_num_queue_reqs{role="monolithic",replica="a\\"b"} 0\n'
    )
    metrics = parse_sglang_diffusion_metrics(text, role="monolithic", replica='a"b')
    assert metrics == {"running": 1200.0, "queued": 0.0}


def test_sglang_metrics_missing_nan_negative_duplicate_and_html_are_unknown():
    base_running = 'sglang:diffusion_num_running_reqs{role="monolithic",replica="0"} 1\n'
    base_queue = 'sglang:diffusion_num_queue_reqs{role="monolithic",replica="0"} 0\n'
    assert parse_sglang_diffusion_metrics(base_running, role="monolithic", replica="0") is None
    assert parse_sglang_diffusion_metrics(
        base_running + base_queue.replace(" 0\n", " NaN\n"), role="monolithic", replica="0"
    ) is None
    assert parse_sglang_diffusion_metrics(
        base_running + base_queue.replace(" 0\n", " -1\n"), role="monolithic", replica="0"
    ) is None
    assert parse_sglang_diffusion_metrics(
        base_running + base_queue + base_queue, role="monolithic", replica="0"
    ) is None
    assert parse_sglang_diffusion_metrics("<html>404</html>", role="monolithic", replica="0") is None


def test_sglang_metrics_label_mismatch_is_unknown():
    text = (
        'sglang:diffusion_num_running_reqs{role="other",replica="0"} 0\n'
        'sglang:diffusion_num_queue_reqs{role="other",replica="0"} 0\n'
    )
    assert parse_sglang_diffusion_metrics(text, role="monolithic", replica="0") is None


def test_probe_scheduler_cadence_singleflight_and_budget():
    scheduler = ProbeScheduler()
    ticket = scheduler.try_begin("a", now=0, waiting=True)
    assert ticket is not None
    assert ticket.deadline == 3.0
    assert scheduler.try_begin("a", now=0, waiting=True) is None  # singleflight
    scheduler.finish(ticket, success=True, now=0)
    assert scheduler.try_begin("a", now=4.9, waiting=True) is None
    assert scheduler.try_begin("a", now=5, waiting=True) is not None

    idle = ProbeScheduler()
    ticket = idle.try_begin("b", now=0, waiting=False)
    idle.finish(ticket, success=True, now=0)
    assert idle.try_begin("b", now=29.9, waiting=False) is None
    assert idle.try_begin("b", now=30, waiting=False) is not None


def test_probe_scheduler_on_demand_does_not_wait_full_idle_interval():
    scheduler = ProbeScheduler()
    ticket = scheduler.try_begin("a", now=0, waiting=False)
    scheduler.finish(ticket, success=True, now=0)
    assert scheduler.try_begin("a", now=10, waiting=False, on_demand=True) is not None


def test_probe_scheduler_backoff_allows_one_early_check_only():
    scheduler = ProbeScheduler()
    ticket = scheduler.try_begin("a", now=0, waiting=True)
    scheduler.finish(ticket, success=False, now=0)  # next allowed 15

    early = scheduler.try_begin("a", now=6, waiting=False, on_demand=True)
    assert early is not None
    scheduler.finish(early, success=False, now=6)  # next allowed 36, override consumed

    assert scheduler.try_begin("a", now=11, waiting=False, on_demand=True) is None
    periodic = scheduler.try_begin("a", now=36, waiting=True)
    assert periodic is not None
    scheduler.finish(periodic, success=False, now=36)  # failures=3 -> backoff 60
    assert scheduler.try_begin("a", now=95.9, waiting=True) is None
    assert scheduler.try_begin("a", now=96, waiting=True) is not None


def test_probe_scheduler_does_not_reuse_same_observation_timestamp():
    scheduler = ProbeScheduler()
    assert scheduler.record_observation("a", "idle", 10) is True
    assert scheduler.record_observation("a", "idle", 10) is False
    assert len(scheduler.recent_observations("a", now=10)) == 1
    assert scheduler.record_observation("a", "idle", 11) is True
    assert len(scheduler.recent_observations("a", now=11)) == 2


def test_observe_sglang_metrics_distinguishes_busy_idle_and_unreachable():
    text = (
        'sglang:diffusion_num_running_reqs{role="monolithic",replica="0"} 1\n'
        'sglang:diffusion_num_queue_reqs{role="monolithic",replica="0"} 2\n'
    )
    busy = observe_sglang_metrics(200, text, role="monolithic", replica="0", observed_at=FIXED_NOW)
    assert busy.state == "busy"
    assert busy.detail == {"running": 1, "queued": 2}

    idle = observe_sglang_metrics(
        200,
        text.replace(" 1\n", " 0\n").replace(" 2\n", " 0\n"),
        role="monolithic",
        replica="0",
    )
    assert idle.state == "idle"

    unreachable = observe_sglang_metrics(404, "", role="monolithic", replica="0")
    assert unreachable.state == "unreachable"
    assert unreachable.coverage == "none"

    malformed = observe_sglang_metrics(200, "<html>bad</html>", role="monolithic", replica="0")
    assert malformed.state == "unknown"
