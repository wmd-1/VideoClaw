"""实例资格证据：配置修订 + 当前 boot + 管理员确认的不可变快照。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class QualificationEvidence:
    instance_id: str
    resource_key: str
    config_revision: str
    boot_id: str
    checked_at: float
    checked_by: str
    checks: Dict[str, bool] = field(default_factory=dict)
    confirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "resource_key": self.resource_key,
            "config_revision": self.config_revision,
            "boot_id": self.boot_id,
            "checked_at": self.checked_at,
            "checked_by": self.checked_by,
            "checks": dict(self.checks),
            "confirmed": bool(self.confirmed),
        }

    @classmethod
    def from_dict(cls, value: Any) -> Optional["QualificationEvidence"]:
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(
                instance_id=str(value.get("instance_id") or ""),
                resource_key=str(value.get("resource_key") or ""),
                config_revision=str(value.get("config_revision") or ""),
                boot_id=str(value.get("boot_id") or ""),
                checked_at=float(value.get("checked_at") or 0),
                checked_by=str(value.get("checked_by") or ""),
                checks={
                    str(key): bool(flag)
                    for key, flag in (value.get("checks") or {}).items()
                },
                confirmed=bool(value.get("confirmed")),
            )
        except (TypeError, ValueError):
            return None


def build_qualification_evidence(
    *,
    instance_id: str,
    resource_key: str,
    config_revision: str,
    boot_id: str,
    checked_by: str,
    checks: Mapping[str, bool],
    confirmed: bool,
    checked_at: float,
) -> QualificationEvidence:
    """禁用中的真实实例也可显式检查；占位地址应由调用方在发请求前拦截。"""
    if not instance_id or not resource_key:
        raise ValueError("instance_id/resource_key 不能为空")
    if not config_revision or not boot_id:
        raise ValueError("config_revision/boot_id 不能为空")
    if not checked_by:
        raise ValueError("checked_by 不能为空")
    if not confirmed:
        raise ValueError("资格证据必须由管理员显式确认")
    normalized_checks = {str(key): bool(flag) for key, flag in checks.items()}
    if not normalized_checks or not all(normalized_checks.values()):
        raise ValueError("资格检查必须全部通过")
    return QualificationEvidence(
        instance_id=instance_id,
        resource_key=resource_key,
        config_revision=config_revision,
        boot_id=boot_id,
        checked_at=float(checked_at),
        checked_by=checked_by,
        checks=normalized_checks,
        confirmed=True,
    )


def qualification_is_valid(
    evidence: Any,
    *,
    config_revision: str,
    boot_id: str,
    now: float,
    max_age_seconds: float = 86_400,
) -> bool:
    """缺证据、修订/boot 变化、过期或未确认均不视为就绪。"""
    parsed = (
        evidence
        if isinstance(evidence, QualificationEvidence)
        else QualificationEvidence.from_dict(evidence)
    )
    if parsed is None:
        return False
    if not parsed.confirmed or not parsed.checks or not all(parsed.checks.values()):
        return False
    if parsed.config_revision != config_revision or parsed.boot_id != boot_id:
        return False
    age = float(now) - parsed.checked_at
    if age < 0 or age > max_age_seconds:
        return False
    return True


def qualification_blockers(
    *,
    instance_id: str,
    resource_key: str,
    placeholder_issues: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """资格检查前的静态闸门；占位实例必须在发起任何 HTTP 前被拦住。"""
    blockers: list[dict[str, str]] = []
    if not instance_id:
        blockers.append({"field": "instance_id", "code": "missing", "reason": "实例 ID 为空"})
    if not resource_key:
        blockers.append({"field": "resource_key", "code": "missing", "reason": "资源键为空"})
    for issue in placeholder_issues or []:
        blockers.append(
            {
                "field": issue.get("field", ""),
                "code": issue.get("code", "placeholder"),
                "reason": issue.get("reason", "占位值不能进入资格检查"),
            }
        )
    return blockers


__all__ = [
    "QualificationEvidence",
    "build_qualification_evidence",
    "qualification_blockers",
    "qualification_is_valid",
]
