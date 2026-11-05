"""剧本-1105 第 2/3 项：剧本手动编辑的下游同步、reconfirm 标记与分镜显式重跑。

覆盖 openspec change `script-manual-edit-resync` 的两条规格：
- `script-asset-sync`：手动编辑与应用修订共用同一收尾（重建阶段 2 + 分级标记下游）；
- `stage-view-resync`：保存响应即权威（status_map / stale_storyboards / stale_final_videos）。

零付费门：所有用例都不触网。分镜重跑用 monkeypatch 替换 `_design_episode_storyboard`
（真实执行 process 的过滤/合并/收尾逻辑，只把"模型产 segments"这一步换成假返回）。
必须在既有后端容器内执行（隔离库 videoclaw_test + videoclaw-test* 桶）。
"""
from __future__ import annotations

import asyncio
import copy
import uuid

import pytest

from api.dependencies import workflow_engine
from config import Config, settings
from core import media_refs, script_revision
from core.agents.base_agent import AgentInterface
from core.agents.storyboard_agent import StoryboardAgent
from core.asset_kind import asset_kind_for
from core.orchestrator import WorkflowStage
from db.repositories import FileRepo

OWNER = "alice"
STATUS_KEYS = (
    "script_generation",
    "character_design",
    "storyboard",
    "reference_generation",
    "prompt_rewrite",
    "video_generation",
    "post_production",
)


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    """执行根/结果根隔离：不触碰宿主 data/ 与业务桶。"""
    code_root = tmp_path / "code"
    for path in (code_root / "result", code_root / "temp", code_root / "work" / "executions"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code_root / "result"))
    monkeypatch.setattr(settings, "TEMP_DIR", str(code_root / "temp"))
    refs_root = code_root / "work" / "_refs"
    refs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(media_refs, "_REFS_ROOT", refs_root)
    yield tmp_path


def _media(name: str) -> str:
    """规范引用（已发布过的对象）：publisher 原样保留，不会尝试上传。"""
    return f"/code/result/_media/{name}"


def _script(char_names=("甲", "乙"), setting_names=("大厅",), content="甲：你好"):
    return {
        "title": "原标题",
        "episodes": [{"episode_number": 1, "act_title": "第1集", "content": content}],
        "characters": [
            {"name": name, "description": f"{name}的描述", "species": "人类"}
            for name in char_names
        ],
        "settings": [{"name": name, "description": f"{name}的描述"} for name in setting_names],
    }


def _stage2(char_ids, setting_ids):
    return {
        "characters": [
            {
                "id": cid, "name": name, "description": f"{name}的描述", "species": "人类",
                "asset_kind": "human", "selected": _media(f"{cid}.png"),
                "versions": [_media(f"{cid}.png")], "status": "done",
            }
            for cid, name in char_ids
        ],
        "settings": [
            {
                "id": sid, "name": name, "description": f"{name}的描述",
                "asset_kind": "place", "selected": _media(f"{sid}.png"),
                "versions": [_media(f"{sid}.png")], "status": "done",
            }
            for sid, name in setting_ids
        ],
    }


def _storyboard(segments_by_episode):
    return {
        "episodes": [
            {
                "episode_number": number,
                "segments": [
                    {
                        "segment_id": seg_id, "characters": ["甲"], "location": "大厅",
                        "shots": [{"plot": f"{seg_id} 正文", "duration": 5, "visual_prompt": f"{seg_id} 画面"}],
                    }
                    for seg_id in seg_ids
                ],
            }
            for number, seg_ids in segments_by_episode.items()
        ]
    }


def _index_media(sid: str, *payloads) -> None:
    """把种子里的规范引用先登进 artifact_files：publisher 对已索引对象走 reuse 不重传。

    只写隔离表行（storage_kind=minio、bucket=videoclaw-test），不往 MinIO 放真对象：
    reuse 路径不 stat对象，而本组用例只关心"引用是否被保留/移除"。
    """
    paths: set = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.startswith("/code/result/_media/"):
            paths.add(value)

    for payload in payloads:
        walk(payload)
    if not paths:
        return
    FileRepo().upsert_many([
        {
            "file_id": f"f-{uuid.uuid4().hex[:16]}",
            "owner": OWNER,
            "session_id": sid,
            "stage": "manual_sync_fixture",
            "kind": "artifact",
            "storage_kind": "minio",
            "bucket": settings.STORAGE_BUCKET,
            "object_key": f"{sid}/fixture-{index}.bin",
            "public_path": path,
            "media_type": "image/png",
            "size_bytes": 8,
        }
        for index, path in enumerate(sorted(paths))
    ])


def _seed(sid, *, script=None, stage2=None, storyboard=None, reference=None, rewrite=None,
          clips=None, post=None, statuses=None, current_stage=None):
    """直写产物与阶段状态（不经过同步逻辑），让每个用例的前置态可复核。"""
    _index_media(sid, script, stage2, storyboard, reference, rewrite, clips, post)


    def apply(state: object) -> None:
        for key, value in (
            ("script_generation", script),
            ("character_design", stage2),
            ("storyboard", storyboard),
            ("reference_generation", reference),
            ("prompt_rewrite", rewrite),
            ("video_generation", clips),
            ("post_production", post),
        ):
            if value is not None:
                state.artifacts[key] = copy.deepcopy(value)
        for key, value in (statuses or {}).items():
            state.status[key] = value
        if current_stage is not None:
            state.current_stage = current_stage
        return None

    workflow_engine._mutate(sid, apply)


def _session(**extra_meta) -> str:
    sid = f"ms{uuid.uuid4().hex[:10]}"
    workflow_engine.create_session(
        sid, {"idea": "手改同步", "llm_model": "fake-llm", **extra_meta}, owner=OWNER
    )
    return sid


def _state(sid):
    return workflow_engine.get_state(sid)


ALL_DONE_STATUSES = {
    "script_generation": "completed",
    "character_design": "completed",
    "storyboard": "completed",
    "reference_generation": "completed",
    "prompt_rewrite": "completed",
    "video_generation": "completed",
    "post_production": "completed",
}


def _full_pipeline(sid):
    """铺一份"全链路已执行完"的会话：剧本 2 角色、阶段 2 同 id、分镜 2 段、下游全 selected。"""
    script = _script()
    for item in script["characters"]:
        item["character_id"] = f"char_{item['name']}"
    for item in script["settings"]:
        item["setting_id"] = f"set_{item['name']}"
    stage2 = _stage2([("char_甲", "甲"), ("char_乙", "乙")], [("set_大厅", "大厅")])
    storyboard = _storyboard({1: ["seg_01_01", "seg_01_02"]})
    reference = {
        "scenes": [
            {"id": seg, "name": f"片段_{seg}", "episode": 1, "description": "画面",
             "selected": _media(f"{seg}.png"), "versions": [_media(f"{seg}.png")], "status": "done"}
            for seg in ("seg_01_01", "seg_01_02")
        ]
    }
    rewrite = {
        "items": [
            {"id": seg, "status": "done", "rewritten_prompt": f"{seg} 改写"}
            for seg in ("seg_01_01", "seg_01_02")
        ]
    }
    clips = {
        "clips": [
            {"id": seg, "name": f"片段_{seg}", "episode": 1, "description": "画面",
             "selected": _media(f"{seg}.mp4"), "versions": [_media(f"{seg}.mp4")], "status": "done"}
            for seg in ("seg_01_01", "seg_01_02")
        ]
    }
    _seed(
        sid,
        script=script,
        stage2=stage2,
        storyboard=storyboard,
        reference=reference,
        rewrite=rewrite,
        clips=clips,
        # 后期阶段尚无产物时状态重算会回到 pending；铺一份空成片清单保证前置态是 completed
        post={"final_videos": []},
        statuses=ALL_DONE_STATUSES,
        current_stage=WorkflowStage.STORYBOARD,
    )
    return script


def _art(sid, stage):
    return _state(sid).artifacts.get(stage)


# ───────────────────────── 1. 阶段 2 重建契约（手动路径） ─────────────────────────


def test_manual_delete_character_removes_only_that_stage2_item():
    sid = _session()
    script = _script()
    _seed(
        sid,
        script=script,
        stage2=_stage2([("char_甲", "甲"), ("char_乙", "乙")], [("set_大厅", "大厅")]),
        statuses={"script_generation": "completed", "character_design": "completed"},
    )
    kept = {"name": "甲", "description": "甲的描述", "species": "人类", "character_id": "char_甲"}

    workflow_engine.update_artifact(sid, "script_generation", {"characters": [kept]})

    chars = _art(sid, "character_design")["characters"]
    assert [item["name"] for item in chars] == ["甲"]
    # 保留条目：主键/已选图/版本历史逐字不变（重建只改集合，不动资产）
    assert chars[0]["id"] == "char_甲"
    assert chars[0]["selected"] == _media("char_甲.png")
    assert chars[0]["versions"] == [_media("char_甲.png")]
    assert _art(sid, "character_design")["settings"][0]["selected"] == _media("set_大厅.png")


def test_manual_add_character_creates_pending_item_and_backfills_id():
    sid = _session()
    script = _script(char_names=("甲",))
    for item in script["characters"]:
        item["character_id"] = f"char_{item['name']}"
    _seed(
        sid,
        script=script,
        stage2=_stage2([("char_甲", "甲")], []),
        statuses={"character_design": "completed"},
    )
    added = {"name": "丙", "description": "新角色描述", "species": "人类"}

    workflow_engine.update_artifact(
        sid, "script_generation", {"characters": [*script["characters"], added]}
    )

    chars = {item["name"]: item for item in _art(sid, "character_design")["characters"]}
    assert set(chars) == {"甲", "丙"}
    assert chars["丙"]["status"] == "pending"
    assert chars["丙"]["selected"] == "" and chars["丙"]["versions"] == []
    # 主键与种子化/导入同源（stable_id 口径），并回写进剧本产物
    from core import script_import

    expected = script_import.stable_id("char", "丙")
    assert chars["丙"]["id"] == expected
    script_item = next(i for i in _art(sid, "script_generation")["characters"] if i["name"] == "丙")
    assert script_item["character_id"] == expected


def test_repeated_save_does_not_drift_primary_key():
    sid = _session()
    script = _script(char_names=("甲",))
    characters = [*script["characters"], {"name": "丙", "description": "d", "species": "人类"}]
    _seed(sid, script=script, stage2=_stage2([("char_甲", "甲")], []),
          statuses={"character_design": "completed"})

    workflow_engine.update_artifact(sid, "script_generation", {"characters": characters})
    first = next(i for i in _art(sid, "character_design")["characters"] if i["name"] == "丙")["id"]
    workflow_engine.update_artifact(sid, "script_generation", {"characters": characters})
    workflow_engine.update_artifact(sid, "script_generation", {"characters": characters})
    second = next(i for i in _art(sid, "character_design")["characters"] if i["name"] == "丙")["id"]

    assert first == second, "原样再保存不得换主键（否则资产引用漂移）"


def test_rename_with_existing_id_keeps_generated_asset():
    sid = _session()
    script = _script(char_names=("小刘",))
    script["characters"][0]["character_id"] = "char_liu"
    _seed(
        sid,
        script=script,
        stage2=_stage2([("char_liu", "小刘")], []),
        statuses={"character_design": "completed"},
    )
    renamed = {"name": "刘姐", "description": "改名后的描述", "species": "人类", "character_id": "char_liu"}

    workflow_engine.update_artifact(sid, "script_generation", {"characters": [renamed]})

    chars = _art(sid, "character_design")["characters"]
    assert len(chars) == 1
    assert chars[0]["id"] == "char_liu"
    assert chars[0]["name"] == "刘姐"
    assert chars[0]["description"] == "改名后的描述"
    assert chars[0]["selected"] == _media("char_liu.png"), "改名不得丢掉已生成的图"


def test_legacy_items_without_id_backfill_then_rename_is_safe():
    sid = _session()
    # 旧会话：剧本条目无主键，阶段 2 用名称派生 id
    script = _script(char_names=("老角色",))
    legacy_id = script_revision.item_id({"name": "老角色"}, "character_id", "char")
    _seed(sid, script=script, stage2=_stage2([(legacy_id, "老角色")], []),
          statuses={"character_design": "completed"})

    workflow_engine.update_artifact(sid, "script_generation", {"characters": script["characters"]})
    backfilled = next(
        item for item in _art(sid, "script_generation")["characters"] if item["name"] == "老角色"
    )
    assert backfilled.get("character_id") == legacy_id, "首次保存须回填主键"

    # 首次保存即改名：名称兜底也命不中，按"删旧+加新"处理，不静默声称保住了资产
    other = _session()
    _seed(other, script=_script(char_names=("老角色",)),
          stage2=_stage2([("char_old", "老角色")], []),
          statuses={"character_design": "completed"})
    workflow_engine.update_artifact(
        other, "script_generation",
        {"characters": [{"name": "全新名字", "description": "d", "species": "人类"}]},
    )
    chars = _art(other, "character_design")["characters"]
    assert [item["name"] for item in chars] == ["全新名字"]
    assert chars[0]["id"] != "char_old" and chars[0]["selected"] == ""


def test_species_change_rederives_asset_kind():
    sid = _session()
    script = _script(char_names=("甲",))
    script["characters"][0]["character_id"] = "char_甲"
    _seed(sid, script=script, stage2=_stage2([("char_甲", "甲")], []),
          statuses={"character_design": "completed"})
    cat = {"name": "甲", "description": "甲的描述", "species": "猫", "character_id": "char_甲"}

    workflow_engine.update_artifact(sid, "script_generation", {"characters": [cat]})

    item = _art(sid, "character_design")["characters"][0]
    assert item["species"] == "猫"
    assert item["asset_kind"] == asset_kind_for("猫", container="characters")
    assert item["selected"] == _media("char_甲.png")


# ───────────────────────── 2. 变更判定与标记范围 ─────────────────────────


def test_title_only_edit_changes_nothing_downstream():
    sid = _session()
    _full_pipeline(sid)
    before_status = dict(_state(sid).status)
    before_stage2 = copy.deepcopy(_art(sid, "character_design"))
    before_progress = copy.deepcopy(_state(sid).stage_progress)

    result = workflow_engine.update_artifact(sid, "script_generation", {"title": "只改标题"})

    assert _state(sid).status == before_status
    assert _art(sid, "character_design") == before_stage2
    assert _state(sid).stage_progress == before_progress
    assert _state(sid).current_stage == WorkflowStage.STORYBOARD
    assert result["stale_storyboards"] == [] and result["stale_final_videos"] is False


def test_noop_save_is_not_marked():
    sid = _session()
    script = _full_pipeline(sid)
    characters = copy.deepcopy(_art(sid, "script_generation")["characters"])

    workflow_engine.update_artifact(sid, "script_generation", {"characters": characters})

    assert "reconfirm" not in set(_state(sid).status.values())
    assert "reconfirm" not in set(workflow_engine.update_artifact(
        sid, "script_generation", {"characters": characters}
    )["status_map"].values())
    assert script  # 前置态构造成功（避免误用空会话）


def test_structure_change_marks_downstream_without_touching_artifacts():
    sid = _session()
    _full_pipeline(sid)
    before = {key: copy.deepcopy(_art(sid, key)) for key in
              ("storyboard", "reference_generation", "prompt_rewrite", "video_generation", "post_production")}
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]

    workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})

    state = _state(sid)
    # 分级标记：角色/场景变更 → 阶段 2 起、后期之前的全部启用阶段（含可选 prompt_rewrite）
    for key in ("character_design", "storyboard", "reference_generation", "prompt_rewrite", "video_generation"):
        assert state.status[key] == "reconfirm", key
    assert state.status["post_production"] == "completed", "成片阶段状态不变"
    assert state.current_stage == WorkflowStage.SCRIPT_GENERATION
    # 只标记不动产物：阶段 3 及以后逐字节不变
    for key, snapshot in before.items():
        assert _art(sid, key) == snapshot, key
    # 角色/场景变更没有集级明细：stale_episodes 为空数组而非缺键
    progress = state.stage_progress["storyboard"]
    assert progress["stale_episodes"] == []
    assert progress["stale_reason"] == "script_structure_changed"


def test_stage2_without_artifact_is_not_created_or_marked():
    sid = _session()
    script = _script(char_names=("甲", "乙"))
    for item in script["characters"]:
        item["character_id"] = f"char_{item['name']}"
    _seed(sid, script=script, statuses={"script_generation": "completed"})

    workflow_engine.update_artifact(sid, "script_generation", {"characters": script["characters"][:1]})

    assert "character_design" not in _state(sid).artifacts
    assert _state(sid).status["character_design"] == "pending"


def test_reconfirm_survives_later_writes():
    sid = _session()
    _full_pipeline(sid)
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]
    workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})
    assert _state(sid).status["storyboard"] == "reconfirm"

    # 上传一张图（阶段 2 写入）→ 状态重算不得把标记冲回 completed
    workflow_engine.update_artifact(
        sid, "character_design",
        {"characters": [{"id": "char_甲", "selected": _media("char_甲.png")}]},
    )
    # 再保存一次剧本的非结构字段
    workflow_engine.update_artifact(sid, "script_generation", {"title": "又改标题"})

    status = _state(sid).status
    for key in ("character_design", "storyboard", "reference_generation", "prompt_rewrite", "video_generation"):
        assert status[key] == "reconfirm", key


def test_running_stage_is_not_marked_and_current_stage_kept():
    sid = _session()
    _full_pipeline(sid)
    _seed(sid, statuses={"reference_generation": "running"})
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]

    workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})

    state = _state(sid)
    assert state.status["reference_generation"] == "running", "在途执行不被改写"
    assert state.status["storyboard"] == "reconfirm"
    assert state.current_stage == WorkflowStage.STORYBOARD, "有 running 阶段时不得归位停点"


def test_continue_accepts_reconfirm_and_clears_its_details():
    sid = _session()
    _full_pipeline(sid)
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]
    workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})

    response = workflow_engine._continue_workflow(sid, "storyboard")

    assert response["status"] == "ready"
    state = _state(sid)
    assert state.status["storyboard"] == "completed"
    progress = state.stage_progress["storyboard"]
    assert "stale_episodes" not in progress and "stale_reason" not in progress
    # 只确认被点的阶段：其它阶段的标记仍在
    assert state.status["reference_generation"] == "reconfirm"
    assert state.artifacts["storyboard"]["episodes"][0]["segments"], "确认不重跑也不清空产物"


def test_body_change_marks_and_reports_stale_episodes():
    sid = _session()
    _full_pipeline(sid)
    episodes = copy.deepcopy(_art(sid, "script_generation")["episodes"])
    episodes[0]["content"] = "甲：正文被手改了"

    result = workflow_engine.update_artifact(sid, "script_generation", {"episodes": episodes})

    assert result["stale_storyboards"] == [1]
    state = _state(sid)
    assert "stale_storyboards" not in state.artifacts["script_generation"]
    assert state.stage_progress["storyboard"]["stale_episodes"] == [1]
    assert state.stage_progress["reference_generation"]["stale_episodes"] == [1]
    # 只改正文：条目集合未变 → 阶段 2 不标，分镜起标
    assert state.status["character_design"] == "completed"
    for key in ("storyboard", "reference_generation", "prompt_rewrite", "video_generation"):
        assert state.status[key] == "reconfirm", key


def test_final_videos_are_kept_and_flagged_only():
    sid = _session()
    _full_pipeline(sid)
    final = {"final_videos": [{"name": "成片", "path": _media("final.mp4"), "episode": 1}]}
    _seed(sid, post=copy.deepcopy(final), statuses={"post_production": "completed"})
    before_post = copy.deepcopy(_art(sid, "post_production"))
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]

    result = workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})

    state = _state(sid)
    assert result["stale_final_videos"] is True
    assert state.status["post_production"] == "completed"
    assert _art(sid, "post_production") == before_post, "成片产物与选择不得被改写"
    assert state.stage_progress["post_production"]["stale"] is True


def test_revision_apply_path_shares_the_same_sync():
    """应用修订与手动编辑走同一收尾：同一段变更前快照 → 同样的重建 + 标记结果。"""
    sid = _session()
    _full_pipeline(sid)

    collected: dict = {}

    def apply(state) -> None:
        before = script_revision.structure_snapshot(state.artifacts["script_generation"])
        state.artifacts["script_generation"]["characters"] = [
            item for item in state.artifacts["script_generation"]["characters"]
            if item["name"] == "甲"
        ]
        workflow_engine._recalculate_all_statuses(state)
        collected.update(workflow_engine._sync_script_structure(state, before) or {})

    workflow_engine._mutate(sid, apply)

    state = _state(sid)
    assert [i["name"] for i in _art(sid, "character_design")["characters"]] == ["甲"]
    assert state.status["storyboard"] == "reconfirm"
    assert state.status["post_production"] != "reconfirm"
    assert collected["stale_storyboards"] == []


# ───────────────────────── 3. 分镜显式重跑与 orphan 清理 ─────────────────────────


class ScriptedAgent(AgentInterface):
    """只替换模型产出，执行根/发布/收尾全部走真实编排器。"""

    def __init__(self, behavior):
        super().__init__(name="scripted")
        self.behavior = behavior

    async def process(self, input_data, intervention=None):
        return await self.behavior(self, input_data, intervention)


def _fake_segment_designer(produced: list, segments_by_episode: dict):
    """替换分镜的"模型产 segments"这一步：process 的过滤/合并/收尾仍走真实代码。"""

    async def fake(self, ep_number, ep_title, ep_content, chars, sets, style, llm_model, sid,
                   progress_note=None, script_context=None):
        produced.append(ep_number)
        return [
            {
                "segment_id": seg_id, "segment_number": index + 1, "episode_number": ep_number,
                "characters": ["甲"], "location": "大厅", "total_duration": 5,
                "shots": [{"plot": f"{seg_id} 新正文", "duration": 5, "visual_prompt": f"{seg_id} 新画面"}],
            }
            for index, seg_id in enumerate(segments_by_episode[ep_number])
        ]

    return fake


def _run_storyboard(sid: str, intervention: dict):
    state = workflow_engine.get_state(sid)
    return asyncio.run(workflow_engine.execute_stage(
        state, WorkflowStage.STORYBOARD,
        {"session_id": sid, "llm_model": "fake-llm", "style": "realistic"},
        intervention=intervention,
    ))


def test_rerun_episodes_parses_and_targets_only_requested_episode(monkeypatch):
    produced: list = []
    monkeypatch.setattr(
        StoryboardAgent, "_design_episode_storyboard",
        _fake_segment_designer(produced, {2: ["seg_02_01"]}),
    )
    script = {
        "episodes": [
            {"episode_number": 1, "act_title": "第1集", "content": "一"},
            {"episode_number": 2, "act_title": "第2集", "content": "二"},
        ],
        "characters": [{"name": "甲", "description": "d", "character_id": "char_甲"}],
        "settings": [],
    }
    existing = _storyboard({1: ["seg_01_01"], 2: ["seg_02_01"]})
    input_data = {
        "session_id": "rerun_unit",
        "llm_model": "fake-llm",
        "style": "realistic",
        "_session_artifacts": {"script_generation": script, "storyboard": existing},
    }

    result = asyncio.run(StoryboardAgent().process(input_data, {"regenerate_episodes": ["2", 7, "非法"]}))

    assert produced == [2], "只重跑指定集，已完成的第 1 集不得被重做"
    payload_eps = result["payload"]["episodes"]
    by_number = {ep["episode_number"]: ep for ep in payload_eps}
    assert by_number[1]["segments"] == existing["episodes"][0]["segments"], "未重跑集逐字保留"
    assert [s["segment_id"] for s in by_number[2]["segments"]] == ["seg_02_01"]
    assert result["stage_completed"] is True

    assert StoryboardAgent._rerun_episode_numbers({"regenerate_episodes": [1, "2", "x"]}) == {1, 2}
    assert StoryboardAgent._rerun_episode_numbers(None) == set()
    assert StoryboardAgent._rerun_episode_numbers({"regenerate_episodes": "2"}) == set()


def test_storyboard_rerun_prunes_orphan_items_and_marks_downstream(monkeypatch, tmp_path):
    sid = _session()
    script = _script(content="甲：正文已手改")
    for item in script["characters"]:
        item["character_id"] = f"char_{item['name']}"
    _seed(
        sid,
        script=script,
        stage2=_stage2([("char_甲", "甲")], [("set_大厅", "大厅")]),
        storyboard=_storyboard({1: ["seg_01_01", "seg_01_02"]}),
        reference={
            "scenes": [
                {"id": seg, "name": f"片段_{seg}", "episode": 1, "description": "画面",
                 "selected": _media(f"{seg}.png"), "versions": [_media(f"{seg}.png")], "status": "done"}
                for seg in ("seg_01_01", "seg_01_02")
            ]
        },
        rewrite={"items": [
            {"id": seg, "status": "done", "rewritten_prompt": f"{seg} 改写"}
            for seg in ("seg_01_01", "seg_01_02")
        ]},
        clips={"clips": [
            {"id": seg, "name": f"片段_{seg}", "episode": 1, "description": "画面", "duration": 5,
             "selected": _media(f"{seg}.mp4"), "versions": [_media(f"{seg}.mp4")], "status": "done"}
            for seg in ("seg_01_01", "seg_01_02")
        ]},
        post={"final_videos": [{"name": "成片", "path": _media("final.mp4"), "episode": 1}]},
        statuses=ALL_DONE_STATUSES,
        current_stage=WorkflowStage.STORYBOARD,
    )
    # 真实磁盘文件：清理只移引用，不得删文件
    disk = tmp_path / "code" / "result" / "_media" / "seg_01_02.png"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"legacy-image")
    produced: list = []
    # 真分镜智能体 + 假“模型产 segments”：过滤/合并/发布/收尾全部走真实代码
    monkeypatch.setitem(
        workflow_engine.agent_factories, WorkflowStage.STORYBOARD, lambda: StoryboardAgent()
    )
    monkeypatch.setattr(
        StoryboardAgent, "_design_episode_storyboard",
        _fake_segment_designer(produced, {1: ["seg_01_01"]}),
    )

    _run_storyboard(sid, {"regenerate_episodes": [1]})

    assert produced == [1], "重跑集合绕过 ready_eps"
    state = _state(sid)
    scenes = _art(sid, "reference_generation")["scenes"]
    items = _art(sid, "prompt_rewrite")["items"]
    clips = _art(sid, "video_generation")["clips"]
    assert [s["id"] for s in scenes] == ["seg_01_01"], "孤儿参考图引用被移除"
    assert [i["id"] for i in items] == ["seg_01_01"], "孤儿改写条目同样被清理"
    assert [c["id"] for c in clips] == ["seg_01_01"]
    assert scenes[0]["selected"] == _media("seg_01_01.png"), "仍存在 segment 的选择保留"
    assert clips[0]["versions"] == [_media("seg_01_01.mp4")]
    assert disk.exists(), "MUST NOT 删除磁盘文件"
    for key in ("reference_generation", "video_generation"):
        assert state.status[key] == "reconfirm", key
        assert state.stage_progress[key]["stale_episodes"] == [1], key
    # 提示词改写：分镜同步会把匹配条目打回 pending，重算后它是 waiting（比 reconfirm 更准：确实缺产物）
    assert state.status["prompt_rewrite"] == "waiting"
    assert state.status["storyboard"] != "reconfirm", "刚执行的阶段由收尾给结论，不留旧标记"
    assert state.status["post_production"] == "completed"
    assert state.stage_progress["post_production"]["stale"] is True
    assert _art(sid, "post_production")["final_videos"], "成片产物不被清空"


def test_rerun_prune_skips_disabled_prompt_rewrite(monkeypatch):
    monkeypatch.setattr(Config, "H3_REWRITE_ENABLED", False)
    sid = _session()
    _seed(
        sid,
        storyboard=_storyboard({1: ["seg_01_01", "seg_01_02"]}),
        reference={"scenes": [
            {"id": seg, "episode": 1, "selected": _media(f"{seg}.png"), "versions": [], "status": "done"}
            for seg in ("seg_01_01", "seg_01_02")
        ]},
        clips={"clips": [
            {"id": seg, "episode": 1, "selected": _media(f"{seg}.mp4"), "versions": [], "status": "done"}
            for seg in ("seg_01_01", "seg_01_02")
        ]},
        statuses={"storyboard": "completed", "reference_generation": "completed",
                  "video_generation": "completed"},
    )

    def apply(state) -> None:
        workflow_engine._prune_downstream_items_for_episodes(
            state, {1},
            _storyboard({1: ["seg_01_01", "seg_01_02"]}),
            _storyboard({1: ["seg_01_01"]}),
        )

    workflow_engine._mutate(sid, apply)

    state = _state(sid)
    assert "prompt_rewrite" not in state.artifacts, "禁用时不得创建该键"
    assert state.status.get("prompt_rewrite") != "reconfirm", "禁用时不参与标记"
    assert state.status.get("prompt_rewrite", "pending") == "pending"
    assert [s["id"] for s in state.artifacts["reference_generation"]["scenes"]] == ["seg_01_01"]
    assert state.status["reference_generation"] == "reconfirm"


# ───────────────────────── 4. 阶段化确认与响应形状 ─────────────────────────


def test_continue_with_stage_only_confirms_that_stage():
    sid = _session()
    _full_pipeline(sid)
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]
    workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})
    assert _state(sid).current_stage == WorkflowStage.SCRIPT_GENERATION

    response = workflow_engine._continue_workflow(sid, "reference_generation")

    state = _state(sid)
    assert response["status"] == "ready"
    assert response["next_stage"] == "prompt_rewrite", "H3 启用时参考图的后继是提示词改写"
    assert state.status["reference_generation"] == "completed"
    assert state.status["script_generation"] == "completed"
    assert state.status["storyboard"] == "reconfirm", "其它阶段状态不被误改"
    assert state.status["video_generation"] == "reconfirm"
    assert state.current_stage == WorkflowStage.REFERENCE_GENERATION

    # 不传 stage 时仍按 current_stage（旧客户端兼容）
    fallback = workflow_engine._continue_workflow(sid)
    assert fallback["next_stage"] in ("prompt_rewrite", "video_generation", "post_production")
    assert _state(sid).status["reference_generation"] == "completed"


def test_continue_rejects_illegal_stage_without_breaking():
    sid = _session()
    _full_pipeline(sid)

    response = workflow_engine._continue_workflow(sid, "not_a_stage")

    # 非法 stage 回退 current_stage：分镜已完成 → 可继续
    assert response["status"] in ("ready", "completed", "waiting")
    assert _state(sid).status["storyboard"] in ("completed", "reconfirm")


def test_patch_response_shape_is_authoritative():
    sid = _session()
    _full_pipeline(sid)
    characters = copy.deepcopy(_art(sid, "script_generation")["characters"])
    characters = [item for item in characters if item["name"] == "甲"]

    result = workflow_engine.update_artifact(sid, "script_generation", {"characters": characters})

    assert result["status"] == "ok"
    assert result["artifact"]["characters"] == characters or [
        item["name"] for item in result["artifact"]["characters"]
    ] == ["甲"]
    status_map = result["status_map"]
    assert status_map["storyboard"] == "reconfirm"
    assert set(status_map) >= set(STATUS_KEYS)
    assert result["stale_storyboards"] == [] and result["stale_final_videos"] is False


# ────────────────────── 5. 审核后的回归（P0/P1 修复点） ──────────────────────


def test_stopped_and_waiting_stages_are_not_flipped_to_reconfirm():
    """`stopped`/`waiting` 现场 MUST 不被本次变更标为 reconfirm：判据用重算前的基线。"""
    sid = _session()
    _full_pipeline(sid)
    # 用户停住了参考图，视频阶段因缺选择处于 waiting（分镜保持 completed）
    _seed(sid, statuses={"reference_generation": "stopped", "video_generation": "waiting"})
    kept = [i for i in _art(sid, "script_generation")["characters"] if i["name"] == "甲"]

    workflow_engine.update_artifact(sid, "script_generation", {"characters": kept})

    status = _state(sid).status
    # 基线取重算前的现场：这些阶段不得因本次剧本变更被标为待重新确认
    # （`_recalculate_all_statuses` 本身会把 stopped/waiting 归一为产物结果，属既有行为）
    assert status["reference_generation"] != "reconfirm", status
    assert status["video_generation"] != "reconfirm", status
    assert status["storyboard"] == "reconfirm", "原本 completed 的阶段仍要标上"
    assert status["character_design"] == "reconfirm"


def test_repeated_body_edits_accumulate_stale_episodes():
    """连续改两集正文：集级过期详情取并集，不得被第二次的覆盖式赋值冲掉。"""
    sid = _session()
    _full_pipeline(sid)
    _seed(sid, storyboard=_storyboard({1: ["seg_01_01"], 2: ["seg_02_01"]}))
    _seed(sid, statuses={"storyboard": "completed", "reference_generation": "completed",
                        "video_generation": "completed", "post_production": "completed"})
    episodes = copy.deepcopy(_art(sid, "script_generation")["episodes"])
    episodes.append({"episode_number": 2, "act_title": "第2集", "content": "第二集正文"})

    workflow_engine.update_artifact(sid, "script_generation", {"episodes": episodes})
    first = (_state(sid).stage_progress.get("storyboard") or {}).get("stale_episodes")
    episodes[0]["content"] = "甲：正文又改了一次"
    workflow_engine.update_artifact(sid, "script_generation", {"episodes": episodes})
    second = (_state(sid).stage_progress.get("storyboard") or {}).get("stale_episodes")

    assert 2 in first, first
    assert second == sorted(set(first) | {1}), (first, second)


def test_continue_clears_stale_details_and_always_returns_status_map():
    """确认无论原状态是什么都清掉该阶段过期详情，且每个分支都回状态表。"""
    sid = _session()
    _full_pipeline(sid)
    episodes = copy.deepcopy(_art(sid, "script_generation")["episodes"])
    episodes[0]["content"] = "甲：正文手改"
    workflow_engine.update_artifact(sid, "script_generation", {"episodes": episodes})
    assert (_state(sid).stage_progress.get("storyboard") or {}).get("stale_episodes") == [1]

    # 先确认上游阶段（原状态是 reconfirm），再确认一个本来就 waiting 的阶段
    ready = workflow_engine._continue_workflow(sid, "storyboard")
    assert ready["status"] in ("ready", "completed")
    assert "status_map" in ready
    assert "stale_episodes" not in (_state(sid).stage_progress.get("storyboard") or {})

    # 拒绝继续的分支（阶段还在执行）同样必须带状态表，前端才能同步视图
    _seed(sid, statuses={"video_generation": "running"})
    rejected = workflow_engine._continue_workflow(sid, "video_generation")
    assert rejected["status"] == "waiting", rejected
    assert isinstance(rejected.get("status_map"), dict) and rejected["status_map"]
    assert _state(sid).status["video_generation"] == "running", "拒绝继续不得改写执行中阶段"


def test_revision_apply_marks_downstream_through_execute_stage(monkeypatch):
    """验证 `_finish_stage` 的 `revision_applied` 接线（而不是只测收尾函数本身）。"""
    sid = _session()
    _full_pipeline(sid)

    async def apply_revision(_agent, input_data, _intervention):
        source = (input_data.get("_session_artifacts") or {}).get("script_generation") or {}
        payload = copy.deepcopy(source)
        payload["session_id"] = input_data.get("session_id")
        payload["characters"] = [
            item for item in payload.get("characters", []) if item.get("name") == "甲"
        ]
        return {"payload": payload, "stage_completed": True, "revision_applied": True}

    monkeypatch.setitem(
        workflow_engine.agent_factories, WorkflowStage.SCRIPT_GENERATION,
        lambda: ScriptedAgent(apply_revision),
    )
    state = workflow_engine.get_state(sid)
    asyncio.run(workflow_engine.execute_stage(
        state, WorkflowStage.SCRIPT_GENERATION, {"session_id": sid},
        intervention={"action": "apply_revision"},
    ))

    status = _state(sid).status
    assert [c["name"] for c in _art(sid, "character_design")["characters"]] == ["甲"]
    assert status["storyboard"] == "reconfirm" and status["reference_generation"] == "reconfirm"
    assert status["post_production"] != "reconfirm"


def test_revision_apply_path_does_not_mark_stopped_stage(monkeypatch):
    """应用修订路径与手动路径同口径：`stopped` 现场不得被误标为 reconfirm。

    回归点：`_finish_stage` 的 `revision_applied` 分支曾经漏传重算前的 `baseline`。
    """
    sid = _session()
    _full_pipeline(sid)
    _seed(sid, statuses={"reference_generation": "stopped"})

    async def apply_revision(_agent, input_data, _intervention):
        source = (input_data.get("_session_artifacts") or {}).get("script_generation") or {}
        payload = copy.deepcopy(source)
        payload["session_id"] = input_data.get("session_id")
        payload["characters"] = [
            item for item in payload.get("characters", []) if item.get("name") == "甲"
        ]
        return {"payload": payload, "stage_completed": True, "revision_applied": True}

    monkeypatch.setitem(
        workflow_engine.agent_factories, WorkflowStage.SCRIPT_GENERATION,
        lambda: ScriptedAgent(apply_revision),
    )
    state = workflow_engine.get_state(sid)
    asyncio.run(workflow_engine.execute_stage(
        state, WorkflowStage.SCRIPT_GENERATION, {"session_id": sid},
        intervention={"action": "apply_revision"},
    ))

    status = _state(sid).status
    assert status["reference_generation"] != "reconfirm", status
    assert status["storyboard"] == "reconfirm", "同一次变更里已执行的阶段仍要标上"


def test_prune_guards_bad_payload_and_unknown_episodes():
    """新产物形状非法不得“全集算孤儿”；不存在的集号不写进过期详情。"""
    sid = _session()
    _seed(
        sid,
        storyboard=_storyboard({1: ["seg_01_01", "seg_01_02"]}),
        reference={"scenes": [
            {"id": seg, "episode": 1, "selected": "", "versions": [], "status": "done"}
            for seg in ("seg_01_01", "seg_01_02")
        ]},
        statuses={"storyboard": "completed", "reference_generation": "completed"},
    )

    def apply_bad(state) -> None:
        workflow_engine._prune_downstream_items_for_episodes(
            state, {1}, _storyboard({1: ["seg_01_01", "seg_01_02"]}), None,
        )

    workflow_engine._mutate(sid, apply_bad)
    assert [s["id"] for s in _art(sid, "reference_generation")["scenes"]] == [
        "seg_01_01", "seg_01_02"
    ], "拿不到新分镜时不得误删条目引用"

    def apply_unknown(state) -> None:
        workflow_engine._prune_downstream_items_for_episodes(
            state, {9}, _storyboard({1: ["seg_01_01", "seg_01_02"]}), _storyboard({1: ["seg_01_01"]}),
        )

    workflow_engine._mutate(sid, apply_unknown)
    progress = (_state(sid).stage_progress.get("reference_generation") or {}).get("stale_episodes")
    assert progress != [9], progress


def test_explicit_null_characters_clears_stage2_items():
    """剧本侧显式 `characters: null` 与 [] 同口径：不能指纹判变更而重建跳过。"""
    sid = _session()
    script = _script(char_names=("甲",))
    script["characters"][0]["character_id"] = "char_甲"
    _seed(sid, script=script, stage2=_stage2([("char_甲", "甲")], []),
          statuses={"character_design": "completed"})

    workflow_engine.update_artifact(sid, "script_generation", {"characters": None})

    assert _art(sid, "character_design")["characters"] == []


def test_structure_fingerprint_rejects_unknown_scope():
    with pytest.raises(ValueError):
        script_revision.structure_fingerprint(_script(), scope="nope")