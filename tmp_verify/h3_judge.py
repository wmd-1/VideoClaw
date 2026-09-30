# 任务 6.2 LLM-judge 评分脚本（软门槛：质量回归基线与诊断，不作唯一硬门槛）
# 前置：已运行 h3_golden.py 生成 /tmp/h3_golden_output.json；config.yaml 已配置 LLM Key。
# 运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/h3_judge.py
import json
import os
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

import config  # noqa: E402
GOLDEN_PATH = "/tmp/h3_golden_output.json"
BASELINE_PATH = "/tmp/h3_judge_baseline.json"

if not os.path.exists(GOLDEN_PATH):
    print("FAIL: 请先运行 h3_golden.py 生成金标输出")
    sys.exit(1)

with open(GOLDEN_PATH, encoding="utf-8") as f:
    golden = json.load(f)

from models.llm_client import LLM  # noqa: E402

DIMENSIONS = ["structure", "specificity", "duration", "labels"]
JUDGE_TEMPLATE = """You are reviewing a MiniMax H3 video-generation prompt. Score each dimension 1-5 (integer).

Prompt to review (mode: {mode}, target duration: {duration}s):
---
{prompt}
---

Scoring rules:
- structure: required fields present and in the correct order for the mode
  (base modes: integrated_multimodal_description / overall_soundscape / non_diegetic_music;
   Ref2VA: subject_definitions / summary / retention_analysis / detailed_description /
   overall_soundscape / non_diegetic_music).
- specificity: concrete visible/audible details only; no abstract words
  (cinematic, beautiful, stunning, etc.), no plot summary.
- duration: the stated total duration matches {duration} seconds and timing notation is consistent.
- labels: reference labels (e.g. <Picture 1>) are consistent across all sections and none undefined.
  For base modes without references, give 5 if no labels are used.

Reply with ONLY a JSON object: {{"structure": n, "specificity": n, "duration": n, "labels": n}}
"""

llm = LLM()
baseline = {}
all_pass = True
for mode, data in golden.items():
    prompt_text = data.get("rewritten_prompt") or ""
    if not prompt_text:
        baseline[mode] = {"error": "empty prompt"}
        all_pass = False
        continue
    judge_prompt = JUDGE_TEMPLATE.format(mode=mode, duration=data.get("duration", 8), prompt=prompt_text)
    judge_model = (
        config.Config.H3_REWRITE_LLM_MODEL
        or config.Config.LLM_MODEL
        or ""
    )
    raw = llm.query(judge_prompt, model=judge_model, safe_content=False)
    try:
        start, end = raw.find("{"), raw.rfind("}") + 1
        scores = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        scores = {"error": f"judge 输出不可解析：{raw[:120]}"}
        all_pass = False
    baseline[mode] = scores
    flat = [scores.get(d) for d in DIMENSIONS if isinstance(scores.get(d), int)]
    dims_ok = len(flat) == 4 and all(v >= 4 for v in flat)
    print(f"[{'PASS' if dims_ok else 'SOFT-FAIL'}] {mode} judge: {scores}")

with open(BASELINE_PATH, "w", encoding="utf-8") as f:
    json.dump(baseline, f, ensure_ascii=False, indent=2)
print(f"\njudge 基线已保存：{BASELINE_PATH}")
print("定位说明：judge 为软门槛（质量回归基线/诊断），任一维显著下降（≥1 分）触发人工复核，不阻塞交付。")

if not all_pass:
    print("存在不可解析/空输出，请检查模型配置后重跑。")
    sys.exit(1)
print("JUDGE BASELINE RECORDED")
