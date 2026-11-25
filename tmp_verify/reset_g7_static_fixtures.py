# -*- coding: utf-8 -*-
"""Reset group-7 browser fixtures in videoclaw_test before the static server starts."""
from __future__ import annotations

import copy
import time

from api.dependencies import workflow_engine
from core import script_revision
from core.orchestrator import WorkflowStage
from seed_g6_ui import script_artifact


def reset(sid, *, stage, reparse=None, suggestions=False, episodes=2, board=None):
    def apply(state):
        script = script_artifact(episodes)
        version = script_revision.structure_fingerprint(script, scope="body")
        state.artifacts = {"script_generation": copy.deepcopy(script)}
        state.executions = {}
        state.stage_progress = {}
        state.error = None
        state.current_stage = stage
        for key in state.status:
            state.status[key] = "pending"
        state.status["script_generation"] = "completed"
        state.meta = {key: value for key, value in state.meta.items() if not str(key).startswith("_")}
        if stage in (WorkflowStage.CHARACTER_DESIGN, WorkflowStage.STORYBOARD):
            state.artifacts["character_design"] = {
                "characters": [{
                    "id": "char_old", "name": "老张", "description": "旧描述",
                    "selected": "", "versions": [],
                }],
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
                "active_job": "fixture-job" if reparse == "running" else None,
                "error": "假模型失败" if reparse == "failed" else None,
                "summary": {"added": 1, "updated": 1, "suggested": 1} if reparse == "done" else None,
            }
            if reparse == "superseded":
                record.update({"superseded_at": time.time(), "superseded_attempt": 1})
            if reparse == "accepted_stale":
                record.update({
                    "accepted_by": "alice", "accepted_at": time.time(),
                    "accepted_base_version": version, "accepted_attempt": 2,
                })
            state.meta["_cast_reparse"] = record
        if suggestions:
            state.meta["_cast_suggestions"] = {
                "base_version": version,
                "generated_at": time.time(),
                "items": [{
                    "id": "cast_sug_ui", "kind": "characters", "container": "characters",
                    "item_id": "char_old", "name": "老张", "description": "模型建议描述",
                    "reason": "user_owned",
                }],
            }

    workflow_engine._mutate(sid, apply)


def add_revision(sid, scope):
    def apply(state):
        script = state.artifacts["script_generation"]
        base_version = script_revision.content_fingerprint(script)
        episodes = copy.deepcopy(script["episodes"])
        characters = copy.deepcopy(script["characters"])
        if scope == "episode":
            episodes[0]["content"] = "第1集正文：老张与新角色走进办公室。"
            characters.append({"name": "新角色", "description": "单集修订新增人物", "description_owner": "model"})
            preview = {"scope": "episode", "episode_number": 1, "episode": episodes[0], "summary": "单集修订夹具"}
        else:
            for episode in episodes:
                episode["content"] = f"{episode['act_title']}正文：全剧修订后的版本。"
            preview = {"scope": "all", "episodes": episodes, "summary": "全剧修订夹具"}
        preview.update({
            "instruction": "浏览器夹具", "base_version": base_version,
            "characters": characters, "settings": copy.deepcopy(script["settings"]),
            "diff_episode_numbers": [1], "removed_items": [], "user_edited_items": [],
            "stale_storyboards": [], "created_at": time.time(),
        })
        script["pending_revision"] = preview

    workflow_engine._mutate(sid, apply)


def add_quality(sid):
    def apply(state):
        script = state.artifacts["script_generation"]
        script["pending_review"] = {
            "summary": "质检应用/撤销夹具",
            "base_version": script_revision.content_fingerprint(script),
            "created_at": time.time(),
            "issues": [{
                "id": "qr_g7_1", "episode_number": 1, "line": 1, "problem": "表达生硬",
                "action": "replace", "original": "第1集正文：老张走进办公室。",
                "text": "第1集正文：老张走进办公室，发现一封神秘信件。",
            }],
        }

    workflow_engine._mutate(sid, apply)


def main():
    reset("g7ui_locked", stage=WorkflowStage.CHARACTER_DESIGN, reparse="done")
    reset("g7ui_pending_long", stage=WorkflowStage.SCRIPT_GENERATION, reparse="pending")
    reset("g7ui_manual", stage=WorkflowStage.SCRIPT_GENERATION)
    reset("g7ui_failed", stage=WorkflowStage.SCRIPT_GENERATION, reparse="failed")
    reset("g7ui_stopped", stage=WorkflowStage.SCRIPT_GENERATION, reparse="stopped")
    reset("g7ui_running", stage=WorkflowStage.SCRIPT_GENERATION, reparse="running")
    reset("g7ui_superseded", stage=WorkflowStage.SCRIPT_GENERATION, reparse="superseded")
    reset("g7ui_accepted", stage=WorkflowStage.SCRIPT_GENERATION, reparse="accepted_stale")
    reset("g7ui_suggest", stage=WorkflowStage.SCRIPT_GENERATION, reparse="done", suggestions=True)
    reset("g7ui_storyboard_missing", stage=WorkflowStage.STORYBOARD, episodes=3)
    reset("g7ui_storyboard_empty", stage=WorkflowStage.STORYBOARD, episodes=3, board={
        "episodes": [
            {"episode_number": 1, "episode_title": "第1集", "segments": [{"segment_id": "seg_1"}]},
            {"episode_number": 2, "episode_title": "第2集", "segments": []},
        ]
    })
    reset("g7ui_revision_single", stage=WorkflowStage.SCRIPT_GENERATION)
    reset("g7ui_revision_all", stage=WorkflowStage.SCRIPT_GENERATION)
    reset("g7ui_quality", stage=WorkflowStage.SCRIPT_GENERATION)
    add_revision("g7ui_revision_single", "episode")
    add_revision("g7ui_revision_all", "all")
    add_quality("g7ui_quality")
    print("reset g7ui_* static fixtures")


if __name__ == "__main__":
    main()
