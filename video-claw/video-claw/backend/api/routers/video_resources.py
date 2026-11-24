"""视频资源池只读查询、池文件保存/重载与作业查询。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config import Config
from core.identity import CurrentUser, get_current_user, require_admin
from core.video_async_impact import (
    ASYNC_CONFIRM_KEY,
    collect_async_impact,
    parse_confirmed_models,
    unconfirmed_async_models,
)
from core.video_pool_config import (
    PoolConfigConflict,
    VideoPoolConfigError,
    collect_placeholder_issues,
    load_pool_config,
    write_pool_config,
)
from core.video_qualification import (
    build_qualification_evidence,
    qualification_is_valid,
)
from db.repositories.video_dispatch import VideoDispatchRepo

router = APIRouter(tags=["VideoResources"])


class PoolSaveRequest(BaseModel):
    values: Dict[str, Any]
    expected_revision: Optional[str] = None


class QualifyRequest(BaseModel):
    checks: Dict[str, bool] = {"config": True}
    confirmed: bool = False
    generate_test: bool = False


class AsyncConfirmRequest(BaseModel):
    model_ids: List[str]


# generate_test 只能由服务端 runner 追加，不能由管理员请求自报。
ALLOWED_QUALIFICATION_CHECKS = {
    "config",
    "probe",
    "jobs",
    "metrics",
    "download",
}

# 真实生成测试由 4.8 接线；未接线时显式拒绝，不静默调用真实模型。
_qualification_test_runner = None


def configure_qualification_test_runner(runner) -> None:
    global _qualification_test_runner
    _qualification_test_runner = runner


def _pool_path() -> Path:
    return Path(str(getattr(Config, "VIDEO_PROFILES_FILE", "")))


def _registries():
    config = Config.CONFIG if isinstance(Config.CONFIG, dict) else {}
    providers = config.get("api_providers") if isinstance(config.get("api_providers"), dict) else {}
    models = {
        str(item.get("id")): item
        for item in config.get("custom_models") or []
        if isinstance(item, dict) and item.get("id")
    }
    return providers, models


def _dispatch_runtime():
    from core.video_dispatch_runtime import get_dispatch_runtime

    runtime = get_dispatch_runtime()
    if runtime is None:
        raise HTTPException(status_code=409, detail="dispatch_not_started")
    return runtime


def _snapshot():
    providers, models = _registries()
    return load_pool_config(_pool_path(), providers=providers, models=models)


def _instance_index(snapshot) -> Dict[str, dict]:
    return {
        str(row.get("instance_id")): row
        for row in snapshot.data.get("video_instances") or []
        if isinstance(row, dict)
    }


def _qualification_ok(instance: dict, snapshot, blocked_models: set[str]) -> bool:
    if str(instance.get("model_id") or "") in blocked_models:
        return False
    repo = VideoDispatchRepo()
    evidence = repo.get_qualification(
        instance_id=str(instance.get("instance_id") or ""),
        resource_key=str(instance.get("resource_key") or ""),
    )
    return qualification_is_valid(
        evidence,
        config_revision=snapshot.revision,
        boot_id=str(getattr(Config, "BOOT_ID", "")),
        now=time.time(),
    )


def _confirmed_async_models() -> set[str]:
    raw = VideoDispatchRepo().get_system_meta(ASYNC_CONFIRM_KEY)
    return parse_confirmed_models(raw)


def _unconfirmed_models(snapshot) -> set[str]:
    providers, models = _registries()
    confirmed = _confirmed_async_models()
    impacts = collect_async_impact(
        snapshot.data,
        models=models,
        providers=providers,
        confirmed_models=confirmed,
    )
    return set(unconfirmed_async_models(impacts, confirmed_models=confirmed))


def _observation_view(
    instance_ids: List[str], observations: Dict[str, Any]
) -> dict:
    samples = [
        observations[instance_id]
        for instance_id in instance_ids
        if instance_id in observations
    ]
    if not samples:
        return {
            "state": "unknown",
            "observed_at": None,
            "coverage": "none",
            "stale": True,
        }
    states = {str(observation.state) for observation, _epoch in samples}
    state = next(iter(states)) if len(states) == 1 else "mixed"
    epochs = [float(epoch) for _observation, epoch in samples]
    oldest = min(samples, key=lambda item: item[1])
    return {
        "state": state,
        "observed_at": oldest[0].observed_at,
        "coverage": "complete" if len(samples) == len(instance_ids) else "partial",
        "stale": bool(epochs) and time.time() - min(epochs) > 15,
    }


def _profile_view(
    profile: dict,
    instances: Dict[str, dict],
    snapshot,
    active_instances: set[str],
    blocked_models: set[str],
    observations: Optional[Dict[str, Any]] = None,
) -> dict:
    members = [
        instances[member]
        for member in profile.get("instances") or []
        if member in instances
    ]
    enabled_members = [row for row in members if row.get("enabled")]
    qualified = [
        row for row in enabled_members if _qualification_ok(row, snapshot, blocked_models)
    ]
    qualified_ids = {str(row.get("instance_id")) for row in qualified}
    free = {
        str(row.get("instance_id"))
        for row in qualified
        if str(row.get("instance_id")) not in active_instances
    }
    if not profile.get("enabled", True) or not qualified:
        state = "not_ready"
    elif not free:
        state = "busy"
    else:
        state = "ready"
    missing = []
    if not profile.get("enabled", True):
        missing.append({"field": f"video_profiles.{profile.get('id')}.enabled", "code": "disabled"})
    if not enabled_members:
        missing.append({"field": f"video_profiles.{profile.get('id')}.instances", "code": "no_enabled_member"})
    elif not qualified:
        missing.append({"field": f"video_profiles.{profile.get('id')}.instances", "code": "no_qualified_member"})
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "revision": profile.get("revision"),
        "enabled": bool(profile.get("enabled", True)),
        "state": state,
        "dispatchable": state == "ready" and bool(free),
        "contracts": sorted((profile.get("contracts") or {}).keys()),
        "instances": [row.get("instance_id") for row in members],
        "missing": missing,
        "observation": _observation_view(
            sorted(qualified_ids), observations or {}
        ),
    }


def _profile_payload(snapshot) -> dict:
    instances = _instance_index(snapshot)
    active_instances = VideoDispatchRepo().active_instance_ids()
    blocked_models = _unconfirmed_models(snapshot)
    from core.video_dispatch_runtime import get_dispatch_runtime

    runtime = get_dispatch_runtime()
    service = runtime.service if runtime is not None else None
    observations: Dict[str, Any] = {}
    probe_service = service.probe_service if service is not None else None
    if probe_service is not None:
        for row in instances.values():
            instance_id = str(row.get("instance_id") or "")
            observation = probe_service.last_observation(instance_id)
            if observation is not None:
                observations[instance_id] = observation
    profiles = [
        _profile_view(
            profile,
            instances,
            snapshot,
            active_instances,
            blocked_models,
            observations,
        )
        for profile in snapshot.data.get("video_profiles") or []
        if isinstance(profile, dict)
    ]
    stopped = bool(service is not None and service.stopped)
    draining_profiles = set(service.draining_profiles) if service is not None else set()
    draining_instances = set(service.draining_instances) if service is not None else set()
    for profile in profiles:
        if profile["id"] in draining_profiles:
            profile["state"] = "not_ready"
            profile["dispatchable"] = False
            profile.setdefault("blocking_reasons", []).append("pool_draining")
    defaults = snapshot.data["video_dispatch"]["default_profiles"]
    default_ok = bool(defaults) and all(
        any(profile["id"] == pool_id and profile["state"] == "ready" for profile in profiles)
        for pool_id in defaults.values()
    )
    return {
        "dispatch_enabled": bool(getattr(Config, "VIDEO_DISPATCH_ENABLED", True))
        and not stopped,
        "dispatch_stopped": stopped,
        "draining_profiles": sorted(draining_profiles),
        "draining_instances": sorted(draining_instances),
        "profiles_file": str(_pool_path()),
        "config_revision": snapshot.revision,
        "has_default_recommendation": bool(defaults),
        "recommendation_reason": "none" if default_ok else ("no_default" if not defaults else "default_not_ready"),
        "profiles": profiles,
    }


@router.get("/api/video/profiles")
async def get_video_profiles(_: CurrentUser = Depends(get_current_user)):
    try:
        return _profile_payload(_snapshot())
    except VideoPoolConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/video/profiles/raw")
async def get_video_profiles_raw(_: CurrentUser = Depends(require_admin)):
    """管理员读取运行时 YAML 原文；保存必须回传同一 revision。"""
    try:
        snapshot = _snapshot()
    except VideoPoolConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"revision": snapshot.revision, "values": snapshot.data}


class QuarantineReleaseRequest(BaseModel):
    resource_key: str
    reason: str = ""
    expected_attempt_id: Optional[str] = None
    expected_remote_job_id: Optional[str] = None
    force: bool = False


@router.get("/api/video/resources/diagnostics")
async def get_video_resource_diagnostics(
    _: CurrentUser = Depends(require_admin),
):
    repo = VideoDispatchRepo()
    now_ms = int(time.time() * 1000)
    items = []
    for row in repo.list_quarantined():
        started_at = row.get("started_at")
        items.append(
            {
                "resource_key": row.get("resource_key"),
                "quarantine_reason": row.get("quarantine_reason"),
                "attempt_id": row.get("active_attempt_id"),
                "job_id": row.get("job_id"),
                "instance_id": row.get("instance_id"),
                "submission_state": row.get("submission_state"),
                "remote_job_id": row.get("remote_job_id"),
                "owner": row.get("owner"),
                "profile_id": row.get("profile_id"),
                "job_state": row.get("job_state"),
                "error_detail": row.get("error_json"),
                "age_ms": max(0, now_ms - int(started_at)) if started_at else None,
            }
        )
    return {
        "quarantined": items,
        "deleting_sources": [
            {"source_kind": kind, "source_id": source_id}
            for kind, source_id in repo.list_source_deleting()
        ],
        "inflight": repo.count_inflight(),
    }


@router.post("/api/video/resources/deleting/reconcile")
async def reconcile_deleting_sources(
    _: CurrentUser = Depends(require_admin),
):
    from db.repositories.sessions import SessionRepo

    runtime = _dispatch_runtime()
    result = await run_in_threadpool(
        runtime.service.reconcile_pending_deletions,
        lambda sid: SessionRepo().get(sid) is not None,
    )
    return result


@router.post("/api/video/resources/quarantine/release")
async def release_video_quarantine(
    req: QuarantineReleaseRequest,
    user: CurrentUser = Depends(require_admin),
):
    from db.repositories.video_dispatch import QuarantineReleaseConflict

    try:
        released = VideoDispatchRepo().release_quarantine(
            resource_key=req.resource_key,
            released_by=str(user.login),
            reason=req.reason,
            expected_attempt_id=req.expected_attempt_id,
            expected_remote_job_id=req.expected_remote_job_id,
            force=bool(req.force),
        )
    except QuarantineReleaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "quarantine_release_conflict",
                "error_code": exc.code,
                "reason": exc.reason,
            },
        ) from exc
    if released is None:
        raise HTTPException(status_code=404, detail="quarantined resource not found")
    return released


@router.get("/api/video/resources/summary")
async def get_video_resources(_: CurrentUser = Depends(get_current_user)):
    payload = await get_video_profiles(_)
    repo = VideoDispatchRepo()
    counts = {
        "ready": 0,
        "not_ready": 0,
        "busy": 0,
        "inflight": repo.count_inflight(),
        "quarantined": repo.count_quarantined(),
    }
    for profile in payload["profiles"]:
        state = profile["state"] if profile["state"] in counts else "not_ready"
        counts[state] += 1
    return {
        "dispatch_enabled": payload["dispatch_enabled"],
        "config_revision": payload["config_revision"],
        "profiles": counts,
    }


@router.put("/api/video/profiles")
async def save_video_profiles(
    req: PoolSaveRequest,
    _: CurrentUser = Depends(require_admin),
):
    if not req.expected_revision:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    providers, models = _registries()
    try:
        snapshot = write_pool_config(
            _pool_path(),
            req.values,
            expected_revision=req.expected_revision,
            providers=providers,
            models=models,
        )
    except PoolConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VideoPoolConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config_revision": snapshot.revision, "saved": True}


@router.post("/api/video/profiles/reload")
async def reload_video_profiles(_: CurrentUser = Depends(require_admin)):
    try:
        snapshot = _snapshot()
    except VideoPoolConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config_revision": snapshot.revision, "reloaded": True}


@router.post("/api/video/instances/{instance_id}/qualify")
async def qualify_video_instance(
    instance_id: str,
    req: QualifyRequest,
    user: CurrentUser = Depends(require_admin),
):
    """管理员显式资格检查；只写证据，不启用实例、不调用真实生成。"""
    snapshot = _snapshot()
    instance = _instance_index(snapshot).get(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="instance not found")
    providers, models = _registries()
    issues = [
        issue
        for issue in collect_placeholder_issues(
            snapshot.data, providers=providers, models=models
        )
        if str(issue.get("field") or "").startswith(f"video_instances.{instance_id}.")
    ]
    if issues:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "qualification_error",
                "error_code": "placeholder_instance",
                "reason": "占位实例不能在 HTTP 前进入资格检查",
                "missing": issues,
            },
        )
    blocked_models = _unconfirmed_models(snapshot)
    if str(instance.get("model_id") or "") in blocked_models:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "qualification_error",
                "error_code": "async_model_unconfirmed",
                "reason": "模型尚未确认默认 async 影响，不能进入就绪集合",
                "model_id": str(instance.get("model_id") or ""),
            },
        )
    if req.generate_test and not req.confirmed:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "qualification_error",
                "error_code": "confirmation_required",
                "reason": "真实生成测试必须显式确认",
            },
        )
    checks = {str(key): bool(value) for key, value in req.checks.items()}
    unknown_checks = sorted(set(checks) - ALLOWED_QUALIFICATION_CHECKS)
    if unknown_checks:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "qualification_error",
                "error_code": "invalid_check",
                "reason": "资格检查包含未定义的检查项",
                "checks": unknown_checks,
            },
        )
    if req.generate_test:
        runner = _qualification_test_runner
        if runner is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "qualification_error",
                    "error_code": "generate_test_not_available",
                    "reason": "真实生成测试尚未接线；请只做只读检查",
                },
            )
        try:
            extra = await run_in_threadpool(runner, instance, snapshot) or {}
        except Exception as exc:  # noqa: BLE001 - 真实测试失败转为可诊断错误
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "qualification_error",
                    "error_code": "generate_test_failed",
                    "reason": str(exc)[:300],
                },
            ) from exc
        extra_payload = dict(extra)
        control_fields = {"pending", "generate_submitted", "completed"}
        extra_checks = {
            str(key): bool(value)
            for key, value in extra_payload.items()
            if str(key) not in control_fields
        }
        checks.update(extra_checks)
        if (
            bool(extra_payload.get("pending"))
            or extra_payload.get("completed") is not True
            or checks.get("generate_test") is not True
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "qualification_error",
                    "error_code": "generate_test_incomplete",
                    "reason": "真实生成未完成或缺少成功证据，不能写入资格",
                },
            )
    try:
        evidence = build_qualification_evidence(
            instance_id=instance_id,
            resource_key=str(instance.get("resource_key") or ""),
            config_revision=snapshot.revision,
            boot_id=str(getattr(Config, "BOOT_ID", "")),
            checked_by=str(user.login),
            checks=checks,
            confirmed=bool(req.confirmed),
            checked_at=time.time(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    VideoDispatchRepo().save_qualification(
        instance_id=instance_id,
        resource_key=evidence.resource_key,
        evidence=evidence.to_dict(),
    )
    return {
        "instance_id": instance_id,
        "qualified": True,
        "config_revision": snapshot.revision,
        "evidence": evidence.to_dict(),
    }


@router.get("/api/video/models/async-impact")
async def get_async_impact(_: CurrentUser = Depends(require_admin)):
    snapshot = _snapshot()
    providers, models = _registries()
    confirmed = _confirmed_async_models()
    impacts = collect_async_impact(
        snapshot.data,
        models=models,
        providers=providers,
        confirmed_models=confirmed,
    )
    return {
        "config_revision": snapshot.revision,
        "confirmed_models": sorted(confirmed),
        "items": [impact.to_dict() for impact in impacts],
    }


@router.post("/api/video/models/async-impact/confirm")
async def confirm_async_impact(
    req: AsyncConfirmRequest,
    _: CurrentUser = Depends(require_admin),
):
    snapshot = _snapshot()
    providers, models = _registries()
    confirmed = _confirmed_async_models()
    impacts = collect_async_impact(
        snapshot.data,
        models=models,
        providers=providers,
        confirmed_models=confirmed,
    )
    known = {impact.model_id for impact in impacts}
    unknown = sorted({str(item) for item in req.model_ids} - known)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "qualification_error",
                "error_code": "unknown_model",
                "reason": "确认清单包含未进入默认 async 影响清单的模型",
                "models": unknown,
            },
        )
    updated = sorted(confirmed | {str(item) for item in req.model_ids})
    VideoDispatchRepo().set_system_meta(ASYNC_CONFIRM_KEY, json.dumps(updated))
    impacts = collect_async_impact(
        snapshot.data,
        models=models,
        providers=providers,
        confirmed_models=set(updated),
    )
    return {
        "config_revision": snapshot.revision,
        "confirmed_models": updated,
        "items": [impact.to_dict() for impact in impacts],
    }


@router.post("/api/video/profiles/{profile_id}/drain")
async def drain_video_profile(
    profile_id: str, _: CurrentUser = Depends(require_admin)
):
    runtime = _dispatch_runtime()
    return await run_in_threadpool(runtime.service.drain_profile, profile_id)


@router.post("/api/video/profiles/{profile_id}/undrain")
async def undrain_video_profile(
    profile_id: str, _: CurrentUser = Depends(require_admin)
):
    runtime = _dispatch_runtime()
    return await run_in_threadpool(runtime.service.undrain_profile, profile_id)


@router.post("/api/video/instances/{instance_id}/drain")
async def drain_video_instance(
    instance_id: str, _: CurrentUser = Depends(require_admin)
):
    snapshot = _snapshot()
    profile_ids = [
        str(profile.get("id"))
        for profile in snapshot.data.get("video_profiles") or []
        if instance_id in (profile.get("instances") or [])
    ]
    runtime = _dispatch_runtime()
    result = await run_in_threadpool(
        runtime.service.drain_instance, instance_id, profile_ids
    )
    result["profile_ids"] = profile_ids
    return result


@router.post("/api/video/instances/{instance_id}/undrain")
async def undrain_video_instance(
    instance_id: str, _: CurrentUser = Depends(require_admin)
):
    runtime = _dispatch_runtime()
    return await run_in_threadpool(runtime.service.undrain_instance, instance_id)


@router.post("/api/video/dispatch/stop")
async def stop_video_dispatch(_: CurrentUser = Depends(require_admin)):
    runtime = _dispatch_runtime()
    return await run_in_threadpool(runtime.service.stop_all)


@router.post("/api/video/dispatch/resume")
async def resume_video_dispatch(_: CurrentUser = Depends(require_admin)):
    runtime = _dispatch_runtime()
    return await run_in_threadpool(runtime.service.resume_all)


@router.post("/api/video/jobs/{job_id}/cancel")
async def cancel_video_job(job_id: str, user: CurrentUser = Depends(get_current_user)):
    repo = VideoDispatchRepo()
    job = repo.get_job(job_id)
    if job is None or (not user.is_admin and job.get("owner") != user.owner_key):
        raise HTTPException(status_code=404, detail="job not found")
    runtime = _dispatch_runtime()
    result = await run_in_threadpool(
        runtime.service.cancel_job,
        job_id,
        transport_factory=runtime.transport_factory,
    )
    return {
        "job_id": job_id,
        "state": result.get("state"),
        "error_detail": result.get("error_json"),
    }


@router.get("/api/video/jobs/{job_id}")
async def get_video_job(job_id: str, user: CurrentUser = Depends(get_current_user)):
    job = VideoDispatchRepo().get_job(job_id)
    if job is None or (not user.is_admin and job.get("owner") != user.owner_key):
        raise HTTPException(status_code=404, detail="job not found")
    dispatch_phase = str(job.get("state") or "unknown")
    try:
        runtime = _dispatch_runtime()
        dispatch_phase = await run_in_threadpool(
            runtime.service.job_phase, job_id
        )
    except Exception:  # noqa: BLE001 - 诊断阶段不能阻断 job 可见性
        pass
    return {
        "job_id": job.get("job_id"),
        "state": job.get("state"),
        "dispatch_phase": dispatch_phase,
        "profile_id": job.get("profile_id"),
        "queue_deadline_at": job.get("queue_deadline_at"),
        "accepted_at": job.get("accepted_at"),
        "error_detail": job.get("error_json"),
    }
