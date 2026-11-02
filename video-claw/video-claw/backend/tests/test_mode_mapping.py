"""两层模式映射回归：H3 resolver ↔ 传输层 task（WF-07 的映射断言）。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

from core.agents.prompt_rewrite_agent import resolve_h3_mode
from models.custom_video import CustomVideoClient


# ─────────────── 第一层：H3 resolver ───────────────


def test_resolve_h3_mode_reference_wins_even_without_selection():
    assert resolve_h3_mode("reference", has_reference_set=False) == "Ref2VA"
    assert resolve_h3_mode("reference", has_reference_set=True) == "Ref2VA"


def test_resolve_h3_mode_frame_matrix():
    assert resolve_h3_mode("start_end_frame", True, True) == "FL2VA"
    assert resolve_h3_mode("start_end_frame", False, True) == "L2VA"
    assert resolve_h3_mode("start_end_frame", True, False) == "I2VA"
    assert resolve_h3_mode("first_frame", True, False) == "I2VA"
    assert resolve_h3_mode("first_frame", False, False) == "T2VA"


# ─────────────── 第二层：适配层 task 与 partition 收敛 ───────────────


def _client(tasks=None) -> CustomVideoClient:
    meta = {
        "id": "m1",
        "protocol": "vllm-omni",
        "base_url": "http://127.0.0.1:1/v1",
        "capabilities": {"tasks": tasks} if tasks is not None else {},
    }
    return CustomVideoClient(meta, poll_interval=0.01, poll_timeout=1, timeout=1)


def test_task_mapping_by_input():
    client = _client()
    assert client._task_for_input(None, None, []) == "t2va"
    assert client._task_for_input("first.png", None, []) == "fl2va"
    assert client._task_for_input(None, "last.png", []) == "fl2va"
    assert client._task_for_input(None, None, ["ref.png"]) == "ref2va"


def test_undeclared_tasks_leave_inputs_unchanged():
    client = _client()
    assert client._align_task_to_partitions("f.png", "l.png", ["r.png"], None) == (
        "f.png", "l.png", ["r.png"],
    )


def test_fl2va_degrades_to_ref2va_and_keeps_frame_order():
    client = _client(tasks=["ref2va"])
    image, last, refs = client._align_task_to_partitions("first.png", "last.png", ["r.png"], None)

    # 语义断言：帧图不再走首/尾帧字段，而是按原顺序并入参考图前部
    assert (image, last) == (None, None)
    assert refs == ["first.png", "last.png", "r.png"]
    assert any("ref2va" in item for item in client._adjustments), "降级必须留下可见 adjustment"


def test_ref2va_merges_frames_when_already_ref_task():
    client = _client(tasks=["ref2va"])
    image, last, refs = client._align_task_to_partitions("first.png", None, ["r.png"], None)

    assert (image, last) == (None, None)
    assert refs == ["first.png", "r.png"]
    assert any("并入参考图" in item for item in client._adjustments)


def test_media_less_request_on_fl2va_only_instance_fails_loudly():
    client = _client(tasks=["fl2va"])
    try:
        client._align_task_to_partitions(None, None, None, None)
    except ValueError as exc:
        assert "不匹配" in str(exc)
    else:  # pragma: no cover - 失败必须抛出
        raise AssertionError("含图任务不支持时应明确失败，不能静默去掉输入")
