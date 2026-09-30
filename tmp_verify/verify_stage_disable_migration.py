# 任务 9.1 验证：阶段禁用迁移不死锁——启用态执行改写后关闭开关，会话恢复/继续可推进
import asyncio
import sys

sys.path.insert(0, "/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


import config
from core.orchestrator import WorkflowEngine, WorkflowStage

config.Config.H3_REWRITE_ENABLED = True
engine = WorkflowEngine()

# 1a 启用态：正常推进链
check("1a 启用态 reference→prompt_rewrite",
      engine._get_next_stage(WorkflowStage.REFERENCE_GENERATION) == WorkflowStage.PROMPT_REWRITE)
check("1b 启用态 prompt_rewrite→video",
      engine._get_next_stage(WorkflowStage.PROMPT_REWRITE) == WorkflowStage.VIDEO_GENERATION)

# 2 关闭开关后：current=prompt_rewrite（停点在改写阶段的会话）
config.Config.H3_REWRITE_ENABLED = False
check("2a 禁用态 prompt_rewrite 跳过至 video_generation",
      engine._get_next_stage(WorkflowStage.PROMPT_REWRITE) == WorkflowStage.VIDEO_GENERATION)
check("2b 禁用态其余阶段不受影响",
      engine._get_next_stage(WorkflowStage.REFERENCE_GENERATION) == WorkflowStage.VIDEO_GENERATION
      and engine._get_next_stage(WorkflowStage.VIDEO_GENERATION) == WorkflowStage.POST_PRODUCTION)

# 3 会话级演练：启用态建会话停在 prompt_rewrite waiting → 关闭开关 → continue 可推进
config.Config.H3_REWRITE_ENABLED = True
engine.create_session("mig-test", {"idea": "test"})
state = engine.get_state("mig-test")
state.current_stage = WorkflowStage.PROMPT_REWRITE
state.status["prompt_rewrite"] = "waiting"
state.artifacts["storyboard"] = {"episodes": []}

config.Config.H3_REWRITE_ENABLED = False
result = asyncio.run(engine.continue_workflow("mig-test"))
check("3a continue 不死锁且推进至 video_generation",
      result.get("status") == "ready" and result.get("next_stage") == "video_generation", str(result)[:120])

# 4 终点阶段无下一阶段（全序末尾，禁用态 post_production）
check("4 post_production 无下一阶段", engine._get_next_stage(WorkflowStage.POST_PRODUCTION) is None)

# 5 禁用阶段状态冻结：重算不把 prompt_rewrite 的 waiting 打回 pending
config.Config.H3_REWRITE_ENABLED = False
state.status["prompt_rewrite"] = "waiting"
engine._recalculate_all_statuses(state)
check("5b 禁用态重算冻结 waiting", state.status.get("prompt_rewrite") == "waiting",
      str(state.status.get("prompt_rewrite")))
# 重新启用后恢复重算
config.Config.H3_REWRITE_ENABLED = True
engine._recalculate_all_statuses(state)

# 清理
try:
    engine.delete_session("mig-test")
except Exception:  # noqa: BLE001
    pass
config.Config.H3_REWRITE_ENABLED = False

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
