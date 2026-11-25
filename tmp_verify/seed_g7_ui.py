# -*- coding: utf-8 -*-
"""Seed isolated videoclaw_test sessions for group-7 browser verification."""
from __future__ import annotations

import copy
import time

from api.dependencies import workflow_engine
from core import script_revision
from core.orchestrator import WorkflowStage
from db.database import init_db
from seed_g6_ui import seed


def _mutate(sid, apply):
    workflow_engine._mutate(sid, apply)


def _seed_revision(sid, scope):
    seed(sid, stage=WorkflowStage.SCRIPT_GENERATION)

    def apply(state):
        script = state.artifacts["script_generation"]
        base_version = script_revision.content_fingerprint(script)
        episodes = copy.deepcopy(script["episodes"])
        characters = copy.deepcopy(script["characters"])
        if scope == "episode":
            episodes[0]["content"] = "第1集正文：老张与新角色走进办公室。"
            characters.append({
                "name": "新角色",
                "description": "单集修订新增人物",
                "description_owner": "model",
            })
            preview = {
                "scope": "episode",
                "episode_number": 1,
                "episode": episodes[0],
                "summary": "单集修订夹具",
            }
        else:
            for episode in episodes:
                episode["content"] = f"{episode['act_title']}正文：全剧修订后的版本。"
            preview = {
                "scope": "all",
                "episodes": episodes,
                "summary": "全剧修订夹具",
            }
        preview.update({
            "instruction": "浏览器夹具",
            "base_version": base_version,
            "characters": characters,
            "settings": copy.deepcopy(script["settings"]),
            "diff_episode_numbers": [1],
            "removed_items": [],
            "user_edited_items": [],
            "stale_storyboards": [],
            "created_at": time.time(),
        })
        script["pending_revision"] = preview

    _mutate(sid, apply)


def _seed_quality(sid):
    seed(sid, stage=WorkflowStage.SCRIPT_GENERATION)

    def apply(state):
        script = state.artifacts["script_generation"]
        script["pending_review"] = {
            "summary": "质检应用/撤销夹具",
            "base_version": script_revision.content_fingerprint(script),
            "created_at": time.time(),
            "issues": [{
                "id": "qr_g7_1",
                "episode_number": 1,
                "line": 1,
                "problem": "表达生硬",
                "action": "replace",
                "original": "第1集正文：老张走进办公室。",
                "text": "第1集正文：老张走进办公室，发现一封神秘信件。",
            }],
        }

    _mutate(sid, apply)


def main():
    init_db()
    seed("g7ui_locked", stage=WorkflowStage.CHARACTER_DESIGN, reparse="done")
    seed("g7ui_pending_long", stage=WorkflowStage.SCRIPT_GENERATION, reparse="pending")
    seed("g7ui_manual", stage=WorkflowStage.SCRIPT_GENERATION)
    seed("g7ui_failed", stage=WorkflowStage.SCRIPT_GENERATION, reparse="failed")
    seed("g7ui_stopped", stage=WorkflowStage.SCRIPT_GENERATION, reparse="stopped")
    seed("g7ui_running", stage=WorkflowStage.SCRIPT_GENERATION, reparse="running")
    seed("g7ui_superseded", stage=WorkflowStage.SCRIPT_GENERATION, reparse="superseded")
    seed("g7ui_accepted", stage=WorkflowStage.SCRIPT_GENERATION, reparse="accepted_stale")
    seed("g7ui_suggest", stage=WorkflowStage.SCRIPT_GENERATION, reparse="done", suggestions=True)
    seed("g7ui_storyboard_missing", stage=WorkflowStage.STORYBOARD, episodes=3)
    seed("g7ui_storyboard_empty", stage=WorkflowStage.STORYBOARD, episodes=3, board={
        "episodes": [
            {"episode_number": 1, "episode_title": "第1集", "segments": [{"segment_id": "seg_1"}]},
            {"episode_number": 2, "episode_title": "第2集", "segments": []},
        ]
    })
    _seed_revision("g7ui_revision_single", "episode")
    _seed_revision("g7ui_revision_all", "all")
    _seed_quality("g7ui_quality")
    print("seeded g7ui_* sessions in videoclaw_test")


if __name__ == "__main__":
    main()
