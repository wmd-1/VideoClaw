"""视频任务能力预检回归（openspec: video-parameter-mapping「视频任务能力预检」）。

覆盖：模式 × 声明集合交叉矩阵、先授权后交集、tasks 全非法、未声明放行，
以及两个实际调用点（阶段执行 / 介入重跑）在启动生成前的 409 行为。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routers.workflow import _preflight_stage_models

MODEL_ID = "h3-test-video"
VIDEO_FIELD = {
    "first_frame": "video_first_frame_model",
    "start_end_frame": "video_start_end_model",
    "reference": "video_reference_model",
}
BASE_MODELS = {
    "llm_model": "test-llm",
    "vlm_model": "test-vlm",
    "image_t2i_model": "test-t2i",
    "image_it2i_model": "test-i2i",
}


def configure_model(monkeypatch, capabilities):
    """把测试视频模型写进有效配置（provider 完整，可用性预检可过）。"""
    from config import Config

    entry = {
        "id": MODEL_ID,
        "provider": "local_video",
        "model": MODEL_ID,
        "types": ["video"],
        "abilities": ["text_to_video", "first_frame_i2v", "start_end_frame_i2v", "reference_to_video"],
    }
    if capabilities is not None:
        entry["capabilities"] = capabilities
    monkeypatch.setattr(
        Config,
        "CONFIG",
        {
            "api_providers": {
                "local_video": {"protocol": "vllm-omni", "base_url": "http://127.0.0.1:9/v1"},
            },
            "custom_models": [entry],
        },
        raising=False,
    )


def video_values(mode: str) -> dict:
    return {"video_generation_mode": mode, VIDEO_FIELD[mode]: MODEL_ID}


# mode, capabilities, 期望 error_code（None = 放行）
PREFLIGHT_MATRIX = [
    # reference：主流程唯一可能 task 为 ref2va
    ("reference", {"tasks": ["ref2va"]}, None),
    ("reference", {"tasks": ["ref2va", "fl2va"]}, None),
    ("reference", {"tasks": ["fl2va"]}, "task_not_supported"),
    ("reference", {"tasks": ["t2va"]}, "task_not_supported"),
    # first_frame：主流程只下发 fl2va（缺首帧图时前置失败，不会走 t2va）
    ("first_frame", {"tasks": ["fl2va"]}, None),
    ("first_frame", {"tasks": ["t2va", "fl2va"]}, None),
    ("first_frame", {"tasks": ["ref2va"]}, "task_not_supported"),
    ("first_frame", {"tasks": ["ref2va"], "allow_frame_as_reference": True}, None),
    ("first_frame", {"tasks": ["t2va"]}, "task_not_supported"),
    ("first_frame", {"tasks": ["t2va"], "allow_frame_as_reference": True}, "task_not_supported"),
    # 授权优先于交集：['t2va','ref2va'] + 授权必须放行
    ("first_frame", {"tasks": ["t2va", "ref2va"], "allow_frame_as_reference": True}, None),
    ("first_frame", {"tasks": ["t2va", "ref2va"], "allow_frame_as_reference": False}, "task_not_supported"),
    # start_end_frame
    ("start_end_frame", {"tasks": ["fl2va"]}, None),
    ("start_end_frame", {"tasks": ["ref2va"]}, "task_not_supported"),
    ("start_end_frame", {"tasks": ["ref2va"], "allow_frame_as_reference": True}, None),
    ("start_end_frame", {"tasks": ["t2va"]}, "task_not_supported"),
    # 声明存在但全部非法：独立 error_code，不得放行
    ("first_frame", {"tasks": ["foo", "bar"]}, "tasks_invalid"),
    ("reference", {"tasks": ["foo", "bar"]}, "tasks_invalid"),
    ("first_frame", {"tasks": ["foo", "bar"], "allow_frame_as_reference": True}, "tasks_invalid"),
    # 未声明 tasks：完全放行（行为与变更前一致）
    ("first_frame", None, None),
    ("reference", None, None),
    ("start_end_frame", None, None),
]


@pytest.mark.parametrize("mode,capabilities,expected", PREFLIGHT_MATRIX)
def test_video_task_preflight_matrix(monkeypatch, mode, capabilities, expected):
    """三种模式 × 声明集合的交叉矩阵，含授权优先与全非法组合（tasks 2.1）。"""
    configure_model(monkeypatch, capabilities)
    values = video_values(mode)
    if expected is None:
        assert _preflight_stage_models("video_generation", values) is None
        return
    with pytest.raises(HTTPException) as excinfo:
        _preflight_stage_models("video_generation", values)
    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert detail["code"] == "model_unavailable"
    assert detail["error_code"] == expected
    assert detail["stage"] == "video_generation"
    assert detail["field"] == VIDEO_FIELD[mode]
    assert detail["model"] == MODEL_ID
    assert detail["reason"]


def test_task_not_supported_reason_lists_actionable_paths(monkeypatch):
    configure_model(monkeypatch, {"tasks": ["ref2va"]})
    with pytest.raises(HTTPException) as excinfo:
        _preflight_stage_models("video_generation", video_values("first_frame"))
    reason = excinfo.value.detail["reason"]
    assert "ref2va" in reason and "fl2va" in reason
    assert "capabilities.tasks" in reason
    assert "allow_frame_as_reference" in reason
    assert "生成方式" in reason


def test_tasks_invalid_lists_legal_values(monkeypatch):
    configure_model(monkeypatch, {"tasks": ["foo"]})
    with pytest.raises(HTTPException) as excinfo:
        _preflight_stage_models("video_generation", video_values("first_frame"))
    reason = excinfo.value.detail["reason"]
    assert "t2va / fl2va / ref2va" in reason


def test_preflight_ignores_non_video_stages(monkeypatch):
    """任务能力预检只作用于视频阶段：其它阶段不读取 tasks 声明。"""
    configure_model(monkeypatch, {"tasks": ["t2va"]})
    # 没有阶段字段的阶段直接返回，不受视频模型的 tasks 声明影响
    assert _preflight_stage_models("post_production", {}) is None


def test_first_frame_with_t2va_only_is_rejected_before_missing_image(monkeypatch):
    """tasks 2.3：任务不匹配在阶段启动前拒绝；与“缺首帧图”是两件不同的事。"""
    from core.agents.video_agent import VideoDirectorAgent

    configure_model(monkeypatch, {"tasks": ["t2va"]})
    with pytest.raises(HTTPException) as excinfo:
        _preflight_stage_models("video_generation", video_values("first_frame"))
    assert excinfo.value.detail["error_code"] == "task_not_supported"

    # 缺图是运行期片段级失败，与任务声明无关：同一模式下不声明 tasks 时预检放行
    configure_model(monkeypatch, None)
    assert _preflight_stage_models("video_generation", video_values("first_frame")) is None
    result = VideoDirectorAgent()._generate_one(
        "sid", "seg1", "prompt", None, MODEL_ID, video_generation_mode="first_frame"
    )
    assert result[1] is None
    assert result[3]["error_type"] == "missing_input_image"


def _headers(login: str = "admin") -> dict:
    return {"X-User-Login-Name": login}


def _start_session(client) -> str:
    response = client.post(
        "/api/project/start",
        headers=_headers(),
        json={
            "idea": "视频任务能力预检",
            "video_first_frame_model": MODEL_ID,
            "video_generation_mode": "first_frame",
            **BASE_MODELS,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def test_execute_returns_409_and_skips_inference(client, monkeypatch):
    """tasks 2.2：阶段执行入口在预检阶段返回 409，不调用推理服务。"""
    from models.custom_video import CustomVideoClient

    configure_model(monkeypatch, {"tasks": ["ref2va"]})
    invoked: list = []
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, *args, **kwargs: invoked.append(1) or "",
        raising=True,
    )
    session_id = _start_session(client)

    response = client.post(
        f"/api/project/{session_id}/execute/video_generation", headers=_headers(), json={}
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "task_not_supported"
    assert detail["stage"] == "video_generation"
    assert detail["field"] == "video_first_frame_model"
    assert invoked == [], "预检失败 MUST NOT 启动任何生成"


def test_intervene_returns_409_and_skips_inference(client, monkeypatch):
    """tasks 2.2：介入重跑入口同样在预检阶段返回 409。"""
    from models.custom_video import CustomVideoClient

    configure_model(monkeypatch, {"tasks": ["ref2va"]})
    invoked: list = []
    monkeypatch.setattr(
        CustomVideoClient,
        "generate_video",
        lambda self, *args, **kwargs: invoked.append(1) or "",
        raising=True,
    )
    session_id = _start_session(client)

    response = client.post(
        f"/api/project/{session_id}/intervene",
        headers=_headers(),
        json={"stage": "video_generation", "modifications": {}},
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "task_not_supported"
    assert invoked == []


def test_authorized_degrade_passes_preflight(monkeypatch):
    """授权后预检放行：不再以“交集为空”拒绝，交由传输层按授权降级。"""
    configure_model(monkeypatch, {"tasks": ["ref2va"], "allow_frame_as_reference": True})
    assert _preflight_stage_models("video_generation", video_values("first_frame")) is None
    assert _preflight_stage_models("video_generation", video_values("start_end_frame")) is None


# ─────────────── 连通测试的能力视图（tasks 4.2 / 4.3） ───────────────


def _test_request(capabilities):
    from api.routers.models import ModelTestRequest

    model = {"id": MODEL_ID, "model": MODEL_ID, "abilities": []}
    if capabilities is not None:
        model["capabilities"] = capabilities
    return ModelTestRequest(
        model=model,
        provider={
            "key": "local_video",
            "protocol": "vllm-omni",
            "base_url": "http://127.0.0.1:9/v1",
        },
        model_type="video",
    )


def test_build_meta_passes_task_declarations_and_keeps_short_duration():
    """请求体中的 tasks 透传到测试视图，且 duration 被测试用 min=1 覆盖（4.2）。"""
    from api.routers.models import _build_meta
    from models.custom_video import CustomVideoClient

    meta = _build_meta(
        _test_request({"tasks": ["ref2va"], "duration": {"min": 4, "max": 15}})
    )
    assert meta["capabilities"]["tasks"] == ["ref2va"]
    assert meta["capabilities"]["duration"]["min"] == 1
    # 测试用 1 秒请求不会被模型声明的 min=4 夹取
    assert CustomVideoClient(meta, poll_interval=0.01, poll_timeout=1, timeout=1)._clamp_duration(1) == 1


def test_build_meta_falls_back_to_registry_capabilities(monkeypatch):
    """请求体未带 capabilities 时回退注册表条目（已保存模型）。"""
    from api.routers.models import _build_meta

    configure_model(monkeypatch, {"tasks": ["ref2va"], "allow_frame_as_reference": True})
    meta = _build_meta(_test_request(None))
    assert meta["capabilities"]["tasks"] == ["ref2va"]
    assert meta["capabilities"]["allow_frame_as_reference"] is True


def test_build_meta_falls_back_per_key_when_partial_capabilities(monkeypatch):
    """请求体只带无关能力（如 fast_h3）时，tasks 仍按缺失的键从注册表补齐（审核 3a）。"""
    from api.routers.models import _build_meta

    configure_model(monkeypatch, {"tasks": ["ref2va"], "allow_frame_as_reference": True})
    meta = _build_meta(_test_request({"fast_h3": True}))
    assert meta["capabilities"]["tasks"] == ["ref2va"]
    assert meta["capabilities"]["allow_frame_as_reference"] is True
    assert meta["capabilities"]["duration"]["min"] == 1


def test_build_meta_keeps_explicit_task_declaration(monkeypatch):
    """请求体显式声明了 tasks 时不回退：非法声明必须暴露，不能被注册表值掩盖。"""
    from api.routers.models import _build_meta

    configure_model(monkeypatch, {"tasks": ["ref2va"]})
    meta = _build_meta(_test_request({"tasks": ["foo"]}))
    assert meta["capabilities"]["tasks"] == ["foo"]


def test_build_meta_without_any_capabilities_keeps_test_duration():
    from api.routers.models import _build_meta

    meta = _build_meta(_test_request(None))
    assert "tasks" not in meta["capabilities"]
    assert meta["capabilities"]["duration"]["min"] == 1


def test_video_test_reports_task_mismatch_not_service_failure(monkeypatch):
    """ref2va-only 实例的连通测试给出“不支持纯文生视频”，不得被当成服务故障（4.3）。"""
    import pytest as _pytest

    from api.routers.models import _build_meta, _test_video
    from models.custom_common import TaskDeclarationError, TaskNotSupportedError

    def boom_not_supported(self, *args, **kwargs):
        raise TaskNotSupportedError("任务类型与服务实例不匹配")

    meta = _build_meta(_test_request({"tasks": ["ref2va"]}))
    monkeypatch.setattr(
        "models.custom_video.CustomVideoClient.generate_video", boom_not_supported, raising=True
    )
    with _pytest.raises(RuntimeError) as excinfo:
        _test_video(meta)
    assert "该实例不支持纯文生视频" in str(excinfo.value)

    def boom_invalid(self, *args, **kwargs):
        raise TaskDeclarationError("tasks 声明无效")

    monkeypatch.setattr(
        "models.custom_video.CustomVideoClient.generate_video", boom_invalid, raising=True
    )
    with _pytest.raises(RuntimeError) as excinfo:
        _test_video(meta)
    assert "任务能力声明无效" in str(excinfo.value)
