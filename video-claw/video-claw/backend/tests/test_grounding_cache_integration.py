"""D2：H3 grounding 的跨执行复用、隔离与负缓存。"""
from __future__ import annotations

import glob
import json

from config import Config
from core.agents.prompt_rewrite_agent import PromptRewriteAgent
from core.grounding_cache import grounding_cache


class _FakeVLM:
    calls = 0
    answer = "画面描述"

    def __init__(self):
        pass

    def query(self, prompt, image_paths=None, model=None):
        type(self).calls += 1
        return type(self).answer


def _patch(monkeypatch, answer="画面描述"):
    _FakeVLM.calls = 0
    _FakeVLM.answer = answer
    monkeypatch.setattr("models.vlm_client.VLM", _FakeVLM)
    monkeypatch.setattr(Config, "H3_REWRITE_GROUNDING_ENABLED", True)


def _image(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_same_session_reuses_across_agent_instances(monkeypatch, tmp_path):
    _patch(monkeypatch)
    sid = "s-d2-reuse"
    grounding_cache.clear_session(sid)
    image = _image(tmp_path, "a.png", b"d2-image-a")

    text1, hashes1 = PromptRewriteAgent()._grounding_describe([image], "vlm", sid)
    text2, hashes2 = PromptRewriteAgent()._grounding_describe([image], "vlm", sid)
    assert text1 == text2 == "<Picture 1>：画面描述"
    assert hashes1 == hashes2
    assert _FakeVLM.calls == 1


def test_session_model_and_content_changes_recompute(monkeypatch, tmp_path):
    _patch(monkeypatch)
    sid = "s-d2-scope"
    grounding_cache.clear_session(sid)
    image = _image(tmp_path, "b.png", b"d2-image-b")
    other = _image(tmp_path, "c.png", b"d2-image-c")

    PromptRewriteAgent()._grounding_describe([image], "vlm", sid)
    PromptRewriteAgent()._grounding_describe([image], "vlm", "s-other")
    PromptRewriteAgent()._grounding_describe([other], "vlm", sid)
    PromptRewriteAgent()._grounding_describe([image], "vlm2", sid)
    assert _FakeVLM.calls == 4


def test_empty_description_is_negative_only_for_current_agent(monkeypatch, tmp_path):
    _patch(monkeypatch, answer="")
    sid = "s-d2-empty"
    grounding_cache.clear_session(sid)
    image = _image(tmp_path, "d.png", b"d2-image-d")

    agent = PromptRewriteAgent()
    assert agent._grounding_describe([image], "vlm", sid)[0] == ""
    assert agent._grounding_describe([image], "vlm", sid)[0] == ""
    assert _FakeVLM.calls == 1, "同一执行内空结果应负缓存"

    PromptRewriteAgent()._grounding_describe([image], "vlm", sid)
    assert _FakeVLM.calls == 2, "跨执行不得复用空结果"


def test_cache_hit_is_observable(monkeypatch, tmp_path):
    _patch(monkeypatch)
    monkeypatch.setenv("VC_OBSERVABILITY_DIR", str(tmp_path / "obs"))
    sid = "s-d2-observe"
    grounding_cache.clear_session(sid)
    image = _image(tmp_path, "e.png", b"d2-image-e")

    PromptRewriteAgent()._grounding_describe([image], "vlm", sid)
    PromptRewriteAgent()._grounding_describe([image], "vlm", sid)
    records = []
    for path in glob.glob(str(tmp_path / "obs" / "calls-*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle if line.strip())
    assert any(record["cache_hit"] for record in records)
    assert any(record["cache_key_kind"] == "grounding" for record in records)
