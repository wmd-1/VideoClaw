# 任务 6.2 验证（可自动化部分）：外部服务停机 / 启用但未配置地址 —— 真实网络、条目级失败隔离
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
from core.agents.prompt_rewrite_agent import PromptRewriteAgent

STORYBOARD = {
    "episodes": [{"episode_number": 1, "segments": [
        {"segment_id": "seg_01_01", "total_duration": 6, "shots": [{"content": "A", "duration": 6}]},
        {"segment_id": "seg_01_02", "total_duration": 6, "shots": [{"content": "B", "duration": 6}]},
    ]}]
}


def make_input():
    return {"session_id": "s1", "_session_artifacts": {"storyboard": copy.deepcopy(STORYBOARD)}, "_session_meta": {}}


agent = PromptRewriteAgent()
agent.set_progress_callback(lambda *a, **k: None)

# 场景 1：启用但未配置地址 → 明确失败，不发起网络请求
config.Config.DESIGN_AGENT_ENABLED = True
config.Config.DESIGN_AGENT_BASE_URL = ""
try:
    asyncio.run(agent.process(make_input()))
    check("1 未配置地址明确失败", False)
except ValueError as exc:
    check("1 未配置地址明确失败", "base_url" in str(exc))

# 场景 2：外部服务停机（不可达地址）→ 阶段快速失败（Design D9：不可达时阶段级快速失败，错误指明不可达）
import time
from models.design_agent_client import DesignAgentUnavailable

config.Config.DESIGN_AGENT_BASE_URL = "http://127.0.0.1:9"  # discard 端口，连接必被拒绝
start = time.time()
try:
    asyncio.run(agent.process(make_input()))
    check("2 不可达时阶段快速失败", False)
except DesignAgentUnavailable as exc:
    elapsed = time.time() - start
    check("2a 阶段快速失败且信息明确", "不可达" in str(exc), f"elapsed={elapsed:.1f}s msg={str(exc)[:60]}")
    check("2b 不长时间阻塞（< 30s）", elapsed < 30, f"elapsed={elapsed:.1f}s")

# 场景 2c：会话已建立后单轮不可达 → 条目级失败隔离（阶段不崩溃）
class PartialFailClient:
    def __init__(self):
        self.session_created = False

    def get_session(self, sid):
        raise RuntimeError("gone")

    def create_session(self):
        self.session_created = True
        return "new-ext-session"

    def submit_turn(self, sid, text):
        raise DesignAgentUnavailable("提示词改写服务不可达：POST /sessions/x/turns（ConnectError）")

    def close(self):
        pass


config.Config.DESIGN_AGENT_ENABLED = True
config.Config.DESIGN_AGENT_BASE_URL = "http://fake-but-enabled:8001"
agent2 = PromptRewriteAgent()
agent2.set_progress_callback(lambda *a, **k: None)
agent2._create_client = lambda: PartialFailClient()
result2 = asyncio.run(agent2.process(make_input()))
items2 = result2["payload"]["items"]
check("2c 单轮不可达时条目级隔离", result2.get("requires_intervention") is True
      and all(i["status"] == "failed" and "不可达" in i.get("error", "") for i in items2))

# 场景 3：恢复禁用 → 阶段不可执行（默认状态保护）
config.Config.DESIGN_AGENT_ENABLED = False
try:
    asyncio.run(agent.process(make_input()))
    check("3 禁用态明确失败", False)
except ValueError as exc:
    check("3 禁用态明确失败", "未启用" in str(exc))
config.Config.DESIGN_AGENT_ENABLED = False

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
