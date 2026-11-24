"""默认 async 变更的纳管影响清单（只读、不触发真实生成）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Set

from core.video_pool_config import collect_placeholder_issues

ASYNC_CONFIRM_KEY = "video_async_confirmed_models"


def parse_confirmed_models(value: Any) -> Set[str]:
    """解析 schema_meta 中的确认清单；非法 JSON 视为未确认。"""
    import json

    if value is None or value == "":
        return set()
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, (list, tuple)):
        return set()
    return {str(item) for item in parsed if str(item).strip()}


@dataclass(frozen=True)
class AsyncImpact:
    model_id: str
    provider: str
    instances: tuple[str, ...]
    profiles: tuple[str, ...]
    confirmed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "instances": list(self.instances),
            "profiles": list(self.profiles),
            "confirmed": self.confirmed,
        }


def collect_async_impact(
    pool_data: Mapping[str, Any],
    *,
    models: Mapping[str, Any],
    providers: Mapping[str, Any],
    confirmed_models: Optional[Iterable[str]] = None,
) -> list[AsyncImpact]:
    """列出未显式声明 video_api_mode 的 vLLM-Omni 模型及其真实引用。

    占位实例（example 域名/CHANGE_ME/样例资源键）不进入清单：它们永远不会被
    当作可纳管资源，也不应诱导管理员确认。
    """
    confirmed = {str(item) for item in (confirmed_models or ())}
    placeholder_fields = {
        issue["field"]
        for issue in collect_placeholder_issues(
            pool_data, providers=providers, models=models
        )
    }
    placeholder_models = {
        field.split(".", 2)[1]
        for field in placeholder_fields
        if field.startswith("custom_models.")
    }
    placeholder_providers = {
        field.split(".", 2)[1]
        for field in placeholder_fields
        if field.startswith("api_providers.")
    }
    instance_rows = {
        str(row.get("instance_id")): row
        for row in pool_data.get("video_instances") or []
        if isinstance(row, dict)
    }
    instance_profiles: Dict[str, list[str]] = {key: [] for key in instance_rows}
    for profile in pool_data.get("video_profiles") or []:
        if not isinstance(profile, dict):
            continue
        for instance_id in profile.get("instances") or []:
            instance_profiles.setdefault(str(instance_id), []).append(str(profile.get("id")))

    grouped: Dict[str, Dict[str, Any]] = {}
    for instance_id, instance in instance_rows.items():
        if _instance_has_placeholder(instance_id, placeholder_fields):
            continue
        model_id = str(instance.get("model_id") or "")
        model = models.get(model_id)
        if model_id in placeholder_models:
            continue
        if isinstance(model, dict) and str(model.get("provider") or "") in placeholder_providers:
            continue
        if not isinstance(model, dict):
            continue
        provider_key = str(model.get("provider") or "")
        provider = providers.get(provider_key)
        if not isinstance(provider, dict) or str(provider.get("protocol") or "") != "vllm-omni":
            continue
        if str(model.get("video_api_mode") or "").strip():
            continue
        entry = grouped.setdefault(
            model_id,
            {
                "model_id": model_id,
                "provider": provider_key,
                "instances": [],
                "profiles": set(),
            },
        )
        entry["instances"].append(instance_id)
        entry["profiles"].update(instance_profiles.get(instance_id, []))

    impacts: list[AsyncImpact] = []
    for model_id in sorted(grouped):
        entry = grouped[model_id]
        impacts.append(
            AsyncImpact(
                model_id=model_id,
                provider=str(entry["provider"]),
                instances=tuple(sorted(entry["instances"])),
                profiles=tuple(sorted(entry["profiles"])),
                confirmed=model_id in confirmed,
            )
        )
    return impacts


def unconfirmed_async_models(
    impacts: Iterable[AsyncImpact],
    *,
    confirmed_models: Optional[Iterable[str]] = None,
) -> list[str]:
    confirmed = {str(item) for item in (confirmed_models or ())}
    return sorted(
        impact.model_id
        for impact in impacts
        if not impact.confirmed and impact.model_id not in confirmed
    )


def _instance_has_placeholder(instance_id: str, fields: Set[str]) -> bool:
    prefix = f"video_instances.{instance_id}."
    return any(field.startswith(prefix) for field in fields)


__all__ = [
    "ASYNC_CONFIRM_KEY",
    "AsyncImpact",
    "collect_async_impact",
    "parse_confirmed_models",
    "unconfirmed_async_models",
]
