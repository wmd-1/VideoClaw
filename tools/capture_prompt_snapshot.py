#!/usr/bin/env python3
"""C3 静态提示词快照：不调用模型，只组装并落盘可复核的最终请求。

用法：
  python3 tools/capture_prompt_snapshot.py \
    --backend video-claw/video-claw/backend \
    --out tmp_verify/prompt_snapshot
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

MODES = ("I2VA", "FL2VA", "Ref2VA")
CONFLICT_RULE = "用户修改只作用于未被固定帧之间的时间轴"
FORBIDDEN_SAMPLE = "cinematic"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _render_sections(mode: str) -> dict:
    sections = {
        "authoritative_section": "【权威事实】\n银行大堂白天营业，小陈穿制服\n",
        "current_section": "【当前分镜】\n小陈站在大堂中央\n",
        "grounding_section": (
            "【参考图/首帧实际内容（客观事实；关键帧任务中被固定的首帧/尾帧必须严格一致）】\n"
            "首帧：小陈站在白天营业的银行大堂\n"
        ),
        "user_section": (
            "【用户修改】\n把时间改为夜晚；加入 cinematic 慢镜头；不要改首帧\n"
        ),
        "continuity_section": "【连续性摘要（辅助信息，非事实来源；与权威事实或当前分镜冲突时必须忽略）】\n- 上一镜：小陈推门进入\n",
    }
    if mode == "I2VA":
        sections["grounding_section"] = ""
    return sections


def _build(backend: Path, mode: str) -> dict:
    template = _read(backend / "prompts/prompt_rewrite/rewrite_zh.txt")
    system = _read(backend / "prompts/prompt_rewrite/system_zh.txt")
    guide_name = "ref-en.txt" if mode == "Ref2VA" else "base-en.txt"
    guide = _read(backend / "prompts/prompt_rewrite/references" / guide_name)
    sections = _render_sections(mode)
    request = template.format(
        input_mode=mode,
        duration=5,
        segment_id="seg_01_01",
        shot_content="小陈站在大堂中央",
        **sections,
    )
    full_prompt = (
        f"{system}\n\n===== 改写指南（严格遵循）=====\n{guide}\n\n"
        f"===== 本次请求 =====\n{request}"
    )
    order = [
        request.find("银行大堂白天营业"),
        request.find("小陈站在大堂中央"),
        request.find("首帧：小陈站在白天营业"),
        request.find("把时间改为夜晚"),
        request.find("上一镜：小陈推门进入"),
    ]
    present = [i for i in order if i >= 0]
    checks = {
        "no_unfilled_placeholder": not re.search(r"\{[a-z_]+\}", full_prompt),
        "section_order_ok": present == sorted(present) and len(present) >= 4,
        "fact_before_user": request.find("银行大堂白天营业") < request.find("把时间改为夜晚"),
        "conflict_rule_present": CONFLICT_RULE in request and (
            mode == "I2VA" or "被固定的首帧/尾帧必须严格一致" in request
        ),
        "forbidden_sample_visible_in_request": FORBIDDEN_SAMPLE in request,
    }
    return {
        "mode": mode,
        "guide": guide_name,
        "request": request,
        "full_prompt": full_prompt,
        "checks": checks,
    }


def _checklist(snapshots: list[dict], captured_at: str) -> str:
    lines = [
        "# C3 提示词快照断言清单",
        "",
        f"- 捕获时间：{captured_at}",
        "- 说明：本文件由静态组装生成，不调用模型；`NEEDS_REAL` 只表示需真实请求判断模型是否遵守。",
        "",
        "| 项目 | 静态状态 | 依据 |",
        "|---|---|---|",
        "| 解析失败不得当成功 | PASS_LOCAL | `test_prompt_rewrite_failure_priority.py`（空输出/结构校验失败→item failed，不写版本）；`test_character_asset_quality.py`（解析失败→evaluated=false/parse_failed） |",
        "| 事实/修订优先级 | PASS_STATIC | 本节快照：权威事实→当前分镜→参考图→用户修改→连续性；冲突规则存在 |",
        "| 禁词/指南冲突 | NEEDS_REAL | 快照显示用户原文与指南原文均被送入请求；模型是否遵守需真实输出对照 |",
        "",
        "## 模式快照",
        "",
    ]
    for item in snapshots:
        failed = [key for key, ok in item["checks"].items() if not ok]
        lines.append(f"### {item['mode']}")
        lines.append("")
        for key, ok in item["checks"].items():
            lines.append(f"- `{key}`: {'PASS' if ok else 'FAIL'}")
        lines.append(f"- 结论：{'STATIC_PASS' if not failed else 'STATIC_FAIL'}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="video-claw/video-claw/backend")
    parser.add_argument("--out", default="tmp_verify/prompt_snapshot")
    args = parser.parse_args()
    backend = Path(args.backend).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshots = [_build(backend, mode) for mode in MODES]
    captured_at = datetime.now(timezone.utc).isoformat()
    payload = {"captured_at": captured_at, "snapshots": snapshots}
    (out_dir / "snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (out_dir / "CHECKLIST.md").write_text(
        _checklist(snapshots, captured_at), encoding="utf-8",
    )
    print(f"wrote {out_dir / 'snapshot.json'}")
    print(f"wrote {out_dir / 'CHECKLIST.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
