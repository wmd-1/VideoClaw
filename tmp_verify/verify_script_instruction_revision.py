# -*- coding: utf-8 -*-
"""C1 script-instruction-revision 验证（mock LLM，容器内执行）。"""

import asyncio
import copy
import json

from core import script_revision
from core.agents.script_agent import ScriptWriterAgent
from core.orchestrator import WorkflowEngine, WorkflowState

PASS = 0
FAILED = []


def check(name, condition):
    global PASS
    if condition:
        PASS += 1
        print("PASS", name)
    else:
        FAILED.append(name)
        print("FAIL", name)


def make_artifact():
    return {
        "title": "测试剧本",
        "logline": "一句话梗概",
        "meta": {"film_type": "宣传", "subject_mode": "人物故事"},
        "characters": [
            {"character_id": "char_1", "name": "小陈", "description": "男，30岁，黑色短发，深色西装", "species": "人类"},
            {"character_id": "char_2", "name": "小周", "description": "女，28岁，马尾，灰色套装", "species": "人类"},
        ],
        "settings": [
            {"setting_id": "set_1", "name": "支行办公室", "description": "开放工位，日光灯，木质桌面"},
        ],
        "episodes": [
            {
                "episode_number": 1,
                "act_title": "第一集",
                "content": "第1集-第1场 日 内 支行办公室\n人物：小陈\n<action>小陈整理材料\n小陈：这份报告太多了。\n",
            },
            {
                "episode_number": 2,
                "act_title": "第二集",
                "content": "第2集-第1场 日 内 支行办公室\n人物：小陈\n<action>小陈看着屏幕\n小陈：终于完成了。\n",
            },
        ],
    }


def make_input(artifact, extra_artifacts=None):
    artifacts = {"script_generation": artifact}
    if extra_artifacts:
        artifacts.update(extra_artifacts)
    return {
        "session_id": "verify-c1",
        "llm_model": "mock-model",
        "_session_artifacts": artifacts,
    }


def make_agent(responses):
    agent = ScriptWriterAgent()
    calls = {"count": 0}

    def fake_query(llm, prompt, image_urls=[], model="", safe_content=True, task_id=None, web_search=False):
        index = min(calls["count"], len(responses) - 1)
        calls["count"] += 1
        return responses[index]

    agent._cancellable_query = fake_query
    return agent, calls


def run_revise(agent, artifact, intervention, extra_artifacts=None):
    return asyncio.run(agent.process(make_input(artifact, extra_artifacts), intervention))


def run_apply(agent, artifact, intervention, extra_artifacts=None):
    return asyncio.run(agent.process(make_input(artifact, extra_artifacts), intervention))


def full_response(e1="新第1集正文", e2="新第2集正文", characters=None, settings=None):
    base = make_artifact()
    return json.dumps(
        {
            "summary": "按方向修订",
            "episodes": [
                {"episode_number": 1, "act_title": "第一集", "content": e1},
                {"episode_number": 2, "act_title": "第二集", "content": e2},
            ],
            "characters": characters if characters is not None else base["characters"],
            "settings": settings if settings is not None else base["settings"],
        },
        ensure_ascii=False,
    )


def single_response(content="单集新正文"):
    return json.dumps(
        {
            "summary": "改第2集",
            "episode": {"episode_number": 2, "act_title": "第二集", "content": content},
        },
        ensure_ascii=False,
    )


# ── 1. 全剧修订成功 ──
artifact = make_artifact()
agent, _ = make_agent([full_response()])
result = run_revise(agent, artifact, {"action": "revise_script", "instruction": "加快节奏", "scope": "all"})
preview = result["payload"]["pending_revision"]
check(
    "全剧修订成功",
    result["requires_intervention"] is True
    and preview["episodes"][0]["content"] == "新第1集正文"
    and preview["scope"] == "all",
)

# ── 2. 单集修订成功 ──
artifact = make_artifact()
agent, _ = make_agent([single_response()])
result = run_revise(
    agent, artifact, {"action": "revise_script", "instruction": "第二集结尾更明确", "scope": "episode", "episode_number": 2}
)
preview = result["payload"]["pending_revision"]
check(
    "单集修订成功",
    preview["episode"]["content"] == "单集新正文"
    and result["payload"]["episodes"][0]["content"] == artifact["episodes"][0]["content"],
)

# ── 3. 单集越界校验拒绝 ──
tampered = [copy.deepcopy(ep) for ep in make_artifact()["episodes"]]
tampered[0]["content"] = "被模型篡改的第1集"
try:
    script_revision.assert_untouched_episodes(make_artifact()["episodes"], tampered, 2)
    check("单集越界校验拒绝", False)
except ValueError:
    check("单集越界校验拒绝", True)

# ── 4. 全剧集号不一致重试后拒绝 ──
bad = json.dumps({"summary": "缺集", "episodes": [{"episode_number": 1, "act_title": "一", "content": "x"}], "characters": [], "settings": []}, ensure_ascii=False)
agent, calls = make_agent([bad, bad])
try:
    run_revise(agent, make_artifact(), {"action": "revise_script", "instruction": "x", "scope": "all"})
    check("全剧集号不一致重试后拒绝", False)
except ValueError:
    check("全剧集号不一致重试后拒绝", calls["count"] == 2)

# ── 5. 空修改方向 ──
agent, _ = make_agent([full_response()])
try:
    run_revise(agent, make_artifact(), {"action": "revise_script", "instruction": "   ", "scope": "all"})
    check("空修改方向拒绝", False)
except ValueError:
    check("空修改方向拒绝", True)

# ── 6. 超长剧本拒绝 ──
big = make_artifact()
big["episodes"] = [{"episode_number": 1, "act_title": "一", "content": "x" * 61000}]
agent, _ = make_agent([full_response()])
try:
    run_revise(agent, big, {"action": "revise_script", "instruction": "x", "scope": "all"})
    check("超长剧本拒绝", False)
except ValueError as exc:
    check("超长剧本拒绝", "上限" in str(exc))

# ── 7. 预览不落盘 ──
artifact = make_artifact()
before = copy.deepcopy(artifact)
agent, _ = make_agent([full_response()])
result = run_revise(agent, artifact, {"action": "revise_script", "instruction": "加快节奏", "scope": "all"})
check(
    "预览不落盘",
    result["payload"]["episodes"] == before["episodes"] and artifact == before,
)

# ── 8/12/13. 应用成功 + 产物无 revision_applied + 派生标注被清除 ──
with_annotations = make_artifact()
with_annotations["line_annotations"] = [{"line": 1, "type": "heading"}]
with_annotations["scene_spans"] = [{"setting": "支行办公室", "from_line": 1, "to_line": 5}]
with_annotations["episodes"][0]["line_start"] = 1
with_annotations["episodes"][0]["line_end"] = 5
agent, _ = make_agent([full_response()])
revision_extra = {
    "storyboard": {
        "episodes": [{"episode_number": 1, "segments": [{"segment_id": "s1"}]}]
    }
}
preview_payload = run_revise(
    agent,
    with_annotations,
    {"action": "revise_script", "instruction": "加快节奏", "scope": "all"},
    extra_artifacts=revision_extra,
)["payload"]
applied = run_apply(
    agent,
    preview_payload,
    {"action": "apply_revision", "confirm_removals": True, "confirm_overwrites": True},
    extra_artifacts=revision_extra,
)
check(
    "应用成功",
    applied["stage_completed"] is True
    and applied["payload"]["episodes"][0]["content"] == "新第1集正文"
    and applied["payload"]["episodes"][1]["content"] == "新第2集正文",
)
check(
    "产物不含 revision_applied",
    applied["revision_applied"] is True and "revision_applied" not in applied["payload"],
)
check(
    "修订后派生标注被清除",
    "line_annotations" not in applied["payload"]
    and "scene_spans" not in applied["payload"]
    and all("line_start" not in ep and "line_end" not in ep for ep in applied["payload"]["episodes"]),
)
check(
    "应用后历史含 stale_storyboards",
    applied["payload"]["revision_history"][-1].get("stale_storyboards") == [1],
)

# ── 9/14. 取消与 PATCH 归一 ──
engine = WorkflowEngine()

cancel_state = WorkflowState("verify-cancel")
cancel_state.artifacts = {"script_generation": copy.deepcopy(preview_payload)}
cancel_before = copy.deepcopy(cancel_state.artifacts["script_generation"]["episodes"])
engine._apply_artifact_update(cancel_state, "script_generation", {"pending_revision": None})
check(
    "取消不改产物且无空键",
    "pending_revision" not in cancel_state.artifacts["script_generation"]
    and cancel_state.artifacts["script_generation"]["episodes"] == cancel_before,
)

meta_state = WorkflowState("verify-meta-patch")
meta_state.artifacts = {"script_generation": copy.deepcopy(preview_payload)}
engine._apply_artifact_update(meta_state, "script_generation", {"title": "新标题"})
check(
    "元信息 PATCH 保留预览",
    "pending_revision" in meta_state.artifacts["script_generation"]
    and meta_state.artifacts["script_generation"]["title"] == "新标题",
)

body_state = WorkflowState("verify-body-patch")
body_state.artifacts = {"script_generation": copy.deepcopy(preview_payload)}
engine._apply_artifact_update(
    body_state, "script_generation", {"episodes": preview_payload["episodes"]}
)
check(
    "正文 PATCH 清除预览",
    "pending_revision" not in body_state.artifacts["script_generation"],
)

# ── 10. 指纹不一致拒绝 ──
stale_payload = copy.deepcopy(preview_payload)
stale_payload["episodes"][0]["content"] = "外部修改过的正文"
try:
    run_apply(agent, stale_payload, {"action": "apply_revision", "confirm_removals": True, "confirm_overwrites": True})
    check("指纹不一致拒绝", False)
except ValueError as exc:
    check("指纹不一致拒绝", "过期" in str(exc) or "变化" in str(exc))

# ── 11. 修订历史追加与 20 条上限 ──
history_artifact = make_artifact()
history_artifact["revision_history"] = [
    {"instruction": f"old{i}", "scope": "all"} for i in range(20)
]
agent, _ = make_agent([full_response("h1", "h2")])
history_preview = run_revise(
    agent, history_artifact, {"action": "revise_script", "instruction": "补充历史", "scope": "all"}
)["payload"]
history_applied = run_apply(
    agent, history_preview, {"action": "apply_revision", "confirm_removals": True, "confirm_overwrites": True}
)
history = history_applied["payload"]["revision_history"]
check(
    "修订历史追加与上限",
    len(history) == 20
    and history[-1]["instruction"] == "补充历史"
    and history[0]["instruction"] == "old1",
)

# ── 同步契约 ──
sync_artifact = make_artifact()
stage_artifact = {
    "characters": [
        {"id": "char_1", "name": "小陈", "description": "阶段 2 手改描述", "selected": "img1.png", "versions": ["img1.png", "img0.png"], "status": "done"},
        {"id": "char_old", "name": "老李", "description": "旧角色", "selected": "old.png", "versions": ["old.png"], "status": "done"},
    ],
    "settings": [
        {"id": "set_1", "name": "支行办公室", "description": "阶段 2 场景", "selected": "bg1.png", "versions": ["bg1.png"], "status": "done"},
    ],
}
sync_state = WorkflowState("verify-sync")
sync_state.artifacts = {
    "script_generation": copy.deepcopy(sync_artifact),
    "character_design": copy.deepcopy(stage_artifact),
    "storyboard": {"episodes": [{"episode_number": 1, "segments": [{"segment_id": "s1"}]}]},
}
engine._rebuild_stage_items_from_script(sync_state)
chars = {c["name"]: c for c in sync_state.artifacts["character_design"]["characters"]}
check(
    "同名复用 ID 与资产",
    chars["小陈"]["id"] == "char_1"
    and chars["小陈"]["selected"] == "img1.png"
    and len(chars["小陈"]["versions"]) == 2,
)
check("新增条目 id 等于 character_id", chars["小周"]["id"] == "char_2")
check("新增条目 pending", chars["小周"]["status"] == "pending" and chars["小周"]["selected"] == "")
check("删除条目移出产物", "老李" not in chars)

removed = script_revision.removed_items(
    sync_artifact["characters"],
    [{"name": "小陈"}],
    [{"id": "char_2", "name": "小周", "versions": ["zhou.png"], "selected": "zhou.png"}],
)
removal_agent, _ = make_agent(
    [
        json.dumps(
            {
                "summary": "删除小周",
                "episodes": sync_artifact["episodes"],
                "characters": [sync_artifact["characters"][0]],
                "settings": sync_artifact["settings"],
            },
            ensure_ascii=False,
        )
    ]
)
removal_preview = run_revise(
    removal_agent, sync_artifact, {"action": "revise_script", "instruction": "删掉小周", "scope": "all"}
)["payload"]
try:
    run_apply(removal_agent, removal_preview, {"action": "apply_revision", "confirm_overwrites": True})
    removal_rejected = False
except ValueError:
    removal_rejected = True
check(
    "删除项确认",
    len(removed) == 1
    and removed[0]["name"] == "小周"
    and removed[0]["asset_count"] == 1
    and removal_rejected,
)

edited = script_revision.user_edited_items(
    sync_artifact["characters"],
    sync_artifact["characters"],
    [{"id": "char_1", "name": "小陈", "description": "用户手改过的描述", "selected": "img1.png"}],
)
check("手改描述可见", len(edited) == 1 and edited[0]["name"] == "小陈")

stale_hit = script_revision.stale_storyboards(
    [1, 2], {"episodes": [{"episode_number": 1, "segments": [{"segment_id": "s1"}]}]}
)
stale_miss = script_revision.stale_storyboards(
    [2], {"episodes": [{"episode_number": 1, "segments": [{"segment_id": "s1"}]}]}
)
stale_none = script_revision.stale_storyboards([], {"episodes": [{"episode_number": 1, "segments": [{"segment_id": "s1"}]}]})
check(
    "stale_storyboards 三种场景",
    stale_hit == [1] and stale_miss == [] and stale_none == [],
)

print(f"\nTOTAL PASS={PASS} FAIL={len(FAILED)}")
if FAILED:
    print("FAILED:", FAILED)
    raise SystemExit(1)
print("ALL PASS")
