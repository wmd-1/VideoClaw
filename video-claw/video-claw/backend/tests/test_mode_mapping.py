"""两层模式映射回归：H3 resolver ↔ 传输层 task（WF-07 的映射断言）。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

import pytest

from core.agents.prompt_rewrite_agent import resolve_h3_mode
from models.custom_common import (
    TaskDeclarationError,
    TaskNotSupportedError,
    frame_as_reference_enabled,
    parse_task_decl,
)
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


def _client(tasks=None, allow_frame_as_reference=None) -> CustomVideoClient:
    caps: dict = {}
    if tasks is not None:
        caps["tasks"] = tasks
    if allow_frame_as_reference is not None:
        caps["allow_frame_as_reference"] = allow_frame_as_reference
    meta = {
        "id": "m1",
        "protocol": "vllm-omni",
        "base_url": "http://127.0.0.1:1/v1",
        "capabilities": caps,
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
    assert client._adjustments == []


def test_fl2va_default_fails_on_ref2va_only_instance():
    """默认配置：首/尾帧请求撞上 ref2va-only 实例 MUST 明确失败，不得静默降级（tasks 1.1）。"""
    client = _client(tasks=["ref2va"])
    with pytest.raises(TaskNotSupportedError) as excinfo:
        client._align_task_to_partitions("first.png", "last.png", None, None)
    message = str(excinfo.value)
    assert "任务类型与服务实例不匹配" in message
    assert "fl2va" in message and "ref2va" in message
    # 三条可操作路径必须在错误里可见
    assert "capabilities.tasks" in message
    assert "allow_frame_as_reference" in message
    assert "其他视频模型" in message
    # 默认配置下不得产生任何降级记录（tasks 1.4）
    assert client._adjustments == []


def test_single_frame_default_fails_on_ref2va_only_instance():
    client = _client(tasks=["ref2va"])
    with pytest.raises(TaskNotSupportedError):
        client._align_task_to_partitions("first.png", None, None, None)
    assert client._adjustments == []


def test_authorized_degrade_merges_frames_in_order():
    """显式授权后保留原降级行为：帧图并入参考图、图在前、保持顺序、记录调整（tasks 1.2）。"""
    client = _client(tasks=["ref2va"], allow_frame_as_reference=True)
    image, last, refs = client._align_task_to_partitions("first.png", "last.png", None, None)

    assert (image, last) == (None, None)
    assert refs == ["first.png", "last.png"]
    assert any("task fl2va -> ref2va" in item for item in client._adjustments)


def test_authorized_degrade_requires_ref2va_in_declared_set():
    client = _client(tasks=["t2va"], allow_frame_as_reference=True)
    with pytest.raises(TaskNotSupportedError):
        client._align_task_to_partitions("first.png", None, None, None)


def test_authorized_degrade_does_not_fire_when_task_already_supported():
    """授权只是降级通道：task 本就命中声明集合时不做任何改写，也不记录调整。"""
    client = _client(tasks=["fl2va"], allow_frame_as_reference=True)
    assert client._align_task_to_partitions("first.png", "last.png", None, None) == (
        "first.png", "last.png", None,
    )
    assert client._adjustments == []


def test_ref2va_merges_frames_when_already_ref_task():
    """task 本就是 ref2va 时的帧图合并不受影响（tasks 1.3）。"""
    client = _client(tasks=["ref2va"])
    image, last, refs = client._align_task_to_partitions("first.png", None, ["r.png"], None)

    assert (image, last) == (None, None)
    assert refs == ["first.png", "r.png"]
    assert any("并入参考图" in item for item in client._adjustments)
    # 这不是降级：不得产生 task -> ref2va 的降级记录
    assert not any("task fl2va -> ref2va" in item for item in client._adjustments)


def test_declared_set_covering_task_keeps_layout():
    client = _client(tasks=["t2va", "fl2va", "ref2va"])
    assert client._align_task_to_partitions("f.png", "l.png", None, None) == (
        "f.png", "l.png", None,
    )
    assert client._adjustments == []


def test_media_less_request_on_fl2va_only_instance_fails_loudly():
    client = _client(tasks=["fl2va"])
    try:
        client._align_task_to_partitions(None, None, None, None)
    except TaskNotSupportedError as exc:
        assert "不匹配" in str(exc)
    else:  # pragma: no cover - 失败必须抛出
        raise AssertionError("含图任务不支持时应明确失败，不能静默去掉输入")


def test_reference_video_only_request_on_fl2va_instance_fails():
    """has_media 不只图片：仅含参考视频的 ref2va 请求同样不得静默放行。"""
    client = _client(tasks=["fl2va"])
    with pytest.raises(TaskNotSupportedError):
        client._align_task_to_partitions(None, None, None, ["ref.mp4"])


# ─────────────── 三态解析（tasks 1.5） ───────────────


def test_parse_task_decl_three_states():
    assert parse_task_decl({}) == ("missing", [])
    assert parse_task_decl({"tasks": None}) == ("missing", [])
    assert parse_task_decl({"tasks": ["foo", "bar"]}) == ("invalid", [])
    assert parse_task_decl({"tasks": ""}) == ("invalid", [])
    assert parse_task_decl({"tasks": "ref2va, FL2VA, ref2va"}) == ("declared", ["ref2va", "fl2va"])
    # 部分非法：保留合法项（非法项被忽略），不整条判为配置错误
    assert parse_task_decl({"tasks": ["ref2va", "bogus"]}) == ("declared", ["ref2va"])


def test_frame_as_reference_requires_explicit_true(caplog):
    import logging

    assert frame_as_reference_enabled({}) is False
    assert frame_as_reference_enabled({"allow_frame_as_reference": False}) is False
    assert frame_as_reference_enabled({"allow_frame_as_reference": True}) is True
    # 非布尔声明视为 false，但必须留下告警（不得静默不生效）
    with caplog.at_level(logging.WARNING):
        assert frame_as_reference_enabled({"allow_frame_as_reference": "true"}) is False
    assert any("allow_frame_as_reference" in record.getMessage() for record in caplog.records)


def test_tasks_declared_but_invalid_is_config_error():
    """字段存在但全非法 MUST 视为配置错误，不得退化为未声明静默放行（tasks 1.5）。"""
    client = _client(tasks=["foo", "bar"])
    with pytest.raises(TaskDeclarationError) as excinfo:
        client._align_task_to_partitions("first.png", None, None, None)
    message = str(excinfo.value)
    assert "tasks" in message
    assert "t2va / fl2va / ref2va" in message

    with pytest.raises(TaskDeclarationError):
        client._supported_tasks()
