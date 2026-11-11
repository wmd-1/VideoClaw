"""金标回归：五模式官方句式样例在确定性校验器下必须零违规。"""
from __future__ import annotations

import json
from pathlib import Path

from core.agents.prompt_rewrite_agent import _validate_h3_prompt

CASES = Path(__file__).resolve().parent / "fixtures" / "h3_rewrite" / "cases.json"


def test_golden_cases_pass_deterministic_validation():
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert {case["mode"] for case in cases} == {"T2VA", "I2VA", "FL2VA", "L2VA", "Ref2VA"}
    for case in cases:
        violations = _validate_h3_prompt(
            case["prompt"], case["mode"], case["duration"],
            picture_count=case["picture_count"],
        )
        assert violations == [], f"{case['name']}: {violations}"


def test_golden_cases_stay_in_repository():
    assert CASES.is_file()
    assert "tmp_verify" not in str(CASES)
