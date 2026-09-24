# 任务 2.1-2.5 验证：阶段开关注册、Agent 行为（stub 客户端）、跨阶段同步、单条重生成与修订
import asyncio
import copy
import sys

sys.path.insert(0, "/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


import config
from core.orchestrator import WorkflowEngine, WorkflowStage, WorkflowState, get_stage_order
from core.agents.prompt_rewrite_agent import PromptRewriteAgent, _clamp_duration, _extract_prompt

# ── 场景 1：阶段顺序（默认禁用 → 六阶段；启用 → 七阶段） ──
order_disabled = get_stage_order()
check("1a 默认禁用六阶段", [s.value for s in order_disabled] == [
    "script_generation", "character_design", "storyboard", "reference_generation", "video_generation", "post_production"
])
config.Config.DESIGN_AGENT_ENABLED = True
order_enabled = get_stage_order()
check("1b 启用七阶段", [s.value for s in order_enabled] == [
    "script_generation", "character_design", "storyboard", "reference_generation",
    "prompt_rewrite", "video_generation", "post_production"
])
engine = WorkflowEngine()
check("1c _get_next_stage 启用态 reference→prompt_rewrite",
      engine._get_next_stage(WorkflowStage.REFERENCE_GENERATION) == WorkflowStage.PROMPT_REWRITE)
check("1d _get_next_stage 启用态 prompt_rewrite→video",
      engine._get_next_stage(WorkflowStage.PROMPT_REWRITE) == WorkflowStage.VIDEO_GENERATION)
config.Config.DESIGN_AGENT_ENABLED = False
check("1e 禁用态 reference→video", engine._get_next_stage(WorkflowStage.REFERENCE_GENERATION) == WorkflowStage.VIDEO_GENERATION)

# GET /api/stages 过滤
from api.routers.stages import _enabled_stages
config.Config.DESIGN_AGENT_ENABLED = False
stages_disabled = _enabled_stages()
check("1f 禁用态 /api/stages 六阶段 order 连续",
      len(stages_disabled) == 6 and [s["order"] for s in stages_disabled] == list(range(1, 7))
      and all(s["id"] != "prompt_rewrite" for s in stages_disabled))
config.Config.DESIGN_AGENT_ENABLED = True
stages_enabled = _enabled_stages()
check("1g 启用态 /api/stages 七阶段且 prompt_rewrite 在参考图后",
      len(stages_enabled) == 7 and stages_enabled[4]["id"] == "prompt_rewrite"
      and [s["order"] for s in stages_enabled] == list(range(1, 8)))

# ── 场景 2：Agent 工具函数 ──
check("2a 时长夹取", _clamp_duration(2) == 4 and _clamp_duration(30) == 15 and _clamp_duration(8.4) == 8 and _clamp_duration(None) == 8)
check("2b 代码块提取", _extract_prompt("前言\n```\nFINAL PROMPT HERE\n```\n后记") == "FINAL PROMPT HERE")
check("2c 无代码块整段", _extract_prompt("PLAIN TEXT") == "PLAIN TEXT")
check("2d 空文本", _extract_prompt("") == "")

# ── 场景 3：Agent 全量执行（stub 客户端，全成功 / 部分失败） ──
config.Config.DESIGN_AGENT_ENABLED = True
config.Config.DESIGN_AGENT_BASE_URL = "http://fake:8001"

STORYBOARD = {
    "episodes": [
        {"episode_number": 1, "segments": [
            {"segment_id": "seg_01_01", "total_duration": 20, "shots": [{"content": "A cat walks in rain", "duration": 20}]},
            {"segment_id": "seg_01_02", "total_duration": 6, "shots": [{"content": "Sunrise over city", "duration": 6}]},
            {"segment_id": "seg_01_03", "total_duration": None, "shots": [{"content": "Close-up of eyes"}]},
        ]},
    ]
}


class FakeClient:
    def __init__(self, fail_ids=(), fail_reuse=False, existing=True):
        self.fail_ids = set(fail_ids)
        self.calls = []
        self.fail_reuse = fail_reuse
        self.existing = existing

    def get_session(self, sid):
        if self.fail_reuse:
            raise RuntimeError("gone")

    def close(self):
        pass

    def create_session(self):
        return "new-ext-session"

    def submit_turn(self, sid, text):
        self.calls.append((sid, text))
        seg_id = text.split("镜头编号：")[1].split("\n")[0] if "镜头编号：" in text else "?"
        if seg_id in self.fail_ids:
            raise RuntimeError("external boom")
        return f"```\nH3 PROMPT for {seg_id}\n```"


def make_input():
    return {
        "session_id": "s1",
        "_session_artifacts": {"storyboard": copy.deepcopy(STORYBOARD)},
        "_session_meta": {},
    }


agent = PromptRewriteAgent()
agent.set_progress_callback(lambda *a, **k: None)

# 3a 全成功
fake = FakeClient()
agent._create_client = lambda: fake
result = asyncio.run(agent.process(make_input()))
payload = result["payload"]
items = payload["items"]
check("3a1 返回外部会话 ID", payload["session_id"] == "new-ext-session")
check("3a2 三条目全部 done", all(i["status"] == "done" for i in items) and len(items) == 3)
check("3a3 id 与 segment 对应", [i["id"] for i in items] == ["seg_01_01", "seg_01_02", "seg_01_03"])
check("3a4 时长夹取落库（20→15，None→8）", items[0]["duration"] == 15 and items[2]["duration"] == 8)
check("3a5 input_mode=T2VA", all(i["input_mode"] == "T2VA" for i in items))
check("3a6 改写文本提取自代码块", items[0]["rewritten_prompt"] == "H3 PROMPT for seg_01_01")
check("3a7 版本记录 agent", any(v.get("source") == "agent" for v in items[0]["versions"]))
check("3a8 requires_intervention 停点", result.get("requires_intervention") is True and result.get("stage_completed") is True)
check("3a9 请求携带镜头描述与时长", "A cat walks in rain" in fake.calls[0][1] and "15 秒" in fake.calls[0][1])

# 3b 部分失败（条目级隔离）
fake2 = FakeClient(fail_ids={"seg_01_02"})
agent._create_client = lambda: fake2
result2 = asyncio.run(agent.process(make_input()))
items2 = result2["payload"]["items"]
statuses = {i["id"]: i["status"] for i in items2}
check("3b1 失败条目标记 failed 且带 error", statuses["seg_01_02"] == "failed" and items2[1].get("error"))
check("3b2 其余条目正常完成", statuses["seg_01_01"] == "done" and statuses["seg_01_03"] == "done")

# 3c 未配置服务 → 明确失败
config.Config.DESIGN_AGENT_ENABLED = False
try:
    asyncio.run(agent.process(make_input()))
    check("3c 未启用时明确失败", False)
except ValueError as exc:
    check("3c 未启用时明确失败", "未启用" in str(exc))
config.Config.DESIGN_AGENT_ENABLED = True
config.Config.DESIGN_AGENT_BASE_URL = ""
try:
    asyncio.run(agent.process(make_input()))
    check("3c2 未配置地址时明确失败", False)
except ValueError as exc:
    check("3c2 未配置地址时明确失败", "base_url" in str(exc))
config.Config.DESIGN_AGENT_BASE_URL = "http://fake:8001"

# ── 场景 4：外部会话复用与失效重建 ──
fake3 = FakeClient()
agent._create_client = lambda: fake3
prev_input = make_input()
prev_input["_session_artifacts"]["prompt_rewrite"] = {
    "session_id": "old-session",
    "items": [{"id": "seg_01_01", "status": "done", "rewritten_prompt": "OLD", "versions": []}],
}
result3 = asyncio.run(agent.process(prev_input))
check("4a 复用既有外部会话", fake3.calls and fake3.calls[0][0] == "old-session"
      and result3["payload"]["session_id"] == "old-session")

fake4 = FakeClient(fail_reuse=True)
agent._create_client = lambda: fake4
result4 = asyncio.run(agent.process(prev_input))
check("4b 复用失效后重建会话", result4["payload"]["session_id"] == "new-ext-session")

# ── 场景 5：单条重生成（regenerate_items） ──
fake5 = FakeClient()
agent._create_client = lambda: fake5
result5 = asyncio.run(agent.process(prev_input, intervention={"regenerate_items": ["seg_01_01"]}))
items5 = result5["payload"]["items"]
check("5a 只返回目标条目", [i["id"] for i in items5] == ["seg_01_01"])
check("5b 重生成结果更新", items5[0]["rewritten_prompt"] == "H3 PROMPT for seg_01_01" and items5[0]["status"] == "done")
try:
    asyncio.run(agent.process(prev_input, intervention={"regenerate_items": ["seg_09_09"]}))
    check("5c 不存在的条目报错", False)
except ValueError as exc:
    check("5c 不存在的条目报错", "seg_09_09" in str(exc))

# ── 场景 6：修订（revise_items，多轮上下文） ──
fake6 = FakeClient()
agent._create_client = lambda: fake6
result6 = asyncio.run(agent.process(prev_input, intervention={
    "revise_items": [{"id": "seg_01_01", "instruction": "改成雨天"}]
}))
items6 = result6["payload"]["items"]
check("6a 修订更新条目", bool(items6[0]["rewritten_prompt"]) and items6[0]["rewritten_prompt"] != "OLD" and items6[0]["status"] == "done")
check("6b 修订请求包含修改意见", any("改成雨天" in text for _, text in fake6.calls))
check("6c 修订追加版本", len(items6[0]["versions"]) >= 2)
config.Config.DESIGN_AGENT_ENABLED = False

# ── 场景 7：跨阶段同步（storyboard 变更 → prompt_rewrite 条目过期） ──
config.Config.DESIGN_AGENT_ENABLED = True
state = WorkflowState("s1")
state.artifacts["storyboard"] = copy.deepcopy(STORYBOARD)
state.artifacts["prompt_rewrite"] = {
    "session_id": "ext",
    "items": [
        {"id": "seg_01_01", "status": "done", "rewritten_prompt": "P1", "versions": []},
        {"id": "seg_01_02", "status": "failed", "rewritten_prompt": "", "versions": []},
    ],
}
engine._sync_artifacts_cross_stages(state, WorkflowStage.STORYBOARD, copy.deepcopy(STORYBOARD))
pr_items = {i["id"]: i["status"] for i in state.artifacts["prompt_rewrite"]["items"]}
check("7 storyboard 变更后 done 条目置 pending", pr_items["seg_01_01"] == "pending" and pr_items["seg_01_02"] == "failed")

# ── 场景 8：旧会话兼容（无 prompt_rewrite 键加载） ──
from core.orchestrator import WorkflowStage as WS
legacy = {"session_id": "legacy", "current_stage": "video_generation", "status": "completed",
          "artifacts": {"storyboard": {"episodes": []}}, "stages_completed": ["script_generation"]}
state2 = WorkflowState("legacy")
state2.current_stage = WS.VIDEO_GENERATION
loaded_status = legacy["status"]
stages_completed = legacy.get("stages_completed", [])
for stage in WS:
    if stage not in (WS.INIT, WS.COMPLETED):
        if stage.value in stages_completed:
            state2.status[stage.value] = "completed"
        elif stage.value == state2.current_stage.value:
            state2.status[stage.value] = loaded_status
        else:
            state2.status[stage.value] = "pending"
check("8 旧会话加载新阶段键为 pending", state2.status.get("prompt_rewrite") == "pending"
      and state2.status.get("video_generation") == "completed")
config.Config.DESIGN_AGENT_ENABLED = False

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
