# 任务 6.1 补充：真实外部会话多轮复用（单条重生成/修订走同一外部会话）
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

config.Config.DESIGN_AGENT_ENABLED = True
config.Config.DESIGN_AGENT_BASE_URL = "http://host.docker.internal:8848"
config.Config.DESIGN_AGENT_LOGIN_NAME = "videoclaw"

STORYBOARD = {
    "episodes": [{"episode_number": 1, "segments": [
        {"segment_id": "seg_01_01", "total_duration": 8,
         "shots": [{"content": "A wet grey tabby cat walks slowly through a rain-soaked neon alley at night", "duration": 8}]},
    ]}]
}

# 复用上一脚本创建的真实外部会话与产物
PREV_SESSION = "6c177c14-5937-4a9b-a690-38d99789d548"
PREV_PROMPT = open("/tmp/prev_prompt.txt").read().strip() if False else None

agent = PromptRewriteAgent()
agent.set_progress_callback(lambda *a, **k: None)

# 直接重新提交一次拿到全新产物（或复用上一会话：改为传入 prev artifacts）
prev_art = {"session_id": PREV_SESSION, "items": [{
    "id": "seg_01_01", "status": "done",
    "rewritten_prompt": "integrated_multimodal_description: previous version",
    "versions": [], "duration": 8, "input_mode": "T2VA", "name": "第1集-片段1", "index": 1,
}]}

result = asyncio.run(agent.process({
    "session_id": "e2e-test",
    "_session_artifacts": {"storyboard": copy.deepcopy(STORYBOARD), "prompt_rewrite": copy.deepcopy(prev_art)},
    "_session_meta": {},
}, intervention={"revise_items": [{"id": "seg_01_01", "instruction": "把猫换成戴斗笠的忍者，并加入樱花飘落"}]}))

payload = result["payload"]
item = payload["items"][0] if payload.get("items") else {}
check("1 修订复用同一外部会话", payload.get("session_id") == PREV_SESSION, str(payload.get("session_id")))
check("2 修订结果更新且非空", item.get("status") == "done" and len(item.get("rewritten_prompt") or "") > 50)
check("3 版本历史保留", len(item.get("versions") or []) >= 2)
has_ninja = "ninja" in (item.get("rewritten_prompt") or "").lower()
has_sakura = "sakura" in (item.get("rewritten_prompt") or "").lower() or "cherry blossom" in (item.get("rewritten_prompt") or "").lower()
check("4 修改意见生效（忍者/樱花入文）", has_ninja and has_sakura)
print("\n--- 修订结果预览（前 300 字）---")
print((item.get("rewritten_prompt") or "")[:300])

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("REAL MULTI-TURN PASSED")
