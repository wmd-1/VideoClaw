# -*- coding: utf-8 -*-
"""Seed isolated videoclaw_test sessions for group-6 browser verification."""
from __future__ import annotations

import copy
import time

from api.dependencies import workflow_engine
from core import script_revision
from core.orchestrator import WorkflowStage
from db.database import init_db


def script_artifact(episodes=2):
    return {
        "title": "第6组浏览器夹具",
        "logline": "用于验证剧本锁、重解析状态与分镜占位卡",
        "episodes": [
            {"episode_number": number, "act_title": f"第{number}集", "content": f"第{number}集正文：老张走进办公室。"}
            for number in range(1, episodes + 1)
        ],
        "characters": [{
            "character_id": "char_old",
            "name": "老张",
            "description": "旧描述",
            "species": "人类",
            "description_owner": "user",
        }],
        "settings": [{
            "setting_id": "set_old",
            "name": "办公室",
            "description": "旧场景",
            "description_owner": "model",
        }],
        "meta": {"language": "zh", "source_format": "episode"},
    }


def seed(sid, *, stage, reparse=None, suggestions=False, episodes=2, board=None):
    workflow_engine.create_session(sid, {"idea": "g6 ui", "llm_model": "fake"}, owner="alice")
    script = script_artifact(episodes)
    version = script_revision.structure_fingerprint(script, scope="body")

    def apply(state):
        state.artifacts["script_generation"] = copy.deepcopy(script)
        state.current_stage = stage
        for key in state.status:
            state.status[key] = "pending"
        state.status["script_generation"] = "completed"
        if stage in (WorkflowStage.CHARACTER_DESIGN, WorkflowStage.STORYBOARD):
            state.artifacts["character_design"] = {
                "characters": [{"id": "char_old", "name": "老张", "description": "旧描述", "selected": "", "versions": []}],
                "settings": [],
            }
            state.status["character_design"] = "completed"
        if stage == WorkflowStage.STORYBOARD:
            state.artifacts["storyboard"] = copy.deepcopy(board or {
                "episodes": [
                    {"episode_number": number, "episode_title": f"第{number}集", "segments": [{"segment_id": f"seg_{number}"}]}
                    for number in range(1, episodes)
                ]
            })
            state.status["storyboard"] = "completed"
        if reparse:
            record = {
                "status": reparse,
                "attempt": 2 if reparse in ("superseded", "accepted_stale") else 1,
                "base_version": version,
                "active_job": "job" if reparse == "running" else None,
                "error": "假模型失败" if reparse == "failed" else None,
                "summary": {"added": 1, "updated": 1, "suggested": 1} if reparse == "done" else None,
            }
            if reparse == "superseded":
                record.update({"superseded_at": time.time(), "superseded_attempt": 1})
            if reparse == "accepted_stale":
                record.update({
                    "accepted_by": "alice",
                    "accepted_at": time.time(),
                    "accepted_base_version": version,
                    "accepted_attempt": 2,
                })
            state.meta["_cast_reparse"] = record
        if suggestions:
            state.meta["_cast_suggestions"] = {
                "base_version": version,
                "generated_at": time.time(),
                "items": [{
                    "id": "cast_sug_ui",
                    "kind": "characters",
                    "container": "characters",
                    "item_id": "char_old",
                    "name": "老张",
                    "description": "模型建议描述",
                    "reason": "user_owned",
                }],
            }

    workflow_engine._mutate(sid, apply)


def main():
    init_db()
    seed("g6ui_locked", stage=WorkflowStage.CHARACTER_DESIGN, reparse="done")
    seed("g6ui_pending_late", stage=WorkflowStage.SCRIPT_GENERATION, reparse="pending")
    seed("g6ui_running_late", stage=WorkflowStage.SCRIPT_GENERATION, reparse="running")
    seed("g6ui_failed", stage=WorkflowStage.SCRIPT_GENERATION, reparse="failed")
    seed("g6ui_stopped", stage=WorkflowStage.SCRIPT_GENERATION, reparse="stopped")
    seed("g6ui_superseded_late", stage=WorkflowStage.SCRIPT_GENERATION, reparse="superseded")
    seed("g6ui_accepted", stage=WorkflowStage.SCRIPT_GENERATION, reparse="accepted_stale")
    seed("g6ui_suggest", stage=WorkflowStage.SCRIPT_GENERATION, reparse="done", suggestions=True)
    seed("g6ui_suggest2", stage=WorkflowStage.SCRIPT_GENERATION, reparse="done", suggestions=True)
    seed("g6ui_storyboard", stage=WorkflowStage.STORYBOARD, episodes=3)
    seed("g6ui_storyboard_empty", stage=WorkflowStage.STORYBOARD, episodes=3, board={
        "episodes": [
            {"episode_number": 1, "episode_title": "第1集", "segments": [{"segment_id": "seg_1"}]},
            {"episode_number": 2, "episode_title": "第2集", "segments": []},
        ]
    })
    print("seeded g6ui_* sessions in videoclaw_test")


if __name__ == "__main__":
    main()
