"""首帧参考图评估边界回归（WF-11 同族）。

只允许在既有后端容器内执行（详见 tests/conftest.py）。
"""
from __future__ import annotations

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
