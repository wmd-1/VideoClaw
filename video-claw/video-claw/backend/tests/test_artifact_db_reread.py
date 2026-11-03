"""A1 证据：payload 写入 DB 后重新读取，字段不得丢失或串味。

只验证“提交路径 + 回读”，不跑真实模型。
"""
from __future__ import annotations

import hashlib
import os
import uuid

from api.dependencies import workflow_engine
from core.agents.character_agent import CharacterDesignerAgent
from core.media_refs import to_public
from db.repositories import FileRepo

META = {
    "video_model": "m3", "video_first_frame_model": "m1",
    "video_generation_mode": "first_frame", "style": "realistic",
}


def _sid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _media(path: str, sid: str) -> str:
    """建索引行（发布器只允许复用已索引的本 owner 文件），返回 public 形式。"""
    public = to_public(path)
    FileRepo().upsert_many([{
        "file_id": hashlib.sha256((sid + public).encode()).hexdigest()[:32],
        "owner": "alice", "session_id": sid, "stage": "video_generation",
        "kind": "video" if path.endswith(".mp4") else "file",
        "storage_kind": "local", "object_key": public.lstrip("/"),
        "public_path": public, "local_path": path,
        "media_type": "video/mp4", "size_bytes": os.path.getsize(path),
    }])
    return public


def _reload(stage: str, payload: dict, sid: str):
    workflow_engine.update_artifact(sid, stage, payload)
    # 绕过内存缓存：只有真正从 DB 重装载的字段才算“回读证据”
    workflow_engine.sessions.pop(sid, None)
    return workflow_engine.get_state(sid).artifacts[stage]


def _create(sid: str):
    workflow_engine.create_session(sid, META, owner="alice")


def test_character_payload_roundtrips_through_db(monkeypatch, result_file):
    sid = _sid("a1_char")
    _create(sid)
    media = _media(result_file, sid)
    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda s, atype, aid: [result_file, result_file])
    payload = agent._build_payload(
        "sid",
        {"c1": {"name": "小陈", "description": "银行职员"}},
        {"s1": {"name": "会议室", "description": "会议室"}},
        selected_chars={"c1": result_file},
        selected_sets={"s1": result_file},
        evaluations={
            "characters": {"c1": {"evaluated": True, "eval_status": "ok", "score": 8,
                                   "issues": ["原文不应入库"]}},
            "settings": {"s1": {"evaluated": False, "eval_status": "unavailable"}},
        },
    )["payload"]

    stored = _reload("character_design", payload, sid)
    char = stored["characters"][0]
    setting = stored["settings"][0]
    assert char["selected"] == media
    assert char["versions"] == [media], "重复路径应去重"
    assert char["description"] == "银行职员"
    assert char["evaluation"] == {"evaluated": True, "eval_status": "ok", "score": 8}
    assert "issues" not in char["evaluation"], "VLM 原文不得落库"
    assert setting["evaluation"] == {"evaluated": False, "eval_status": "unavailable"}


def test_character_empty_run_derives_and_inherits_evaluation_in_db(monkeypatch, result_file):
    """空跑：从已存产物推导评估底稿，合入后写库再回读仍保留。"""
    sid = _sid("a1_empty")
    _create(sid)
    media = _media(result_file, sid)
    seeded = {"characters": [{
        "id": "c1", "name": "小陈", "description": "银行职员",
        "selected": media, "versions": [media],
        "evaluation": {"evaluated": False, "eval_status": "unavailable"},
    }]}
    workflow_engine.create_session(sid, META, owner="alice")
    workflow_engine.update_artifact(sid, "character_design", seeded)

    agent = CharacterDesignerAgent()
    monkeypatch.setattr(agent, "_list_versions", lambda s, atype, aid: [result_file])
    seeded_stored = workflow_engine.get_state(sid).artifacts["character_design"]
    existing = {
        "characters": CharacterDesignerAgent._existing_evaluations(seeded_stored, "characters"),
        "settings": CharacterDesignerAgent._existing_evaluations(seeded_stored, "settings"),
    }
    payload = agent._build_payload(
        "sid", {"c1": {"name": "小陈", "description": "银行职员"}}, {},
        selected_chars={"c1": result_file},
        existing_evaluations=existing,
    )["payload"]

    stored = _reload("character_design", payload, sid)
    char = stored["characters"][0]
    assert char["selected"] == media
    assert char["evaluation"] == {"evaluated": False, "eval_status": "unavailable"}


def test_video_prompt_fields_roundtrip_through_db(result_file):
    sid = _sid("a1_video")
    _create(sid)
    media = _media(result_file, sid)
    clip = {
        "id": "seg_01_01", "description": "小陈汇报", "status": "done",
        "selected": result_file, "versions": [result_file],
        "manual_prompt": "人工正文",
        "prompt_source": {"source": "manual", "freshness": "fresh", "stale_reasons": []},
        "submitted_prompt": "真实提交正文",
        "resolved_fingerprint": "rfp", "resolved_dimensions": {"duration": "d1"},
    }
    stored = _reload("video_generation", {"clips": [clip]}, sid)["clips"][0]
    assert stored["manual_prompt"] == "人工正文"
    assert stored["prompt_source"]["source"] == "manual"
    for key in ("submitted_prompt", "resolved_fingerprint", "resolved_dimensions"):
        assert stored[key] == clip[key], f"字段丢失或改写：{key}"
    assert stored["selected"] == media
    assert stored["description"] == "小陈汇报"
