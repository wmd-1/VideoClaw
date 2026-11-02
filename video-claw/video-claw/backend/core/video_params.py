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
# H3 协议硬区间（vLLM/SGLang cookbook：4–15s），不是 fallback；模型只能在这个区间
# 内进一步收窄
H3_DURATION_RANGE = (4, 15)
FALLBACK_RANGE = H3_DURATION_RANGE  # 兼容旧引用


def _positive_int(value: Any) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def duration_range(model: str) -> Tuple[int, int]:
    """H3 4~15 与所选模型声明能力的**交集**。

    - 模型未声明/名称为空 → 直接用 H3 区间（空名称不进入模糊匹配）；
    - 声明越界则收窄；交集为空 → 报错（不能生成违反 H3 协议的时长）。
    """
    low, high = H3_DURATION_RANGE
    if not str(model or "").strip():
        return low, high

    from models.config_model import get_model_config

    capabilities = (get_model_config(model) or {}).get("capabilities") or {}
    contract = capabilities.get("duration") or {}
    if not contract:
        return low, high
    try:
        declared_low = int(contract.get("min", low))
        declared_high = int(contract.get("max", high))
    except (TypeError, ValueError):
        return low, high
    if declared_low > declared_high:
        raise ValueError(f"视频模型 {model} 的 duration 能力区间非法：{contract}")

    low = max(low, declared_low)
    high = min(high, declared_high)
    if low > high:
        raise ValueError(
            f"视频模型 {model} 的时长能力与 H3 {H3_DURATION_RANGE[0]}~{H3_DURATION_RANGE[1]}s 无交集"
        )
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
    "H3_DURATION_RANGE",
    "duration_range",
    "duration_source",
    "resolve_effective_duration",
]
