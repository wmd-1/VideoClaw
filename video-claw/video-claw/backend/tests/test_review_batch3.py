"""第六轮报告第三批回归：R08 / R13 / R15 / R20。

只允许在既有后端容器内执行：
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \\
    /app/.venv/bin/python -m pytest /tmp/vc_tests -q
"""
from __future__ import annotations

import pytest

from api.dependencies import workflow_engine
from core import script_revision
from core.agents.script_agent import ScriptWriterAgent
from core.orchestrator import WorkflowStage

EPISODES_A = [{"episode_number": 1, "content": "正文 A", "title": "第一集"}]
EPISODES_B = [{"episode_number": 1, "content": "正文 B（用户手改）", "title": "第一集"}]


def headers(login: str) -> dict[str, str]:
    return {"X-User-Login-Name": login}


class FakeAgent:
    """最小 agent 桩：只在 execute_stage 里替身，不绕过被测的状态机。"""

    def __init__(self, on_process=None):
        self._on_process = on_process

    def set_cancellation_check(self, _fn):
        return None

    def set_progress_callback(self, _fn):
        return None

    async def process(self, input_data, intervention=None):
        if self._on_process:
            return self._on_process(input_data, intervention)
        return {"payload": {}, "requires_intervention": True, "stage_completed": False}


# ─────────────── R20：running 标记失败必须释放执行权 ───────────────


def test_r20_running_mark_failure_releases_active_session(session_dir, monkeypatch):
    import core.persistence_service as persistence_service

    workflow_engine.create_session("r20_sid", {}, owner="alice")
    state = workflow_engine.get_state("r20_sid")
    stage = WorkflowStage.PROMPT_REWRITE

    monkeypatch.setitem(workflow_engine.agent_factories, stage, lambda: FakeAgent())
    real_commit = persistence_service.commit_session_change

    def boom(**_kwargs):
        raise RuntimeError("db down")

    # 开始提交（running + 执行层 start）失败
    monkeypatch.setattr(persistence_service, "commit_session_change", boom)
    import asyncio

    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(workflow_engine.execute_stage(state, stage, {}))

    # 执行权、boot_id 与状态都必须回退，否则同进程再也跑不了这个会话
    assert "r20_sid" not in workflow_engine._active_sessions
    reloaded = workflow_engine.get_state("r20_sid")
    assert not reloaded.boot_id
    assert reloaded.status.get(stage.value) != "running"
    assert stage.value not in reloaded.executions

    # DB 恢复后同一进程可直接重试（不报 already running）
    monkeypatch.setattr(persistence_service, "commit_session_change", real_commit)
    result = asyncio.run(workflow_engine.execute_stage(reloaded, stage, {}))
    assert isinstance(result, dict)
    finished = workflow_engine.get_state("r20_sid")
    assert finished.executions[stage.value]["status"] == "completed"
    assert not finished.boot_id


# ─────────────── R08：预览不能回写旧正文 ───────────────


def test_r08_preview_rejected_when_body_changed_while_waiting(session_dir, monkeypatch):
    workflow_engine.create_session("r08_sid", {}, owner="alice")
    workflow_engine.update_artifact("r08_sid", "script_generation", {
        "episodes": EPISODES_A,
        "characters": [],
        "settings": [],
    })
    state = workflow_engine.get_state("r08_sid")
    snapshot = {
        "episodes": EPISODES_A,
        "characters": [],
        "settings": [],
        "pending_revision": {
            "instruction": "改一下",
            "scope": "all",
            "summary": "",
            "base_version": script_revision.content_fingerprint(
                {"episodes": EPISODES_A, "characters": [], "settings": []}
            ),
            "characters": [],
            "settings": [],
        },
    }
    stage = WorkflowStage.SCRIPT_GENERATION

    def process(_input, _intervention):
        # 预览等待期间用户在阶段 1 保存了正文 B
        workflow_engine.update_artifact("r08_sid", "script_generation", {"episodes": EPISODES_B})
        return {"payload": snapshot, "requires_intervention": True, "stage_completed": False}

    monkeypatch.setitem(workflow_engine.agent_factories, stage, lambda: FakeAgent(process))
    import asyncio

    with pytest.raises(ValueError, match="预览已过期"):
        asyncio.run(workflow_engine.execute_stage(state, stage, {}))

    artifact = workflow_engine.get_state("r08_sid").artifacts["script_generation"]
    assert artifact["episodes"] == EPISODES_B, "已保存的手改正文不能被预览回写"
    assert "pending_revision" not in artifact


# ─────────────── R13：预览后新增的阶段 2 手改需重新确认 ───────────────


def _script_artifact_with_preview(displayed=None, characters=None, settings=None):
    characters = characters or [{"name": "小陈", "description": "旧描述"}]
    settings = settings or []
    current = {"episodes": EPISODES_A, "characters": characters, "settings": settings}
    preview = {
        "instruction": "改角色",
        "scope": "all",
        "summary": "",
        "base_version": script_revision.content_fingerprint(current),
        "episodes": [{"episode_number": 1, "content": "修订后的正文", "title": "第一集"}],
        "characters": characters,
        "settings": settings,
        "removed_items": [],
        "user_edited_items": list(displayed or []),
    }
    artifact = dict(current)
    artifact["pending_revision"] = preview
    return artifact


def _conflict(name, desc, kind="character"):
    return {
        "kind": kind,
        "name": name,
        "id": "",
        "current_description": desc,
        "incoming_description": "新描述",
    }


def _apply_inputs(displayed=None, stage2_characters=None, characters=None,
                  settings=None, stage2_settings=None):
    return {
        "session_id": "r13_sid",
        "_session_artifacts": {
            "script_generation": _script_artifact_with_preview(displayed, characters, settings),
            "character_design": {
                "characters": stage2_characters
                or [{"name": "小陈", "description": "预览之后我手改的描述", "description_source": "user"}],
                "settings": stage2_settings or [],
            },
        },
    }


def test_r13_apply_returns_confirmation_instead_of_auto_confirming():
    """真实页面路径：前端不再自动带 confirm_overwrites，后端必须返回待确认预览。"""
    import asyncio

    result = asyncio.run(ScriptWriterAgent()._run_apply_revision(_apply_inputs(), {}))

    assert result["requires_intervention"] is True
    assert result["revision_needs_confirmation"] is True
    assert result["stage_completed"] is False
    preview = result["payload"]["pending_revision"]
    assert [item["name"] for item in preview["user_edited_items"]] == ["小陈"]
    # 未确认前不得覆盖：正文与角色描述仍是原值
    assert result["payload"]["characters"][0]["description"] == "旧描述"


def test_r13_confirmation_does_not_authorize_later_edits():
    """已展示过“小陈”，确认后“小周”又被手改：即使 confirm=true 也必须重新确认。"""
    import asyncio

    inputs = _apply_inputs(
        displayed=[_conflict("小陈", "预览之后我手改的描述")],
        characters=[{"name": "小陈", "description": "旧描述"}, {"name": "小周", "description": "青年女审查员"}],
        stage2_characters=[
            {"name": "小陈", "description": "预览之后我手改的描述", "description_source": "user"},
            {"name": "小周", "description": "确认之后才改的描述", "description_source": "user"},
        ],
    )
    result = asyncio.run(ScriptWriterAgent()._run_apply_revision(inputs, {"confirm_overwrites": True}))

    assert result.get("revision_applied") is not True
    assert result["requires_intervention"] is True and result["revision_needs_confirmation"] is True
    names = [item["name"] for item in result["payload"]["pending_revision"]["user_edited_items"]]
    assert "小周" in names and "小陈" in names


def test_r13_confirmation_refreshes_changed_same_name_description():
    """同名条目描述在确认后变化：必须用最新描述刷新差异，不能沿用旧快照放行。"""
    import asyncio

    inputs = _apply_inputs(
        displayed=[_conflict("小陈", "旧的手改")],
        stage2_characters=[{"name": "小陈", "description": "最新的手改"}],
    )
    result = asyncio.run(ScriptWriterAgent()._run_apply_revision(inputs, {"confirm_overwrites": True}))

    assert result.get("revision_applied") is not True
    items = result["payload"]["pending_revision"]["user_edited_items"]
    assert [item["current_description"] for item in items] == ["最新的手改"]


def test_r13_same_name_character_and_setting_do_not_collapse():
    """角色与场景同名：已展示场景 X 的确认，不能授权后来新增的角色 X 手改。"""
    import asyncio

    inputs = _apply_inputs(
        displayed=[_conflict("X", "场景手改", kind="setting")],
        characters=[{"name": "X", "description": "角色旧"}],
        settings=[{"name": "X", "description": "场景旧"}],
        stage2_characters=[{"name": "X", "description": "角色手改"}],
        stage2_settings=[{"name": "X", "description": "场景手改"}],
    )
    result = asyncio.run(ScriptWriterAgent()._run_apply_revision(inputs, {"confirm_overwrites": True}))

    assert result.get("revision_applied") is not True
    assert result["revision_needs_confirmation"] is True
    kinds = {
        (item["kind"], item["name"], item["current_description"])
        for item in result["payload"]["pending_revision"]["user_edited_items"]
    }
    assert ("character", "X", "角色手改") in kinds
    assert ("setting", "X", "场景手改") in kinds


def test_r13_apply_proceeds_when_confirmed_snapshot_matches(monkeypatch):
    import asyncio

    # 只验证确认逻辑：文件输出边界（RESULT_DIR/script/<sid>.json）不在本用例范围
    monkeypatch.setattr(ScriptWriterAgent, "_save_result", lambda *args, **kwargs: None)
    inputs = _apply_inputs(displayed=[_conflict("小陈", "预览之后我手改的描述")])
    result = asyncio.run(
        ScriptWriterAgent()._run_apply_revision(inputs, {"confirm_overwrites": True})
    )
    assert result["revision_applied"] is True
    assert "pending_revision" not in result["payload"]


def test_r13_apply_reads_preview_stripped_by_materialize(monkeypatch):
    """真实链路：物化按 PREVIEW_KEYS 剥离预览，应用修订仍须读到 pending_revision。

    regression：确认修订后固定报“没有待应用的修订预览”，前端进度停在 0%。
    """
    import asyncio

    monkeypatch.setattr(ScriptWriterAgent, "_save_result", lambda *args, **kwargs: None)
    current = {"episodes": EPISODES_A, "characters": [], "settings": []}
    preview = {
        "instruction": "把正文改掉",
        "scope": "all",
        "episode_number": None,
        "summary": "改正文",
        "base_version": script_revision.content_fingerprint(current),
        "episodes": EPISODES_B,
        "characters": [],
        "settings": [],
        "removed_items": [],
        "user_edited_items": [],
    }
    inputs = {
        "session_id": "r13_strip_sid",
        # 编排器物化后的 agent 视图：split_preview 已剥离 pending_revision
        "_session_artifacts": {"script_generation": dict(current)},
        # prepare_intervention_execution 的顶层快照仍带预览
        "episodes": EPISODES_A,
        "pending_revision": preview,
    }
    result = asyncio.run(
        ScriptWriterAgent()._run_apply_revision(
            inputs, {"confirm_removals": True, "confirm_overwrites": True}
        )
    )

    assert result["revision_applied"] is True
    assert result["payload"]["episodes"] == EPISODES_B
    assert "pending_revision" not in result["payload"]


def test_revise_reports_multi_stage_progress(monkeypatch):
    """修订期间必须上报多个阶段百分比：单点 20% 会让进度条长时间不动。"""
    import asyncio
    import json as jsonlib

    from models import llm_client

    revised = {
        "summary": "改好了",
        "episodes": [{"episode_number": 1, "content": "新正文", "act_title": "第一集"}],
    }

    class FakeLLM:
        def query(self, *args, **kwargs):
            return jsonlib.dumps(revised, ensure_ascii=False)

    monkeypatch.setattr(llm_client, "LLM", FakeLLM)
    events = []
    agent = ScriptWriterAgent()
    agent.set_progress_callback(
        lambda phase, step, percent, data=None: events.append((phase, step, percent))
    )
    inputs = {
        "session_id": "revise_sid",
        "llm_model": "fake-model",
        "_session_artifacts": {
            "script_generation": {"episodes": EPISODES_A, "characters": [], "settings": []}
        },
    }
    result = asyncio.run(
        agent._run_revise_script(inputs, {"instruction": "改正文", "scope": "all"})
    )

    percents = [percent for _phase, _step, percent in events]
    assert percents == sorted(percents), percents  # 单调递增：前端忽略回退值
    assert {5, 10, 20, 80, 92, 96, 100} <= set(percents), percents
    assert result["requires_intervention"] is True


def test_revise_retry_progress_does_not_regress(monkeypatch):
    """首次解析失败、重试后成功：进度不得从 90 跳回 88。

    regression：固定档位写死「解析 90 / 合并 88」，重试成功路径会倒挂；前端虽有
    取大兜底，但 overlay 与阶段快照仍会记录倒退值。
    """
    import asyncio
    import json as jsonlib

    from models import llm_client

    revised = {
        "summary": "改好了",
        "episodes": [{"episode_number": 1, "content": "新正文", "act_title": "第一集"}],
    }

    class FlakyLLM:
        def __init__(self):
            self.calls = 0

        def query(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return "模型没有返回 JSON"
            return jsonlib.dumps(revised, ensure_ascii=False)

    monkeypatch.setattr(llm_client, "LLM", FlakyLLM)
    percents = []
    agent = ScriptWriterAgent()
    agent.set_progress_callback(
        lambda phase, step, percent, data=None: percents.append(percent)
    )
    inputs = {
        "session_id": "revise_retry_sid",
        "llm_model": "fake-model",
        "_session_artifacts": {
            "script_generation": {"episodes": EPISODES_A, "characters": [], "settings": []}
        },
    }
    result = asyncio.run(
        agent._run_revise_script(inputs, {"instruction": "改正文", "scope": "all"})
    )

    assert result["requires_intervention"] is True
    assert percents == sorted(percents), percents
    assert {82, 88, 92, 96} <= set(percents) and percents[-1] == 100, percents


def test_revise_items_merges_partial_payload(session_dir, monkeypatch):
    """提示词按意见修订只返回目标条目：必须走条目合并，不能覆盖其他分镜。

    regression：非后台路径会用部分 items 整体替换阶段产物，其余分镜提示词丢失。
    """
    import asyncio

    sid = "pw_revise_items_sid"
    workflow_engine.create_session(sid, {}, owner="alice")
    workflow_engine.update_artifact(sid, "prompt_rewrite", {
        "items": [
            {"id": "seg_1_1", "status": "done", "rewritten_prompt": "旧提示词一"},
            {"id": "seg_1_2", "status": "done", "rewritten_prompt": "旧提示词二"},
        ]
    })

    def process(_input, _intervention):
        return {
            "payload": {"items": [{
                "id": "seg_1_1",
                "status": "done",
                "rewritten_prompt": "新提示词一",
                # 真实修订路径：拷贝上一版 versions 再追加 superseded 版本
                "versions": [
                    {"source": "user", "content": "旧提示词一"},
                    {"source": "superseded", "content": "旧提示词一"},
                ],
            }]},
            "requires_intervention": True,
        }

    stage = WorkflowStage.PROMPT_REWRITE
    monkeypatch.setitem(workflow_engine.agent_factories, stage, lambda: FakeAgent(process))
    state = workflow_engine.get_state(sid)
    asyncio.run(workflow_engine.execute_stage(
        state, stage, {},
        intervention={"revise_items": [{"id": "seg_1_1", "instruction": "改一下"}]},
    ))

    artifact = workflow_engine.get_state(sid).artifacts["prompt_rewrite"]
    assert [item["id"] for item in artifact["items"]] == ["seg_1_1", "seg_1_2"]
    assert artifact["items"][0]["rewritten_prompt"] == "新提示词一"
    assert artifact["items"][1]["rewritten_prompt"] == "旧提示词二"


def test_revise_backfills_item_ids(monkeypatch):
    """R-16：全剧修订时模型返回的角色/场景不带 ID，预览必须补上同名复用的主键。

    regression：无 ID 条目一旦落入剧本产物，阶段 2 按 character_id/setting_id 取数会
    整段丢条目，并报「未能从剧本中读取到角色或场景描述数据」。
    """
    import asyncio
    import json as jsonlib

    from models import llm_client

    old_chars = [{"character_id": "char_aaa111", "name": "小刘", "description": "柜员"}]
    old_settings = [{"setting_id": "set_bbb222", "name": "营业厅", "description": "大厅"}]
    revised = {
        "summary": "改正文",
        "episodes": [{"episode_number": 1, "content": "新正文", "act_title": "第一集"}],
        "characters": [
            {"name": "小刘", "description": "资深柜员"},
            {"name": "老张", "description": "新客户"},
        ],
        "settings": [{"name": "营业厅", "description": "大厅"}],
    }

    class FakeLLM:
        def query(self, *args, **kwargs):
            return jsonlib.dumps(revised, ensure_ascii=False)

    monkeypatch.setattr(llm_client, "LLM", FakeLLM)
    monkeypatch.setattr(ScriptWriterAgent, "_save_result", lambda *a, **k: None)
    agent = ScriptWriterAgent()
    script_art = {"episodes": EPISODES_A, "characters": old_chars, "settings": old_settings}
    inputs = {
        "session_id": "revise_ids_sid",
        "llm_model": "fake-model",
        "_session_artifacts": {"script_generation": script_art},
    }
    preview = asyncio.run(
        agent._run_revise_script(inputs, {"instruction": "改正文", "scope": "all"})
    )["payload"]["pending_revision"]

    # 同名复用修订前 ID（保住阶段 2 的选中/版本/资产），新增条目现场补齐
    assert preview["characters"][0]["character_id"] == "char_aaa111"
    new_id = preview["characters"][1]["character_id"]
    assert new_id and new_id != "char_aaa111"
    assert preview["settings"][0]["setting_id"] == "set_bbb222"
    # 模型原始输出与修订前产物都不能被改写：base_version 以修订前正文为准
    assert "character_id" not in revised["characters"][0]
    assert old_chars == [{"character_id": "char_aaa111", "name": "小刘", "description": "柜员"}]
    assert old_settings == [{"setting_id": "set_bbb222", "name": "营业厅", "description": "大厅"}]
    assert preview["characters"][0] is not old_chars[0]
    assert preview["base_version"] == script_revision.content_fingerprint(script_art)

    # 应用后产物仍带主键，阶段 2 能按 ID 读到角色与场景
    from core.agents.character_agent import CharacterDesignerAgent

    applied_inputs = {
        "session_id": "revise_ids_sid",
        "_session_artifacts": {"script_generation": dict(script_art)},
        "pending_revision": preview,
    }
    applied = asyncio.run(
        ScriptWriterAgent()._run_apply_revision(
            applied_inputs, {"confirm_removals": True, "confirm_overwrites": True}
        )
    )
    assert applied["revision_applied"] is True
    assert [c["character_id"] for c in applied["payload"]["characters"]] == ["char_aaa111", new_id]
    desc = CharacterDesignerAgent()._read_script_data({"_session_artifacts": {
        "script_generation": applied["payload"]
    }})
    assert set(desc["characters"]) == {"char_aaa111", new_id}
    assert set(desc["settings"]) == {"set_bbb222"}


def test_stage2_derives_ids_for_idless_script_items(session_dir):
    """R-16 兑底：已被旧版修订抹掉 ID 的产物，种子化与取数必须同源按名称派生主键。"""
    from core.agents.character_agent import CharacterDesignerAgent

    damaged = {
        "episodes": EPISODES_A,
        "characters": [{"name": "小刘", "description": "柜员"}],
        "settings": [{"name": "营业厅", "description": "大厅"}],
    }
    state = workflow_engine.create_session("seed_ids_sid", {}, owner="alice")
    state = workflow_engine.get_state("seed_ids_sid")
    state.artifacts["script_generation"] = damaged
    assert workflow_engine._seed_stage_items_from_script(state) is True

    data = CharacterDesignerAgent()._read_script_data(
        {"_session_artifacts": {"script_generation": damaged}}
    )
    seeded_ids = {item["id"] for item in state.artifacts["character_design"]["characters"]}
    seeded_setting_ids = {item["id"] for item in state.artifacts["character_design"]["settings"]}
    assert seeded_ids == set(data["characters"]), (seeded_ids, set(data["characters"]))
    assert seeded_setting_ids == set(data["settings"])
    assert data["characters"] and data["settings"], "不得因缺 ID 整段丢条目"


def test_single_episode_revision_backfills_item_ids(monkeypatch):
    """单集修订不动角色/场景，但必须顺带修好旧产物里缺失的主键。"""
    import asyncio
    import json as jsonlib

    from core.script_import import stable_id
    from models import llm_client

    episodes = [
        {"episode_number": 1, "act_title": "第一集", "content": "第一集正文"},
        {"episode_number": 2, "act_title": "第二集", "content": "第二集正文"},
    ]
    damaged_chars = [{"name": "小刘", "description": "柜员"}]
    damaged_settings = [{"name": "营业厅", "description": "大厅"}]
    revised = {
        "summary": "改第二集",
        "episode": {"episode_number": 2, "act_title": "第二集", "content": "第二集新正文"},
    }

    class FakeLLM:
        def query(self, *args, **kwargs):
            return jsonlib.dumps(revised, ensure_ascii=False)

    monkeypatch.setattr(llm_client, "LLM", FakeLLM)
    agent = ScriptWriterAgent()
    inputs = {
        "session_id": "revise_single_ids_sid",
        "llm_model": "fake-model",
        "_session_artifacts": {"script_generation": {
            "episodes": episodes,
            "characters": damaged_chars,
            "settings": damaged_settings,
        }},
    }
    preview = asyncio.run(agent._run_revise_script(
        inputs, {"instruction": "改第二集", "scope": "episode", "episode_number": 2}
    ))["payload"]["pending_revision"]

    assert preview["characters"][0]["character_id"] == stable_id("char", "小刘")
    assert preview["settings"][0]["setting_id"] == stable_id("set", "营业厅")
    # 修订前产物不能被补 ID 改写：base_version 仍以它计算
    assert "character_id" not in damaged_chars[0]
    assert preview["base_version"] == script_revision.content_fingerprint({
        "episodes": episodes, "characters": damaged_chars, "settings": damaged_settings,
    })


def test_ensure_item_ids_avoids_duplicate_derived_ids():
    """同名新增条目派生 ID 会碰撞：必须退回唯一主键。"""
    from core.script_import import stable_id

    items = [{"name": "小刘", "description": "甲"}, {"name": "小刘", "description": "乙"}]
    out = script_revision.ensure_item_ids([], items, "character_id", "char")
    ids = [item["character_id"] for item in out]
    assert ids[0] == stable_id("char", "小刘")
    assert ids[1] != ids[0] and ids[1]


def test_ensure_item_ids_reassigns_duplicate_incoming_ids():
    """模型给多个条目返回同一主键：保留首个，其余按名称重新分配。"""
    from core.script_import import stable_id

    items = [
        {"name": "小刘", "description": "甲", "character_id": "char_dup"},
        {"name": "老张", "description": "乙", "character_id": "char_dup"},
    ]
    out = script_revision.ensure_item_ids([], items, "character_id", "char")
    ids = [item["character_id"] for item in out]
    assert ids[0] == "char_dup"
    assert ids[1] == stable_id("char", "老张")
    assert len(set(ids)) == 2


# ─────────────── R15：正文手改后旧行标注必须失效 ───────────────


def test_r15_title_only_edit_keeps_annotations(client, session_dir):
    """仅改标题/原样保存：正文未变，标注与行区间必须保留。"""
    from core.script_import import annotate_lines

    workflow_engine.create_session("r15a_sid", {}, owner="alice")
    state = workflow_engine.get_state("r15a_sid")
    original = "小陈走进办公室，把材料放在桌上。\n小陈：材料放这儿。"
    annotations = annotate_lines(original.split("\n"), [])
    state.artifacts["script_generation"] = {
        "episodes": [{"episode_number": 1, "content": original, "line_start": 0, "line_end": 1}],
        "line_annotations": annotations,
        "scene_spans": [{"setting": "办公室", "from_line": 0, "to_line": 1}],
        "meta": {"original_text": original},
    }

    response = client.patch(
        "/api/project/r15a_sid/artifact/script_generation",
        headers=headers("alice"),
        json={"episodes": [{"episode_number": 1, "content": original, "title": "改了标题"}]},
    )
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert artifact["line_annotations"] == annotations
    assert artifact["scene_spans"] == [{"setting": "办公室", "from_line": 0, "to_line": 1}]
    assert artifact["episodes"][0]["line_start"] == 0
    assert artifact["meta"]["original_text"] == original


def test_r15_body_change_rebuilds_units_without_loss(client, session_dir):
    """正文真变化：重建行坐标后，动作/台词/占位都仍能进入分镜。"""
    from core.agents.storyboard_agent import StoryboardAgent
    from core.script_import import annotate_lines

    workflow_engine.create_session("r15b_sid", {}, owner="alice")
    state = workflow_engine.get_state("r15b_sid")
    original = "小陈走进办公室，把材料放在桌上。\n小陈：材料放这儿。"
    state.artifacts["script_generation"] = {
        "episodes": [{"episode_number": 1, "content": original, "line_start": 0, "line_end": 1}],
        "line_annotations": annotate_lines(original.split("\n"), []),
        "scene_spans": [],
        "meta": {"original_text": original},
    }

    new_lines = [
        "小陈推门进入会议室，把材料放在桌上。",
        "小陈：材料放这儿。",
        "【录屏预留位】",
    ]
    response = client.patch(
        "/api/project/r15b_sid/artifact/script_generation",
        headers=headers("alice"),
        json={"episodes": [{"episode_number": 1, "content": "\n".join(new_lines)}]},
    )
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    episode = artifact["episodes"][0]
    assert episode["line_start"] == 0 and episode["line_end"] == 2
    assert artifact["meta"]["original_text"] == "\n".join(new_lines)
    assert [item["type"] for item in artifact["line_annotations"]] == [
        "action",
        "dialogue",
        "placeholder",
    ]

    # 按分镜真实消费口径重放：动作/台词/占位都必须成为 unit，不得漏内容
    _, units = StoryboardAgent._annotate_episode_script(
        episode["content"],
        [],
        [],
        artifact["line_annotations"],
        artifact.get("scene_spans") or [],
        line_offset=episode["line_start"],
        baseline_line_count=len(artifact["meta"]["original_text"].split("\n")),
    )
    assert [unit["text"] for unit in units] == new_lines
    assert [unit["is_dialogue"] for unit in units] == [False, True, False]


CAST_SCRIPT = """剧本名称：贷审
角色设定：
小陈：客户经理，三十岁
小周：青年女审查员

【第一幕：贷审会会议室】
小陈走进会议室，把材料放下。
小陈：开始吧。
【第二幕：支行办公室】
小周：材料呢？"""
CAST_EDITED = CAST_SCRIPT.replace("小陈：开始吧。", "小陈：现在开始吧。")


def _seed_cast_script(session_id):
    from core.script_import import deterministic_annotations

    workflow_engine.create_session(session_id, {}, owner="alice")
    state = workflow_engine.get_state(session_id)
    lines = CAST_SCRIPT.split("\n")
    state.artifacts["script_generation"] = {
        "episodes": [{"episode_number": 1, "content": CAST_SCRIPT, "line_start": 0, "line_end": len(lines) - 1}],
        "line_annotations": deterministic_annotations(lines),
        "scene_spans": [],
        "settings": [{"name": "贷审会会议室"}, {"name": "支行办公室"}],
        "meta": {"original_text": CAST_SCRIPT},
    }


def _patch_episode(client, session_id, content, extra=None):
    payload = {"episodes": [{"episode_number": 1, "content": content}]}
    payload.update(extra or {})
    return client.patch(
        f"/api/project/{session_id}/artifact/script_generation",
        headers=headers("alice"),
        json=payload,
    )


def test_r15_body_change_keeps_cast_section_out_of_dialogue(client, session_dir):
    """重建标注必须复用角色设定区间识别，设定行不得被当成对白。"""
    _seed_cast_script("r15c_sid")
    response = _patch_episode(client, "r15c_sid", CAST_EDITED)
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    types = {item["line"]: item["type"] for item in artifact["line_annotations"]}
    assert types[2] == "cast_block" and types[3] == "cast_block"
    assert types[6] == "action" and types[7] == "dialogue"
    assert artifact["meta"]["original_text"] == CAST_EDITED


def test_r15_body_change_rebuilds_scene_spans(client, session_dir):
    """清空旧映射不够：必须按新正文重建幕/场到场景名的映射。"""
    _seed_cast_script("r15d_sid")
    response = _patch_episode(client, "r15d_sid", CAST_EDITED)
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    spans = {(item["setting"], item["from_line"]) for item in artifact["scene_spans"]}
    assert spans == {("贷审会会议室", 5), ("支行办公室", 8)}


NOISY_SCRIPT = """剧本名称：对公风控宣传片
角色设定：
小陈：男，32岁
小周：女，28岁

【第一幕：开场】
【画面描述】支行办公室，小陈整理材料。
【旁白（系统）】“风控的每个环节都有一位AI助理。”
小陈：材料太多了。
【第二幕：收尾】
【画面描述】小陈走出支行大厅。
小陈：效率提升了很多。"""
NOISY_EDITED = NOISY_SCRIPT.replace("小陈：材料太多了。", "小陈：材料仍然太多了。")


def test_r15_scene_desc_location_rebuilds_and_storyboard_uses_it(client, session_dir):
    """标题不写地点时，地点在【画面描述】里：重建必须命中，且分镜实际使用它。"""
    from core.agents.storyboard_agent import StoryboardAgent
    from core.script_import import deterministic_annotations

    workflow_engine.create_session("r15f_sid", {}, owner="alice")
    state = workflow_engine.get_state("r15f_sid")
    lines = NOISY_SCRIPT.split("\n")
    settings = [{"name": "支行办公室"}, {"name": "支行大厅"}]
    state.artifacts["script_generation"] = {
        "episodes": [{"episode_number": 1, "content": NOISY_SCRIPT, "line_start": 0, "line_end": len(lines) - 1}],
        "line_annotations": deterministic_annotations(lines),
        "scene_spans": [],
        "settings": settings,
        "meta": {"original_text": NOISY_SCRIPT},
    }

    response = _patch_episode(client, "r15f_sid", NOISY_EDITED)
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    spans = {(item["setting"], item["from_line"]) for item in artifact["scene_spans"]}
    assert spans == {("支行办公室", 5), ("支行大厅", 9)}

    # 继续走分镜消费：第二幕实际地点必须是支行大厅，而不是沿用上一幕
    episode = artifact["episodes"][0]
    _, units = StoryboardAgent._annotate_episode_script(
        episode["content"],
        [{"name": "小陈"}, {"name": "小周"}],
        settings,
        artifact["line_annotations"],
        artifact["scene_spans"],
        line_offset=episode["line_start"],
        baseline_line_count=len(artifact["meta"]["original_text"].split("\n")),
    )
    locations = {unit["line_number"]: unit["location"] for unit in units}
    assert locations.get(9) == "支行办公室"
    assert locations.get(12) == "支行大厅"


SPLIT_DESC_SCRIPT = """剧本名称：对公风控宣传片
角色设定：
小陈：男，32岁
小周：女，28岁

【第一幕：开场】
【画面描述】支行办公室，小陈整理材料。
小陈：材料太多了。
【第二幕：收尾】
【画面描述】
小陈走出支行大厅。
小陈：效率提升了很多。"""
SPLIT_DESC_EDITED = SPLIT_DESC_SCRIPT.replace("小陈：材料太多了。", "小陈：材料还是太多。")


def test_r15_split_line_scene_desc_updates_following_dialogue(client, session_dir):
    """【画面描述】单独成行、地点在下一行：描述段要续读，后续无地点对白归新场景。"""
    from core.agents.storyboard_agent import StoryboardAgent
    from core.script_import import deterministic_annotations

    workflow_engine.create_session("r15g_sid", {}, owner="alice")
    state = workflow_engine.get_state("r15g_sid")
    lines = SPLIT_DESC_SCRIPT.split("\n")
    settings = [{"name": "支行办公室"}, {"name": "支行大厅"}]
    state.artifacts["script_generation"] = {
        "episodes": [{"episode_number": 1, "content": SPLIT_DESC_SCRIPT, "line_start": 0, "line_end": len(lines) - 1}],
        "line_annotations": deterministic_annotations(lines),
        "scene_spans": [],
        "settings": settings,
        "meta": {"original_text": SPLIT_DESC_SCRIPT},
    }

    response = _patch_episode(client, "r15g_sid", SPLIT_DESC_EDITED)
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    spans = {(item["setting"], item["from_line"]) for item in artifact["scene_spans"]}
    assert spans == {("支行办公室", 5), ("支行大厅", 8)}

    episode = artifact["episodes"][0]
    _, units = StoryboardAgent._annotate_episode_script(
        episode["content"],
        [{"name": "小陈"}, {"name": "小周"}],
        settings,
        artifact["line_annotations"],
        artifact["scene_spans"],
        line_offset=episode["line_start"],
        baseline_line_count=len(artifact["meta"]["original_text"].split("\n")),
    )
    locations = {unit["line_number"]: unit["location"] for unit in units}
    # 描述段内的动作行与随后不含地点名的对白都应归到新场景
    assert locations.get(11) == "支行大厅"
    assert locations.get(12) == "支行大厅"


def test_r15_full_artifact_patch_cannot_restore_stale_derived(client, session_dir):
    """完整产物 PATCH 携带旧标注/旧原文时，必须被服务端重算结果覆盖。"""
    _seed_cast_script("r15e_sid")
    stale_annotations = [{"line": 0, "type": "heading"}]
    stale_spans = [{"setting": "旧地点", "from_line": 0, "to_line": 1}]
    response = _patch_episode(
        client,
        "r15e_sid",
        CAST_EDITED,
        extra={
            "line_annotations": stale_annotations,
            "scene_spans": stale_spans,
            "meta": {"original_text": "旧原文"},
        },
    )
    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert artifact["line_annotations"] != stale_annotations
    assert artifact["meta"]["original_text"] == CAST_EDITED
    assert {item["setting"] for item in artifact["scene_spans"]} == {"贷审会会议室", "支行办公室"}
