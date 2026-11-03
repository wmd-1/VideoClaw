"""Q9：参考图阶段评估状态落库（与角色阶段共用白名单）。"""
from __future__ import annotations

from core.agents.reference_agent import ReferenceGeneratorAgent
from core.asset_quality import evaluation_payload

SEGMENTS = [{
    "segment_id": "seg_01_01", "episode_number": 1, "segment_number": 1,
    "shots": [{"content": "小陈汇报"}],
}]


def _agent(monkeypatch):
    agent = ReferenceGeneratorAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, segment_id: ["v1.png"])
    return agent


def test_reference_payload_attaches_whitelisted_evaluation(monkeypatch):
    agent = _agent(monkeypatch)
    payload = agent._build_payload(
        "sid", SEGMENTS, {"artifacts": {}}, {"seg_01_01": "提示"},
        selected_images={"seg_01_01": "v1.png"},
        evaluations={"seg_01_01": {
            "evaluated": True, "eval_status": "ok", "score": 8,
            "selection": {"source": "vlm_best", "index": 0, "candidates": 2},
            "issues": ["内部问题"], "suggestion": "内部建议",
        }},
    )
    item = payload["payload"]["scenes"][0]
    assert item["evaluation"] == {
        "evaluated": True, "eval_status": "ok", "score": 8,
        "selection": {"source": "vlm_best", "index": 0, "candidates": 2},
    }
    assert "issues" not in item["evaluation"]
    assert "suggestion" not in item["evaluation"]


def test_reference_payload_inherits_existing_evaluation(monkeypatch):
    agent = _agent(monkeypatch)
    session_data = {"artifacts": {"reference_generation": {"scenes": [
        {"id": "seg_01_01", "evaluation": {"evaluated": False, "eval_status": "unavailable"}},
    ]}}}
    payload = agent._build_payload(
        "sid", SEGMENTS, session_data, {"seg_01_01": "提示"},
        selected_images={"seg_01_01": "v1.png"},
    )
    assert payload["payload"]["scenes"][0]["evaluation"] == {
        "evaluated": False, "eval_status": "unavailable",
    }


def test_reference_payload_prefers_this_run_evaluation(monkeypatch):
    agent = _agent(monkeypatch)
    session_data = {"artifacts": {"reference_generation": {"scenes": [
        {"id": "seg_01_01", "evaluation": {"evaluated": False, "eval_status": "unavailable"}},
    ]}}}
    payload = agent._build_payload(
        "sid", SEGMENTS, session_data, {"seg_01_01": "提示"},
        selected_images={"seg_01_01": "v1.png"},
        evaluations={"seg_01_01": {"evaluated": True, "eval_status": "ok", "score": 9}},
    )
    assert payload["payload"]["scenes"][0]["evaluation"] == {
        "evaluated": True, "eval_status": "ok", "score": 9,
    }


def test_reference_payload_omits_evaluation_when_absent(monkeypatch):
    agent = _agent(monkeypatch)
    payload = agent._build_payload(
        "sid", SEGMENTS, {"artifacts": {}}, {"seg_01_01": "提示"},
        selected_images={"seg_01_01": "v1.png"},
    )
    assert "evaluation" not in payload["payload"]["scenes"][0]


def test_shared_whitelist_is_single_source():
    """参考图与角色使用同一实现：原文一律被过滤。"""
    view = evaluation_payload({"evaluated": True, "eval_status": "ok", "issues": ["x"]})
    assert view == {"evaluated": True, "eval_status": "ok"}
    assert evaluation_payload({"issues": ["x"]}) is None
