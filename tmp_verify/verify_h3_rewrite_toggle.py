# 任务 6.3（开关回归）+ 2.1（配置三场景）验证：阶段注入开关、/api/stages 过滤、禁用态拒绝、旧键迁移提示
import asyncio
import copy
import importlib
import os
import subprocess
import sys

sys.path.insert(0, "/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "total_duration": 8, "shots": [{"content": "A cat walks", "duration": 8}]},
]}]}


def run_fresh(env_extra=None):
    """子进程加载 config（捕获迁移提示日志），返回 stdout。"""
    env = dict(os.environ)
    env.pop("VC_H3_REWRITE__ENABLE", None)
    env.update(env_extra or {})
    code = (
        "import sys; sys.path.insert(0, '/app')\n"
        "import logging; logging.basicConfig(level=logging.WARNING)\n"
        "from config import Config\n"
        "print('ENABLED', Config.H3_REWRITE_ENABLED)\n"
        "print('GROUNDING', Config.H3_REWRITE_GROUNDING_ENABLED)\n"
        "print('TEMP', Config.H3_REWRITE_TEMPERATURE)\n"
        "from core.orchestrator import get_stage_order, WorkflowStage\n"
        "print('ORDER', [s.value for s in get_stage_order()])\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


# ── 2.1 配置三场景 ──
# 场景 A：默认（无覆盖）
out_a = run_fresh()
check("A1 默认禁用", "ENABLED False" in out_a.stdout)
check("A2 默认 grounding 开", "GROUNDING True" in out_a.stdout)
check("A3 默认温度 0.3", "TEMP 0.3" in out_a.stdout)
check("A4 默认六阶段", "ORDER ['script_generation', 'character_design', 'storyboard', 'reference_generation', 'video_generation', 'post_production']" in out_a.stdout, out_a.stdout.strip().splitlines()[-1] if out_a.stdout else "")

# 场景 B：VC_H3_REWRITE__* 覆盖 → 七阶段
out_b = run_fresh({"VC_H3_REWRITE__ENABLE": "true", "VC_H3_REWRITE__GROUNDING_ENABLE": "false", "VC_H3_REWRITE__LLM_MODEL": "deepseek-v4-pro"})
check("B1 env 启用七阶段", "ORDER ['script_generation', 'character_design', 'storyboard', 'reference_generation', 'prompt_rewrite', 'video_generation', 'post_production']" in out_b.stdout)
check("B2 env 关闭 grounding", "GROUNDING False" in out_b.stdout)

# 场景 C：旧键 VC_DESIGN_AGENT__* → 迁移提示且不生效
out_c = run_fresh({"VC_DESIGN_AGENT__ENABLE": "true"})
check("C1 旧键不生效（仍禁用）", "ENABLED False" in out_c.stdout)
check("C2 旧键触发迁移提示", "VC_DESIGN_AGENT" in out_c.stderr and "VC_H3_REWRITE" in out_c.stderr, out_c.stderr.strip().splitlines()[-1][:80] if out_c.stderr else "")

# ── /api/stages 过滤（进程内直接调 router 函数） ──
import config
from api.routers.stages import _enabled_stages

config.Config.H3_REWRITE_ENABLED = False
stages_off = _enabled_stages()
check("D1 禁用态六阶段 order 连续", len(stages_off) == 6 and [s["order"] for s in stages_off] == list(range(1, 7))
      and all(s["id"] != "prompt_rewrite" for s in stages_off))
config.Config.H3_REWRITE_ENABLED = True
stages_on = _enabled_stages()
check("D2 启用态七阶段且位置正确", len(stages_on) == 7 and stages_on[4]["id"] == "prompt_rewrite"
      and [s["order"] for s in stages_on] == list(range(1, 8)))

# ── 禁用态拒绝 + 旧会话兼容 ──
config.Config.H3_REWRITE_ENABLED = False
from core.agents.prompt_rewrite_agent import PromptRewriteAgent
agent = PromptRewriteAgent()
agent.set_progress_callback(lambda *a, **k: None)
try:
    asyncio.run(agent.process({"session_id": "s", "_session_artifacts": {"storyboard": STORYBOARD}, "_session_meta": {}}))
    check("E1 禁用态明确拒绝", False)
except ValueError as exc:
    check("E1 禁用态明确拒绝", "VC_H3_REWRITE__ENABLE" in str(exc))

# 旧产物（含 session_id 外部会话键 + 无 grounding/continuity 字段）加载兼容
config.Config.H3_REWRITE_ENABLED = True
config.Config.H3_REWRITE_GROUNDING_ENABLED = False
agent._llm = type("L", (), {"query": staticmethod(lambda *a, **k: "```\nintegrated_multimodal_description: cat for 8 seconds.\noverall_soundscape: rain.\nnon_diegetic_music: None.\n```")})()
legacy = {"prompt_rewrite": {"session_id": "old-external-session", "items": [
    {"id": "seg_01_01", "status": "done", "rewritten_prompt": "OLD", "versions": [], "duration": 8, "input_mode": "T2VA"}]}}
result = asyncio.run(agent.process({
    "session_id": "s", "_session_artifacts": {"storyboard": copy.deepcopy(STORYBOARD), **legacy}, "_session_meta": {}
}))
item = result["payload"]["items"][0]
check("E2 旧产物兼容（重写成功且无外部会话键）", item["status"] == "done" and "session_id" not in result["payload"])
config.Config.H3_REWRITE_GROUNDING_ENABLED = True
config.Config.H3_REWRITE_ENABLED = False

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
