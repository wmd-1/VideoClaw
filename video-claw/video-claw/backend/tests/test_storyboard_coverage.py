# -*- coding: utf-8 -*-
"""分镜集覆盖与空 segments 确认守卫。"""
from __future__ import annotations

import copy
import uuid

import pytest

from api.dependencies import workflow_engine
from core import script_revision
from core.orchestrator import WorkflowStage


def _script(numbers):
    return {
        "episodes": [
            {"episode_number": number, "act_title": f"第{number}集", "content": f"正文 {number}"}
            for number in numbers
        ],
        "characters": [],
        "settings": [],
    }


def _storyboard(numbers, *, empty=()):
    return {
        "episodes": [
            {
                "episode_number": number,
                "episode_title": f"第{number}集",
                "segments": [] if number in empty else [{"segment_id": f"seg_{number}"}],
            }
            for number in numbers
        ]
    }


def test_storyboard_coverage_matrix():
    complete = script_revision.storyboard_coverage(_script([1, 2, 3]), _storyboard([1, 2, 3]))
    assert complete["complete"] is True
    assert complete["missing_episodes"] == []
    assert complete["empty_episodes"] == []

    missing = script_revision.storyboard_coverage(_script([1, 2, 3]), _storyboard([1, 2]))
    assert missing["complete"] is False
    assert missing["missing_episodes"] == [3]

    empty = script_revision.storyboard_coverage(_script([1, 2, 3]), _storyboard([1, 2, 3], empty=[2]))
    assert empty["complete"] is False
    assert empty["empty_episodes"] == [2]

    no_episodes = script_revision.storyboard_coverage(_script([1]), {"episodes": []})
    assert no_episodes["complete"] is False
    assert no_episodes["missing_episodes"] == [1]
    assert script_revision.storyboard_coverage(_script([]), {"episodes": []})["complete"] is False


def _session(script_numbers, storyboard_numbers, *, empty=()):
    sid = f"sc{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(sid, {"idea": "coverage", "llm_model": "fake"}, owner="alice")
    script = _script(script_numbers)
    storyboard = _storyboard(storyboard_numbers, empty=empty)

    def seed(state):
        state.artifacts["script_generation"] = script
        state.artifacts["character_design"] = {"characters": [{"id": "char_old"}], "settings": []}
        state.artifacts["storyboard"] = storyboard
        state.current_stage = WorkflowStage.STORYBOARD
        state.status["script_generation"] = "completed"
        state.status["character_design"] = "completed"
        state.status["storyboard"] = "completed"

    workflow_engine._mutate(sid, seed)
    return sid


@pytest.mark.parametrize(
    "storyboard_numbers,empty,code,missing,empty_expected",
    [
        ([1, 2], (), "storyboard_incomplete", [3], []),
        ([1, 2, 3], (2,), "storyboard_incomplete", [], [2]),
        ([], (), "storyboard_incomplete", [1, 2, 3], []),
    ],
)
def test_continue_rejects_incomplete_storyboard(
    client, storyboard_numbers, empty, code, missing, empty_expected
):
    sid = _session([1, 2, 3], storyboard_numbers, empty=empty)
    before = copy.deepcopy(workflow_engine.get_state(sid).status)
    response = client.post(
        f"/api/project/{sid}/continue",
        json={"stage": "storyboard"},
        headers={"X-User-Login-Name": "alice"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert detail["missing_episodes"] == missing
    assert detail["empty_episodes"] == empty_expected
    assert workflow_engine.get_state(sid).status == before


def test_continue_allows_complete_storyboard(client):
    sid = _session([1, 2, 3], [1, 2, 3])
    response = client.post(
        f"/api/project/{sid}/continue",
        json={"stage": "storyboard"},
        headers={"X-User-Login-Name": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["next_stage"] == "reference_generation"
