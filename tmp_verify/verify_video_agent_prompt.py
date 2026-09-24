# 任务 3.1 验证：VideoDirectorAgent 优先消费改写结果，缺失/失败回退现有拼装
import sys

sys.path.insert(0, "/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


from core.agents.video_agent import VideoDirectorAgent

agent = VideoDirectorAgent()

SEG = {"segment_id": "seg_01_01", "shots": [{"content": "A cat walks", "duration": 5}]}
STYLE = "cinematic"
REWRITE_ART = {"session_id": "ext", "items": [
    {"id": "seg_01_01", "status": "done", "rewritten_prompt": "H3 REWRITTEN PROMPT"},
    {"id": "seg_01_02", "status": "done", "rewritten_prompt": ""},
    {"id": "seg_01_03", "status": "failed", "rewritten_prompt": "SHOULD NOT BE USED"},
    {"id": "seg_01_04", "status": "pending", "rewritten_prompt": "SHOULD NOT BE USED"},
]}

# 有改写结果（done 且非空）→ 使用改写文本
p1 = agent._final_prompt(SEG, STYLE, None, video_data=None, prompt_rewrite_art=REWRITE_ART, seg_id="seg_01_01")
check("1 优先使用改写结果", p1 == "H3 REWRITTEN PROMPT", repr(p1[:40]))

# 无 prompt_rewrite 产物 → 回退拼装
p2 = agent._final_prompt(SEG, STYLE, None, video_data=None, prompt_rewrite_art=None, seg_id="seg_01_01")
check("2 无改写产物回退拼装", p2 == agent._assemble_prompt(SEG, STYLE, None) and "分镜列表：" in p2)

# 空产物 / 条目空文本 / failed / pending → 回退拼装
fallback = agent._assemble_prompt(SEG, STYLE, None)
for name, art, seg_id in [
    ("3 空产物回退", {}, "seg_01_01"),
    ("4 改写文本为空回退", REWRITE_ART, "seg_01_02"),
    ("5 failed 条目回退", REWRITE_ART, "seg_01_03"),
    ("6 pending 条目回退", REWRITE_ART, "seg_01_04"),
]:
    p = agent._final_prompt(SEG, STYLE, None, video_data=None, prompt_rewrite_art=art, seg_id=seg_id)
    check(name, p == fallback)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
