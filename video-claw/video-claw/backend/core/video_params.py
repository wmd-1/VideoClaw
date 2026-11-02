"""视频有效时长的单点解析（Q2）。

优先级：会话覆盖 > 分镜时长 > 默认 8；再按所选视频模型的
`capabilities.duration{min,max}` 夹取。H3 改写与视频生成必须消费同一结果，
不得各自夹取（否则改写目标时长与实际请求会不一致）。

夹取造成数值变化时返回可读的 adjustment 文案，供阶段进度/产物透出；
只有能力区间本身非法时才抛错。
"""
from __future__ import annotations

from typing import Any, List, Tuple

DEFAULT_DURATION = 8
# 仅在模型未声明 duration 能力时使用；与既有 H3 硬编码区间保持一致
FALLBACK_RANGE = (4, 15)


def _positive_int(value: Any) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def duration_range(model: str) -> Tuple[int, int]:
    """所选视频模型声明的时长区间；未声明回退 4~15。"""
    from models.config_model import get_model_config

    capabilities = (get_model_config(model or "") or {}).get("capabilities") or {}
    contract = capabilities.get("duration") or {}
    try:
        low = int(contract.get("min", FALLBACK_RANGE[0]))
        high = int(contract.get("max", FALLBACK_RANGE[1]))
    except (TypeError, ValueError):
        return FALLBACK_RANGE
    if low <= 0 or high < low:
        raise ValueError(f"视频模型 {model} 的 duration 能力区间非法：{contract}")
    return low, high


def resolve_effective_duration(
    segment_duration: Any = None,
    session_override: Any = None,
    model: str = "",
) -> Tuple[int, List[str]]:
    """返回 (effective_duration, adjustments)。"""
    override = _positive_int(session_override)
    from_segment = _positive_int(segment_duration)
    base = override or from_segment or DEFAULT_DURATION
    source = "会话覆盖" if override else ("分镜时长" if from_segment else "默认值")

    low, high = duration_range(model)
    effective = max(low, min(high, base))
    adjustments: List[str] = []
    if effective != base:
        adjustments.append(
            f"时长 {base}s（{source}）按模型能力夹取为 {effective}s（{low}~{high}s）"
        )
    return effective, adjustments


def duration_source(segment_duration: Any = None, session_override: Any = None) -> str:
    """解析来源（仅用于展示/诊断，不参与计算）。"""
    if _positive_int(session_override):
        return "session"
    if _positive_int(segment_duration):
        return "segment"
    return "default"


__all__ = [
    "DEFAULT_DURATION",
    "FALLBACK_RANGE",
    "duration_range",
    "duration_source",
    "resolve_effective_duration",
]
