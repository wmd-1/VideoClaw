"""WF-03／WF-11 回归：选优结果落库与评估状态语义。

只允许在既有后端容器内执行（详见 tests/conftest.py）：
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \
    /app/.venv/bin/python -m pytest /tmp/vc_tests -q
"""
from __future__ import annotations

from core.agents.character_agent import CharacterDesignerAgent


# ─────────────── WF-03：payload 底稿与选优结果 ───────────────


def test_existing_selected_reads_artifact_only_when_non_empty():
    artifact = {
        "characters": [{"id": "c1", "selected": "a.png"}, {"id": "c2", "selected": ""}],
        "settings": "not-a-list",
    }
    assert CharacterDesignerAgent._existing_selected(artifact, "characters") == {"c1": "a.png"}
    assert CharacterDesignerAgent._existing_selected(artifact, "settings") == {}
    assert CharacterDesignerAgent._existing_selected(None, "characters") == {}


def test_build_payload_keeps_provided_selection_over_latest(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png", "v2.png", "v3.png"])
    payload = agent._build_payload(
        "sid", {"c1": {"name": "小陈", "description": "d"}}, {},
        selected_chars={"c1": "v1.png"},
    )
    item = payload["payload"]["characters"][0]
    assert item["selected"] == "v1.png"
    assert item["versions"] == ["v1.png", "v2.png", "v3.png"]


def test_build_payload_falls_back_to_latest_without_selection(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png", "v2.png"])
    payload = agent._build_payload("sid", {"c1": {"name": "n", "description": "d"}}, {})
    assert payload["payload"]["characters"][0]["selected"] == "v2.png"


def test_build_payload_maps_settings_separately(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["s_old.png", "s_new.png"])
    payload = agent._build_payload(
        "sid", {}, {"s1": {"name": "办公室", "description": "d"}},
        selected_sets={"s1": "s_old.png"},
    )
    assert payload["payload"]["settings"][0]["selected"] == "s_old.png"


def test_build_preview_uses_existing_selection(monkeypatch):
    """预览与最终 payload 必须同一口径，否则同一次执行内“界面选中 ≠ 落库选中”。"""
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png", "v2.png", "v3.png"])
    preview = agent._build_preview(
        "sid", {"c1": {"name": "小陈", "description": "d"}}, {},
        {"characters": {"c1": "v1.png"}, "settings": {}},
    )
    assert preview["characters"][0]["selected"] == "v1.png"


def test_build_preview_falls_back_to_latest_without_selection(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png", "v2.png"])
    preview = agent._build_preview("sid", {"c1": {"name": "n", "description": "d"}}, {})
    assert preview["characters"][0]["selected"] == "v2.png"


def test_process_payload_and_done_event_share_selection(monkeypatch):
    """接线回归：本轮选第一版时，最终 payload 与 done 事件都必须是 v1（改动前为 v3）。"""
    import models.config_model as config_model
    import models.image_client as image_client

    class FakeImageClient:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(image_client, "ImageClient", FakeImageClient)
    monkeypatch.setattr(config_model, "get_max_concurrency", lambda model, enabled: 1)

    agent = CharacterDesignerAgent()
    events = []
    calls = {"n": 0}

    def fake_list(sid, atype, aid):
        calls["n"] += 1
        # 第一次是 run() 判断是否已有版本：返回空以触发本轮生成
        return [] if calls["n"] == 1 else ["v1.png", "v2.png", "v3.png"]

    monkeypatch.setattr(agent, "_list_versions", fake_list)
    monkeypatch.setattr(agent, "_build_preview", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: events.append(k.get("data")))
    monkeypatch.setattr(agent, "_session_meta", lambda input_data: {})
    monkeypatch.setattr(agent, "_session_artifact", lambda input_data, key: {
        "characters": [{"id": "c1", "selected": "", "versions": []}], "settings": [],
    })
    monkeypatch.setattr(agent, "_generate_one", lambda *a, **k: (
        "c1", "v1.png", {"evaluated": False, "eval_status": "selection_only"}, None,
    ))

    import asyncio

    result = asyncio.run(agent.process({
        "session_id": "sid", "style": "anime", "image_t2i_model": "t2i", "vlm_model": "vlm",
        "characters": {"c1": {"name": "小陈", "description": "d"}}, "settings": {},
    }))

    assert result["payload"]["characters"][0]["selected"] == "v1.png"
    done = [
        data["asset_complete"] for data in events
        if isinstance(data, dict) and (data.get("asset_complete") or {}).get("status") == "done"
    ]
    assert done and done[-1]["selected"] == "v1.png", "done 事件与 payload 的选中不一致"


# ─────────────── WF-11：评估状态不再假装合格 ───────────────


def _patch_vlm(monkeypatch, query):
    class FakeVLM:
        def __init__(self):
            pass

        def query(self, *args, **kwargs):
            return query()

    monkeypatch.setattr("models.vlm_client.VLM", FakeVLM)


def test_evaluation_unavailable_is_marked_not_evaluated(monkeypatch):
    def boom():
        raise RuntimeError("vlm down")

    _patch_vlm(monkeypatch, boom)
    result = CharacterDesignerAgent()._evaluate_with_vlm("x.png", "描述", "characters", "m")
    assert result["evaluated"] is False
    assert result["eval_status"] == "unavailable"
    assert result["is_acceptable"] is False
    assert "score" not in result


def test_evaluation_parse_failure_is_not_acceptable(monkeypatch):
    _patch_vlm(monkeypatch, lambda: "我觉得这张还行")
    result = CharacterDesignerAgent()._evaluate_with_vlm("x.png", "描述", "characters", "m")
    assert result["evaluated"] is False
    assert result["eval_status"] == "parse_failed"
    assert result["is_acceptable"] is False


def test_generate_one_stops_after_unavailable_evaluation(monkeypatch, tmp_path):
    """评估不可用时保留本次图片并停止重试：不得把评估故障变成多次生图。"""
    agent = CharacterDesignerAgent()
    target = tmp_path / "c1.png"
    counter = {"gen": 0}

    def fake_generate(*args, **kwargs):
        counter["gen"] += 1
        target.write_bytes(b"img")
        return [str(target)], "prompt", None

    monkeypatch.setattr(agent, "_next_version_path", lambda sid, atype, aid: str(target))
    monkeypatch.setattr(agent, "_generate_image_with_doctor", fake_generate)
    monkeypatch.setattr(
        agent, "_evaluate_with_vlm",
        lambda *a, **k: {"evaluated": False, "eval_status": "unavailable",
                         "issues": ["boom"], "is_acceptable": False},
    )

    _, path, eval_result, _ = agent._generate_one(
        object(), "c1", "小陈", "描述", "characters", "anime", "", "t2i", "vlm", "sid"
    )

    assert counter["gen"] == 1, "评估不可用时不应继续生图"
    assert path == str(target)
    assert eval_result["evaluated"] is False


def test_select_best_reports_selection_without_fabricating_pass(monkeypatch, tmp_path):
    _patch_vlm(monkeypatch, lambda: '{"best_index": 0}')
    paths = []
    for name in ("a.png", "b.png"):
        p = tmp_path / name
        p.write_bytes(b"img")
        paths.append(str(p))

    best_path, best_eval = CharacterDesignerAgent()._select_best_with_vlm(
        paths, "小陈", "三十岁客户经理", "characters", "", "vlm"
    )

    assert best_path == paths[0]
    assert best_eval["eval_status"] == "selection_only"
    assert best_eval["selection"] == {"source": "vlm_best", "index": 0, "candidates": 2}
    assert "score" not in best_eval
    assert "is_acceptable" not in best_eval


# ─────────────── Q9：评估状态落库白名单 ───────────────


def test_evaluation_payload_keeps_only_whitelisted_keys():
    view = CharacterDesignerAgent._evaluation_payload({
        "evaluated": False,
        "eval_status": "selection_only",
        "selection": {"source": "vlm_best", "index": 0, "candidates": 3},
        "score": 7,
        "issues": ["内部问题"],
        "suggestion": "内部建议",
        "hard_failures": ["内部硬性项"],
    })
    assert view == {
        "evaluated": False,
        "eval_status": "selection_only",
        "selection": {"source": "vlm_best", "index": 0, "candidates": 3},
        "score": 7,
    }
    for leaked in ("issues", "suggestion", "hard_failures"):
        assert leaked not in view


def test_evaluation_payload_marks_unknown_without_overclaiming():
    # 只有 score 的旧形态：不补合格，只标未知
    assert CharacterDesignerAgent._evaluation_payload({"score": 6}) == {
        "score": 6, "evaluated": None, "eval_status": "unknown",
    }
    assert CharacterDesignerAgent._evaluation_payload({"issues": ["x"]}) is None
    assert CharacterDesignerAgent._evaluation_payload(None) is None


def test_build_payload_attaches_evaluation_to_both_containers(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png"])
    payload = agent._build_payload(
        "sid",
        {"c1": {"name": "小陈", "description": "d"}},
        {"s1": {"name": "办公室", "description": "d"}},
        selected_chars={"c1": "v1.png"},
        selected_sets={"s1": "v1.png"},
        evaluations={
            "characters": {"c1": {"evaluated": True, "eval_status": "ok", "score": 8, "issues": ["x"]}},
            "settings": {"s1": {"evaluated": False, "eval_status": "unavailable"}},
        },
    )
    char = payload["payload"]["characters"][0]
    setting = payload["payload"]["settings"][0]
    assert char["evaluation"] == {"evaluated": True, "eval_status": "ok", "score": 8}
    assert setting["evaluation"] == {"evaluated": False, "eval_status": "unavailable"}


def test_build_payload_omits_evaluation_when_absent(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png"])
    payload = agent._build_payload("sid", {"c1": {"name": "n", "description": "d"}}, {})
    assert "evaluation" not in payload["payload"]["characters"][0]


def test_existing_evaluations_reads_whitelisted_view():
    artifact = {
        "characters": [{"id": "c1", "evaluation": {"evaluated": True, "eval_status": "ok"}}],
        "settings": "not-a-list",
    }
    assert CharacterDesignerAgent._existing_evaluations(artifact, "characters") == {
        "c1": {"evaluated": True, "eval_status": "ok"},
    }
    assert CharacterDesignerAgent._existing_evaluations(artifact, "settings") == {}
    assert CharacterDesignerAgent._existing_evaluations(None, "characters") == {}


def test_build_payload_inherits_existing_evaluation(monkeypatch):
    """全量空跑（本轮未生成）不得把历史评估状态抹掉。"""
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png"])
    payload = agent._build_payload(
        "sid", {"c1": {"name": "n", "description": "d"}}, {},
        existing_evaluations={
            "characters": {"c1": {"evaluated": False, "eval_status": "unavailable"}},
            "settings": {},
        },
    )
    assert payload["payload"]["characters"][0]["evaluation"] == {
        "evaluated": False, "eval_status": "unavailable",
    }


def test_build_payload_prefers_this_run_evaluation(monkeypatch):
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png"])
    payload = agent._build_payload(
        "sid", {"c1": {"name": "n", "description": "d"}}, {},
        evaluations={"characters": {"c1": {"evaluated": True, "eval_status": "ok", "score": 8}},
                     "settings": {}},
        existing_evaluations={"characters": {"c1": {"evaluated": False, "eval_status": "unavailable"}},
                              "settings": {}},
    )
    assert payload["payload"]["characters"][0]["evaluation"] == {
        "evaluated": True, "eval_status": "ok", "score": 8,
    }


def test_build_payload_falls_back_to_existing_when_this_run_unevaluable(monkeypatch):
    """本轮返回的只是失败标记（不可落库）时，不能算“本轮有评估”。"""
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda sid, atype, aid: ["v1.png"])
    payload = agent._build_payload(
        "sid", {"c1": {"name": "n", "description": "d"}}, {},
        evaluations={"characters": {"c1": {"error_type": "model_unavailable", "error": "down"}},
                     "settings": {}},
        existing_evaluations={"characters": {"c1": {"evaluated": True, "eval_status": "ok", "score": 8}},
                              "settings": {}},
    )
    assert payload["payload"]["characters"][0]["evaluation"] == {
        "evaluated": True, "eval_status": "ok", "score": 8,
    }


def test_process_events_carry_whitelisted_evaluation(monkeypatch):
    """事件层同样不能带原始 VLM 文本：issues/suggestion 不得进入 asset_complete。"""
    import asyncio

    import models.config_model as config_model
    import models.image_client as image_client

    class FakeImageClient:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(image_client, "ImageClient", FakeImageClient)
    monkeypatch.setattr(config_model, "get_max_concurrency", lambda model, enabled: 1)

    agent = CharacterDesignerAgent()
    events = []
    calls = {"n": 0}

    def fake_list(sid, atype, aid):
        calls["n"] += 1
        return [] if calls["n"] == 1 else ["v1.png"]

    monkeypatch.setattr(agent, "_list_versions", fake_list)
    monkeypatch.setattr(agent, "_build_preview", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_report_progress", lambda *a, **k: events.append(k.get("data")))
    monkeypatch.setattr(agent, "_session_meta", lambda input_data: {})
    monkeypatch.setattr(agent, "_session_artifact", lambda input_data, key: {
        "characters": [{"id": "c1", "selected": "", "versions": []}], "settings": [],
    })
    monkeypatch.setattr(agent, "_generate_one", lambda *a, **k: (
        "c1", "v1.png", {
            "evaluated": True, "eval_status": "ok", "score": 7,
            "issues": ["VLM_RAW_ISSUE"], "suggestion": "VLM_RAW_SUGGESTION",
        }, None,
    ))

    asyncio.run(agent.process({
        "session_id": "sid", "style": "anime", "image_t2i_model": "t2i", "vlm_model": "vlm",
        "characters": {"c1": {"name": "小陈", "description": "d"}}, "settings": {},
    }))

    done = [
        data["asset_complete"] for data in events
        if isinstance(data, dict) and (data.get("asset_complete") or {}).get("status") == "done"
    ]
    assert done, "未捕获到完成事件"
    evaluation = done[-1]["evaluation"]
    assert evaluation == {"evaluated": True, "eval_status": "ok", "score": 7}
    assert "issues" not in evaluation and "suggestion" not in evaluation
