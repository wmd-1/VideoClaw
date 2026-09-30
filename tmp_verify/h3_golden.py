# 任务 6.1 金标集（硬门槛）：四模式真实改写 + deterministic validator 断言
# 前置：config.yaml 或 .env 已配置 h3_rewrite.llm_model（或会话 llm 模型）与对应 API Key。
# 运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/h3_golden.py
import asyncio
import copy
import json
import os
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


import config
from core.agents.prompt_rewrite_agent import (
    PromptRewriteAgent, _validate_h3_prompt, resolve_h3_mode,
)

config.Config.H3_REWRITE_ENABLED = True
config.Config.H3_REWRITE_GROUNDING_ENABLED = False  # 金标集关闭 grounding（纯文本改写，模式结构不变）

IMG1 = "/tmp/h3g/ref1.png"
IMG2 = "/tmp/h3g/ref2.png"
os.makedirs("/tmp/h3g", exist_ok=True)
with open(IMG1, "wb") as f:
    f.write(b"\x89PNG-golden-ref")
with open(IMG2, "wb") as f:
    f.write(b"\x89PNG-golden-ref-last")

# 四模式用例：mode → (video_generation_mode, segments, 选中参考图)
CASES = {
    "T2VA": ("first_frame", [
        {"segment_id": "g_t2va", "total_duration": 8,
         "shots": [{"content": "A wet grey tabby cat walks slowly through a rain-soaked neon alley at night", "duration": 8}]},
    ], {}),
    "I2VA": ("first_frame", [
        {"segment_id": "g_i2va", "total_duration": 8, "characters": ["林夏"],
         "shots": [{"content": "林夏在雨夜霓虹小巷中缓慢前行", "duration": 8}]},
    ], {"g_i2va": IMG1}),
    "FL2VA": ("start_end_frame", [
        {"segment_id": "g_fl2va_a", "total_duration": 8,
         "shots": [{"content": "A cat starts walking under a streetlight", "duration": 8}]},
        {"segment_id": "g_fl2va_b", "total_duration": 8,
         "shots": [{"content": "The cat arrives at a doorway", "duration": 8}]},
    ], {"g_fl2va_a": IMG1, "g_fl2va_b": IMG2}),
    "Ref2VA": ("reference", [
        {"segment_id": "g_ref2va", "total_duration": 10, "characters": ["林夏"],
         "shots": [{"content": "林夏转身离开，霓虹灯在身后闪烁", "duration": 10}]},
    ], {"g_ref2va": IMG1}),
}

CHARACTERS = {"characters": [{"name": "林夏", "description": "短发女孩，穿黑色风衣，左眉有疤"}]}

results = {}
for mode, (vgm, segments, selected_map) in CASES.items():
    agent = PromptRewriteAgent()
    agent.set_progress_callback(lambda *a, **k: None)
    arts = {
        "storyboard": {"episodes": [{"episode_number": 1, "segments": copy.deepcopy(segments)}]},
        "character_design": CHARACTERS,
        "reference_generation": {"scenes": [
            {"id": sid, "selected": path, "status": "done"} for sid, path in selected_map.items()
        ]},
    }
    result = asyncio.run(agent.process({
        "session_id": f"golden-{mode}",
        "llm_model": config.Config.H3_REWRITE_LLM_MODEL or "",
        "video_generation_mode": vgm,
        "_session_artifacts": arts,
        "_session_meta": {},
    }))
    item = result["payload"]["items"][0]
    violations = _validate_h3_prompt(item.get("rewritten_prompt") or "", mode, item.get("duration", 8))
    seg = segments[0]
    expected_mode = resolve_h3_mode(
        vgm,
        has_first_frame=bool(selected_map.get(seg["segment_id"])),
        has_last_frame=(vgm == "start_end_frame" and len(segments) > 1),
        has_reference_set=(vgm == "reference"),
    )
    check(f"{mode} 金标 validator 硬门槛", item["status"] == "done" and not violations,
          str(violations)[:100])
    check(f"{mode} 模式符合契约表", item["input_mode"] == expected_mode,
          f"got {item['input_mode']} expect {expected_mode}")
    results[mode] = {
        "input_mode": item.get("input_mode"),
        "duration": item.get("duration"),
        "rewritten_prompt": item.get("rewritten_prompt"),
        "violations": violations,
    }

config.Config.H3_REWRITE_GROUNDING_ENABLED = True

out_path = "/tmp/h3_golden_output.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n金标输出已保存：{out_path}（供 h3_judge.py 评分）")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("GOLDEN HARD GATE PASSED")
