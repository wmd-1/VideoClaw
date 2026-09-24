# 任务 6.1（真实外部服务）：VideoClaw 客户端 → Design Agent Platform 真实改写全链路
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

# 真实配置：指向宿主机网关（已在容器内验证连通）
config.Config.DESIGN_AGENT_ENABLED = True
config.Config.DESIGN_AGENT_BASE_URL = "http://host.docker.internal:8848"
config.Config.DESIGN_AGENT_LOGIN_NAME = "videoclaw"

STORYBOARD = {
    "episodes": [{"episode_number": 1, "segments": [
        {"segment_id": "seg_01_01", "total_duration": 8,
         "shots": [{"content": "A wet grey tabby cat walks slowly through a rain-soaked neon alley at night", "duration": 8}]},
    ]}]
}

agent = PromptRewriteAgent()
progress_log = []
agent.set_progress_callback(lambda phase, msg, pct, data=None: progress_log.append((pct, msg, data)))

result = asyncio.run(agent.process({
    "session_id": "e2e-test",
    "_session_artifacts": {"storyboard": copy.deepcopy(STORYBOARD)},
    "_session_meta": {},
}))

payload = result["payload"]
items = payload["items"]
item = items[0] if items else {}

check("1a 外部会话创建成功", bool(payload.get("session_id")), str(payload.get("session_id")))
check("1b 条目 done 且非空改写文本", item.get("status") == "done" and len(item.get("rewritten_prompt") or "") > 50)
check("1c input_mode=T2VA", item.get("input_mode") == "T2VA")
check("1d duration 夹取落库", item.get("duration") == 8)
check("1e 停点标记", result.get("requires_intervention") is True)
check("1f 进度事件含条目完成", any(d and d.get("asset_complete") for _, _, d in progress_log if d))
print("\n--- 改写结果预览（前 400 字）---")
print((item.get("rewritten_prompt") or "")[:400])
print("--- 外部会话 ID ---")
print(payload.get("session_id"))

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("REAL E2E PASSED")
