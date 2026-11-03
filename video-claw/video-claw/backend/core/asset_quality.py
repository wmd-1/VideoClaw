"""资产评估结果的最小集落库（Q9）。

角色与参考图两个资产阶段共用同一份白名单与回退规则，避免各写一份：
- 只落 `evaluated` / `eval_status` / `selection` / `score`；
- 本轮没有可落库评估时回退已存评估，不把历史状态抹掉；
- VLM 原文（issues / suggestion 等）一律不入产物。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

EVALUATION_KEYS = ("evaluated", "eval_status", "selection", "score")


def asset_status(versions: Any, selected: str = "", *, stage_completed: bool = False) -> str:
    """资产条目状态唯一口径：有选中/版本为 done；阶段已跑完但无图为 failed，否则 pending。"""
    if selected or (versions or []):
        return "done"
    return "failed" if stage_completed else "pending"


def evaluation_payload(evaluation: Any) -> Optional[Dict[str, Any]]:
    """把评估结果收敛为可落库的最小集；非字典/空集返回 None。"""
    if not isinstance(evaluation, dict):
        return None
    view = {key: evaluation[key] for key in EVALUATION_KEYS if key in evaluation}
    if not view:
        return None
    if "evaluated" not in view:
        view["evaluated"] = None
    if "eval_status" not in view:
        view["eval_status"] = "unknown"
    return view


def existing_evaluations(artifact: Dict, container: str) -> Dict[str, Any]:
    """已存产物里的 evaluation（原样取用，构造 payload 时再统一收敛）。"""
    evaluations: Dict[str, Any] = {}
    items = artifact.get(container) if isinstance(artifact, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        value = item.get("evaluation")
        if isinstance(value, dict):
            evaluations[item["id"]] = value
    return evaluations


def evaluation_for(current: Dict[str, Any], existing: Dict[str, Any], asset_id: str) -> Optional[Dict[str, Any]]:
    """本轮有可落库评估则用本轮的，否则回退已存；都没有返回 None。"""
    view = evaluation_payload((current or {}).get(asset_id))
    if view is not None:
        return view
    return evaluation_payload((existing or {}).get(asset_id))


__all__ = ["EVALUATION_KEYS", "evaluation_payload", "existing_evaluations", "evaluation_for", "asset_status"]
