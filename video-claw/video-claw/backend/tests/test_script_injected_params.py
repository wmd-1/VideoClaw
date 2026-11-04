"""剧本产物与会话注入参数的键名冲突：`episodes` 分集正文不得被剔除。

`episodes` 同时是会话参数（集数，int）和剧本产物字段（分集正文，list）。
首次执行时旧产物为空，若无类型判别，编排器会把分集正文当注入参数删掉，
前端剧本阶段就只剩角色与场景。
"""
from __future__ import annotations

from core.orchestrator import WorkflowEngine


def _payload(**overrides):
    payload = {
        "session_id": "s1",
        "title": "测试剧本",
        "characters": [{"name": "小李"}],
        "settings": [{"name": "大厅"}],
        "episodes": [{"episode_number": 1, "act_title": "第1集", "content": "正文"}],
    }
    payload.update(overrides)
    return payload


def test_first_run_keeps_episode_body():
    """旧产物为空（首次执行）时必须保留分集正文。"""
    payload = _payload()
    kept = WorkflowEngine._drop_injected_params(None, payload)
    assert kept["episodes"] == payload["episodes"]


def test_import_artifact_keeps_episodes_after_filter():
    """上传导入的真实产物过一遍过滤器后仍带分集正文。"""
    from core.script_import import build_import_artifact

    artifact = build_import_artifact("第1集\n小李：你好。\n", session_id="s1", title="测试")
    assert artifact["episodes"]

    kept = WorkflowEngine._drop_injected_params(None, artifact)
    assert kept["episodes"] == artifact["episodes"]
    assert kept["characters"] == artifact["characters"]


def test_session_param_episodes_still_dropped():
    """会话参数形态（int）仍按注入参数剔除，不能混进产物。"""
    kept = WorkflowEngine._drop_injected_params(None, _payload(episodes=3))
    assert "episodes" not in kept


def test_other_injected_params_are_dropped():
    payload = _payload(llm_model="qwen", style="realistic", script_source="upload", file_path="users/u/a.txt")
    kept = WorkflowEngine._drop_injected_params(None, payload)
    for key in ("llm_model", "style", "script_source", "file_path"):
        assert key not in kept
    assert kept["episodes"]


def test_existing_key_keeps_previous_semantics():
    """旧产物已有该键时本就不剔除，行为保持不变。"""
    payload = _payload(episodes=3)
    kept = WorkflowEngine._drop_injected_params({"episodes": [{"content": "旧正文"}]}, payload)
    assert kept["episodes"] == 3
