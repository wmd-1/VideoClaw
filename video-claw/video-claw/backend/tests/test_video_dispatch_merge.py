"""会话参数合并：显式 false/null 不被会话快照覆盖，解析结果不回写。"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from api.dependencies import workflow_engine
from core.agents.base_agent import AgentInterface


class _Agent(AgentInterface):
    async def process(self, input_data: Any, intervention: Optional[Dict] = None) -> Dict:
        return {}


def test_explicit_false_and_null_are_not_backfilled():
    agent = _Agent()
    merged = agent._merge_session_params(
        {
            "video_dispatch_mode": "manual",
            "video_profile_id": None,
            "enable_concurrency": False,
            "_session_meta": {
                "video_dispatch_mode": "auto",
                "video_profile_id": "default-pool",
                "enable_concurrency": True,
                "video_model": "session-model",
            },
        }
    )
    assert merged["video_dispatch_mode"] == "manual"
    assert merged["video_profile_id"] is None
    assert merged["enable_concurrency"] is False
    assert merged["video_model"] == "session-model"


def test_missing_keys_are_filled_from_session_snapshot():
    agent = _Agent()
    merged = agent._merge_session_params(
        {
            "video_dispatch_mode": "auto",
            "_session_meta": {
                "video_profile_id": "preferred-pool",
                "video_first_frame_model": "model-x",
            },
        }
    )
    assert merged["video_profile_id"] == "preferred-pool"
    assert merged["video_first_frame_model"] == "model-x"


def test_merge_never_writes_resolved_models_back_to_session():
    meta = {"video_dispatch_mode": "auto", "video_model": "session-model"}
    agent = _Agent()
    merged = agent._merge_session_params(
        {"video_model": "resolved-model", "_session_meta": meta}
    )
    assert merged["video_model"] == "resolved-model"
    assert meta["video_model"] == "session-model"
    merged["video_model"] = "changed-again"
    assert meta["video_model"] == "session-model"


def test_clear_preference_and_false_concurrency_persist():
    sid = f"m{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(
        sid,
        {
            "idea": "持久化快照",
            "video_dispatch_mode": "auto",
            "video_profile_id": "pool-a",
            "enable_concurrency": True,
        },
        owner="local",
    )
    workflow_engine.update_session_meta(
        sid,
        {"video_profile_id": None, "enable_concurrency": False},
        ("video_profile_id", "enable_concurrency"),
    )

    state = workflow_engine.get_state(sid)
    assert state is not None
    assert state.meta["video_profile_id"] is None
    assert state.meta["enable_concurrency"] is False
    assert state.meta["video_dispatch_mode"] == "auto"

    # 真正的持久化 round-trip：绕过内存状态，直接从 DB 读回 meta。
    from db.repositories import SessionRepo

    stored = SessionRepo().get(sid)["meta_json"]
    assert stored["video_profile_id"] is None
    assert stored["enable_concurrency"] is False
