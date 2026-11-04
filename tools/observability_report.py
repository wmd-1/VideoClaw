#!/usr/bin/env python3
"""观测汇总与保留清理（D1）。

用法：
  python3 tools/observability_report.py --dir data/observability --cleanup
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path


def _load_records(directory: Path) -> list:
    records = []
    for path in glob.glob(str(directory / "*.jsonl")):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            continue
    return records


def _cleanup(directory: Path, days: int, max_total_bytes: int) -> dict:
    cutoff = time.time() - days * 86400
    deleted = []
    for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path.name)
        except OSError:
            continue

    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    total = sum(path.stat().st_size for path in files)
    for path in files:
        if total <= max_total_bytes:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
            deleted.append(path.name)
        except OSError:
            continue
    return {"deleted_files": deleted, "deleted_count": len(deleted)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="data/observability")
    parser.add_argument("--backend", default="video-claw/video-claw/backend")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-total-bytes", type=int, default=1_073_741_824)
    parser.add_argument("--input-warn-chars", type=int, default=None)
    parser.add_argument("--input-hard-chars", type=int, default=None)
    parser.add_argument("--execution-warn-chars", type=int, default=None)
    parser.add_argument("--execution-hard-chars", type=int, default=None)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    backend = Path(args.backend).resolve()
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from core.observability import summarize_records

    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    records = _load_records(directory)
    summary = summarize_records(
        records,
        input_warn_chars=args.input_warn_chars,
        input_hard_chars=args.input_hard_chars,
        execution_warn_chars=args.execution_warn_chars,
        execution_hard_chars=args.execution_hard_chars,
    )
    if args.cleanup:
        summary["cleanup"] = _cleanup(directory, args.days, args.max_total_bytes)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"records={len(records)} dropped={summary['dropped_events']}")
        for name, item in summary["by_kind"].items():
            print(f"kind={name} calls={item['calls']}")
        for name, item in summary["by_stage"].items():
            print(
                f"stage={name} calls={item['calls']} ok={item['ok']} failed={item['failed']} "
                f"retries={item['retries']} cache_hits={item['cache_hits']} "
                f"p50={item['p50_ms']} p95={item['p95_ms']} "
                f"input_p50={item['input_chars_p50']} input_p95={item['input_chars_p95']} "
                f"input_max={item['input_chars_max']} over_warn={item['input_over_warn']} "
                f"over_hard={item['input_over_hard']}"
            )
        executions = sorted(
            summary["by_execution"].items(),
            key=lambda pair: pair[1]["input_chars_sum"], reverse=True,
        )[:5]
        for execution_id, item in executions:
            print(
                f"execution={execution_id} calls={item['calls']} "
                f"input_sum={item['input_chars_sum']} input_max={item['input_chars_max']} "
                f"over_warn={item['over_warn']} over_hard={item['over_hard']} "
                f"total_over_warn={item['total_over_warn']} total_over_hard={item['total_over_hard']}"
            )
        if "cleanup" in summary:
            print(f"cleanup_deleted={summary['cleanup']['deleted_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
