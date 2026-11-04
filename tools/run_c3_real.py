#!/usr/bin/env python3
"""C3 真实请求对照（单样本，不生成图片/视频）。

用法（容器内）：
  cd /app && python3 /tmp/run_c3_real.py --out /tmp/c3_real
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from core.agents.prompt_rewrite_agent import (
    PromptRewriteAgent,
    _extract_prompt,
    _validate_h3_prompt,
)
from models.llm_client import LLM
from prompts.loader import load_prompt


def _segment() -> dict:
    return {
        "id": "seg_c3_01",
        "name": "C3 单样本",
        "index": 1,
        "episode": 1,
        "shot_content": "小陈站在银行大堂中央，白天自然光",
        "duration": 5,
        "characters": ["小陈"],
        "location": "银行大堂",
    }


def _sections(user_text: str) -> dict:
    return PromptRewriteAgent._render_sections(
        authoritative="【权威事实】\n银行大堂白天营业，小陈穿深色制服",
        shot_content="小陈站在大堂中央，白天自然光",
        user_section=f"【用户修改】\n{user_text}",
        continuity_summaries=[],
        grounding_desc="首帧：小陈站在白天营业的银行大堂，窗外明亮",
    )


def _run_case(agent: PromptRewriteAgent, name: str, user_text: str, model: str) -> dict:
    mode = "I2VA"
    segment = _segment()
    sections = _sections(user_text)
    request = agent._build_request_text(segment, mode, sections)
    system = load_prompt("prompt_rewrite", "system", "zh")
    guide = agent._load_guide(mode)
    full_prompt = (
        f"{system}\n\n===== 改写指南（严格遵循）=====\n{guide}\n\n"
        f"===== 本次请求 =====\n{request}"
    )
    started = datetime.now(timezone.utc).isoformat()
    raw = LLM().query(full_prompt, model=model, safe_content=False)
    rewritten = _extract_prompt(raw)
    violations = _validate_h3_prompt(rewritten, mode, segment["duration"])

    lowered = rewritten.lower()
    first_ref = lowered.find("at 0.00 seconds")
    opening = lowered[:first_ref + 260] if first_ref >= 0 else lowered[:500]
    judgement = {
        "opening_snippet": opening[:260],
        "first_opening_has_night": "night" in opening or "夜晚" in opening,
        "night_after_offset": (
            "night" in lowered[first_ref + len("at 0.00 seconds"):]
            if first_ref >= 0 else "night" in lowered
        ),
        "night_mentions": lowered.count("night") + rewritten.count("夜晚"),
        "forbidden_hits": [word for word in ("cinematic", "beautiful") if word in lowered],
        "validator_violations": violations,
    }
    return {
        "case": name,
        "model": model,
        "temperature": "not transmitted by LLM.query (Config.H3_REWRITE_TEMPERATURE is reserved)",
        "started_at": started,
        "segment": segment,
        "mode": mode,
        "system_prompt": system,
        "guide": guide,
        "request": request,
        "full_prompt": full_prompt,
        "raw_output": raw,
        "extracted_prompt": rewritten,
        "judgement": judgement,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/c3_real")
    args = parser.parse_args()
    model = str(Config.H3_REWRITE_LLM_MODEL or Config.LLM_MODEL or "").strip()
    if not model:
        raise SystemExit("无可用 LLM 模型配置")

    agent = PromptRewriteAgent()
    cases = [
        _run_case(
            agent, "fact_vs_revision",
            "把时间改为夜晚，灯光变暖；首帧保持白天不变",
            model,
        ),
        _run_case(
            agent, "forbidden_words",
            "把时间改为夜晚，加入 cinematic 慢镜头和 beautiful 光影",
            model,
        ),
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"c3_real_{stamp}.json"
    path.write_text(
        json.dumps({"model": model, "cases": cases}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {path}")
    for case in cases:
        j = case["judgement"]
        print(
            f"[{case['case']}] opening_night={j['first_opening_has_night']} "
            f"night_after_offset={j['night_after_offset']} night_mentions={j['night_mentions']} "
            f"forbidden_hits={j['forbidden_hits']} violations={len(j['validator_violations'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
