"""首帧参考图评估边界回归（WF-11 同族）。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

import asyncio

from core.agents.reference_agent import ReferenceGeneratorAgent

SEGMENT = {"segment_id": "seg1", "shots": [{"content": "小陈整理材料", "duration": 8}],
           "location": "支行办公室", "characters": ["小陈"]}


def test_reference_stops_after_unavailable_evaluation(monkeypatch, tmp_path):
    """评估不可用时保留本次图片并停止重试：不得把评估故障变成多次生图。"""
    agent = ReferenceGeneratorAgent()
    target = tmp_path / "seg1.png"
    counter = {"gen": 0}

    def fake_generate(*args, **kwargs):
        counter["gen"] += 1
        target.write_bytes(b"img")
        return [str(target)], "prompt", None

    monkeypatch.setattr(agent, "_next_version_path", lambda sid, shot_id: str(target))
    monkeypatch.setattr(agent, "_generate_image_with_doctor", fake_generate)
    monkeypatch.setattr(agent, "_evaluate_with_vlm", lambda *a, **k: {
        "evaluated": False, "eval_status": "unavailable", "score": 0,
        "hard_failures": ["VLM评估失败"], "issues": ["boom"], "is_acceptable": False,
    })

    _, path, eval_result, _ = agent._generate_one(
        object(), "sid", dict(SEGMENT), "首帧提示词", [], "anime", "it2i", "t2i",
    )

    assert counter["gen"] == 1, "评估不可用时不应继续生成下一版本"
    assert path == str(target)
    assert eval_result["evaluated"] is False


def test_reference_disabled_evaluation_skips_vlm_and_versions(monkeypatch, tmp_path):
    agent = ReferenceGeneratorAgent()
    target = tmp_path / "seg1.png"
    counter = {"gen": 0}

    def fake_generate(*args, **kwargs):
        counter["gen"] += 1
        target.write_bytes(b"img")
        return [str(target)], "prompt", None

    def fail_vlm(*args, **kwargs):
        raise AssertionError("评分关闭时不得调用 VLM")

    monkeypatch.setattr(agent, "_next_version_path", lambda sid, shot_id: str(target))
    monkeypatch.setattr(agent, "_generate_image_with_doctor", fake_generate)
    monkeypatch.setattr(agent, "_evaluate_with_vlm", fail_vlm)
    monkeypatch.setattr(agent, "_select_best_with_vlm", fail_vlm)

    _, path, eval_result, _ = agent._generate_one(
        object(), "sid", dict(SEGMENT), "首帧提示词", [], "anime", "it2i", "t2i",
        vlm_eval_enabled=False,
    )

    assert counter["gen"] == 1
    assert path == str(target)
    assert eval_result["evaluated"] is False
    assert eval_result["eval_status"] == "disabled"
    assert "score" not in eval_result


def test_reference_disabled_evaluation_keeps_doctor_retry(monkeypatch, tmp_path):
    agent = ReferenceGeneratorAgent()
    target = tmp_path / "seg1.png"
    calls = {"generate": 0}

    class FakeImageClient:
        def generate_image(self, **kwargs):
            calls["generate"] += 1
            if calls["generate"] == 1:
                raise RuntimeError("prompt format error")
            target.write_bytes(b"img")
            return [str(target)]

    monkeypatch.setattr(agent, "_next_version_path", lambda sid, shot_id: str(target))
    monkeypatch.setattr(
        "core.agents.doctor_agent.DoctorAgent.maybe_rewrite_prompt",
        lambda self, **kwargs: ("rewritten prompt", {"reason": "format"}, {"doctor": True}),
    )
    monkeypatch.setattr(agent, "_evaluate_with_vlm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得调用 VLM")))

    _, path, eval_result, rewrite_result = agent._generate_one(
        FakeImageClient(), "sid", dict(SEGMENT), "首帧提示词", [], "anime", "it2i", "t2i",
        llm_model="llm", vlm_eval_enabled=False,
    )

    assert calls["generate"] == 2
    assert path == str(target)
    assert eval_result["eval_status"] == "disabled"
    assert rewrite_result == {"doctor": True}


def test_reference_process_allows_missing_vlm_when_scoring_disabled(monkeypatch):
    from config import settings
    import models.config_model as config_model
    import models.image_client as image_client

    class FakeImageClient:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(settings, "IMAGE_QUALITY_VLM_EVAL_ENABLED", False)
    monkeypatch.setattr(image_client, "ImageClient", FakeImageClient)
    monkeypatch.setattr(config_model, "get_max_concurrency", lambda model, enabled: 1)
    agent = ReferenceGeneratorAgent()
    try:
        asyncio.run(agent.process({
            "session_id": "s", "style": "anime", "llm_model": "llm",
            "image_t2i_model": "t2i", "image_it2i_model": "it2i",
            "_session_artifacts": {"storyboard": {"episodes": []}},
        }))
    except Exception as exc:
        assert "未找到分镜" in str(exc)
    else:
        raise AssertionError("应继续到分镜输入校验，而不是因缺少 vlm_model 提前失败")


def test_reference_eval_failure_result_is_marked_not_evaluated(monkeypatch):
    def boom():
        raise RuntimeError("vlm down")

    class FakeVLM:
        def query(self, *args, **kwargs):
            return boom()

    monkeypatch.setattr("models.vlm_client.VLM", FakeVLM)
    result = ReferenceGeneratorAgent()._evaluate_with_vlm("x.png", dict(SEGMENT), "plot", "prompt")
    assert result["evaluated"] is False
    assert result["eval_status"] == "unavailable"
    assert result["is_acceptable"] is False
