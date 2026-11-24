"""受管生成调用授权：DispatchGrant 由调度服务签发，客户端只验签。"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional


class GrantError(PermissionError):
    """缺少/伪造/过期/不匹配的调度授权。"""

    def __init__(self, message: str, *, error_code: str = "managed_instance_invalid") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class DispatchGrant:
    job_id: str
    attempt_id: str
    instance_id: str
    resource_key: str
    expires_at: float
    nonce: str
    signature: str


class GrantService:
    """HMAC 签发授权；用户可构造 dataclass，但无法生成有效签名。"""

    def __init__(self, secret: Optional[bytes] = None) -> None:
        self._secret = secret or secrets.token_bytes(32)

    def issue(
        self,
        *,
        job_id: str,
        attempt_id: str,
        instance_id: str,
        resource_key: str,
        ttl_seconds: float = 3.0,
        now: Optional[float] = None,
    ) -> DispatchGrant:
        moment = time.time() if now is None else float(now)
        nonce = secrets.token_hex(8)
        expires_at = moment + max(0.1, float(ttl_seconds))
        signature = self._sign(
            job_id, attempt_id, instance_id, resource_key, expires_at, nonce
        )
        return DispatchGrant(
            job_id=job_id,
            attempt_id=attempt_id,
            instance_id=instance_id,
            resource_key=resource_key,
            expires_at=expires_at,
            nonce=nonce,
            signature=signature,
        )

    def require(
        self,
        grant: Optional[DispatchGrant],
        *,
        job_id: str,
        instance_id: str,
        resource_key: str,
        now: Optional[float] = None,
    ) -> None:
        if grant is None:
            raise GrantError("受管调用缺少调度授权")
        moment = time.time() if now is None else float(now)
        if grant.expires_at < moment:
            raise GrantError("调度授权已过期")
        if (grant.job_id, grant.instance_id, grant.resource_key) != (
            job_id,
            instance_id,
            resource_key,
        ):
            raise GrantError("调度授权与目标作业/实例/资源不匹配")
        expected = self._sign(
            grant.job_id,
            grant.attempt_id,
            grant.instance_id,
            grant.resource_key,
            grant.expires_at,
            grant.nonce,
        )
        if not hmac.compare_digest(expected, grant.signature):
            raise GrantError("调度授权签名无效")

    def _sign(
        self,
        job_id: str,
        attempt_id: str,
        instance_id: str,
        resource_key: str,
        expires_at: float,
        nonce: str,
    ) -> str:
        message = "|".join(
            [
                job_id,
                attempt_id,
                instance_id,
                resource_key,
                f"{expires_at:.6f}",
                nonce,
            ]
        ).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()


_DEFAULT_SERVICE = GrantService()


def default_grant_service() -> GrantService:
    return _DEFAULT_SERVICE


def _load_pool_for_guard(path: Path):
    """受管 guard 的池配置读取：复用配置层维护的最后有效快照。"""
    from core.video_pool_config import last_good_snapshot, load_pool_config

    try:
        return load_pool_config(path), None
    except Exception as exc:  # noqa: BLE001 - 交给调用方决定阻断范围
        return last_good_snapshot(path), exc


def managed_instances_for_model(model_id: str) -> List[Dict[str, Any]]:
    """返回运行池中引用该模型（含同一端点的别名）的实例，含未启用草稿。

    直接匹配 model_id 优先；没有直接命中时按“协议 + base_url + request_model”
    识别别名，避免另取一个模型名绕过受管资源锁。

    解析失败时的阻断范围：
    - 有最后一份有效快照：只拒绝已知受管模型，既有非受管调用继续可用；
    - 没有有效快照：无法判断归属，全局 fail-closed，拒绝直接调用自定义视频模型。
    """
    model_key = str(model_id or "").strip()
    if not model_key:
        return []
    try:
        from config import Config
    except Exception:  # noqa: BLE001 - 无配置上下文时保持既有非受管调用
        return []
    path = str(getattr(Config, "VIDEO_PROFILES_FILE", "") or "").strip()
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        return []
    snapshot, error = _load_pool_for_guard(file_path)
    if snapshot is None:
        raise GrantError(
            f"资源池配置不可用，无法判断模型是否受管，拒绝直接调用：{error}"
        ) from error
    rows = [
        dict(row)
        for row in snapshot.data.get("video_instances") or []
        if isinstance(row, dict)
    ]
    direct = [row for row in rows if str(row.get("model_id") or "") == model_key]
    matched = direct
    if not matched:
        identity = _custom_model_identity(model_key)
        if identity is not None:
            matched = [
                row
                for row in rows
                if _custom_model_identity(str(row.get("model_id") or "")) == identity
            ]
    if error is not None and matched:
        raise GrantError(
            f"资源池配置不可用，拒绝直接调用已知受管视频模型：{error}"
        ) from error
    return matched


def _custom_model_identity(model_id: str) -> Optional[tuple[str, str, str]]:
    """自定义模型的远端身份：协议 + 规范化 base_url + 服务端 model 名。"""
    if not model_id:
        return None
    try:
        from models.config_model import resolve_model_entry

        kind, meta = resolve_model_entry(model_id)
    except Exception:  # noqa: BLE001 - 注册表不可用时不推断别名
        return None
    if kind != "custom":
        return None
    return (
        str(meta.get("protocol") or ""),
        str(meta.get("base_url") or "").rstrip("/"),
        str(meta.get("request_model") or ""),
    )


def _same_model_identity(left: str, right: str) -> bool:
    """同一远端端点的不同注册名视为同一受管资源（别名）。"""
    if not left or not right:
        return False
    if left == right:
        return True
    left_id = _custom_model_identity(left)
    right_id = _custom_model_identity(right)
    return left_id is not None and left_id == right_id


def resolve_managed_instance(model_id: str, instance_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """解析受管实例；未纳管返回 None，草稿/多实例未指定时默认拒绝。"""
    rows = managed_instances_for_model(model_id)
    if not rows:
        return None
    wanted = str(instance_id or "").strip()
    if wanted:
        for row in rows:
            if str(row.get("instance_id")) == wanted:
                if not row.get("enabled"):
                    raise GrantError("实例未启用，不能直接调用受管模型")
                return row
        raise GrantError("授权实例不属于该受管模型")
    enabled = [row for row in rows if row.get("enabled")]
    if not enabled:
        raise GrantError("实例未启用，不能直接调用受管模型")
    if len(enabled) > 1:
        raise GrantError(
            "该模型对应多个受管实例，必须显式指定调度实例",
            error_code="managed_instance_ambiguous",
        )
    return enabled[0]


def require_managed_dispatch(
    *,
    model: str,
    grant: Optional[DispatchGrant],
    instance: Dict[str, Any],
    expected_job_id: Optional[str] = None,
) -> None:
    """底层受管调用必须携带调度服务颁发的授权；别名/草稿不能绕过。

    ``expected_job_id`` 由调用方显式给出时，授权必须绑定同一作业；未给出时
    兼容既有调用，仅校验实例/资源与签名。
    """
    instance_model = str(instance.get("model_id") or "")
    if not _same_model_identity(instance_model, str(model or "")):
        raise GrantError("受管实例与调用模型不匹配")
    instance_id = str(instance.get("instance_id") or "")
    resource_key = str(instance.get("resource_key") or "")
    if not instance_id or not resource_key:
        raise GrantError("受管实例缺少实例/资源标识")
    grant_job_id = str(getattr(grant, "job_id", "") or "")
    if expected_job_id is not None and grant_job_id != str(expected_job_id):
        raise GrantError("调度授权与本次调用作业不匹配")
    default_grant_service().require(
        grant,
        job_id=str(expected_job_id) if expected_job_id is not None else grant_job_id,
        instance_id=instance_id,
        resource_key=resource_key,
    )


def managed_meta_for_grant(
    meta: Dict[str, Any],
    grant: DispatchGrant,
    instance: Dict[str, Any],
    *,
    job_id: str,
) -> Dict[str, Any]:
    """把受管上下文写进客户端 meta；job_id 必须来自 attempt/job，而不是 grant 自填。"""
    if not job_id:
        raise GrantError("受管调用缺少真实 attempt/job 标识")
    if grant is None or str(job_id) != str(grant.job_id):
        raise GrantError("调度作业与授权作业不一致")
    return {
        **meta,
        "managed": True,
        "dispatch_job_id": str(job_id),
        "managed_instance_id": str(instance.get("instance_id") or ""),
        "managed_resource_key": str(instance.get("resource_key") or ""),
    }


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatchVideoItem:
    """一个片段的轻量入队描述；媒体在派发前由执行层物化。"""

    item_id: str
    prompt: str
    save_path: str
    image_path: Optional[str] = None
    last_image_path: Optional[str] = None
    reference_image_paths: Optional[List[str]] = None
    reference_video_paths: Optional[List[str]] = None
    duration: int = 5
    video_ratio: str = "16:9"
    resolution: Optional[str] = None
    fps: Optional[int] = None
    short_edge: Optional[int] = None
    audio_reference_url: Optional[str] = None
    negative_prompt: Optional[str] = None
    seed: Optional[int] = None
    canonical_image_path: Optional[str] = None
    canonical_last_image_path: Optional[str] = None
    canonical_reference_image_paths: Optional[List[str]] = None
    canonical_reference_video_paths: Optional[List[str]] = None
    canonical_audio_reference_url: Optional[str] = None

    def to_request(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "save_path": self.save_path,
            "image_path": self.image_path,
            "last_image_path": self.last_image_path,
            "reference_image_paths": list(self.reference_image_paths or []),
            "reference_video_paths": list(self.reference_video_paths or []),
            "duration": self.duration,
            "video_ratio": self.video_ratio,
            "resolution": self.resolution,
            "fps": self.fps,
            "short_edge": self.short_edge,
            "audio_reference_url": self.audio_reference_url,
            "negative_prompt": self.negative_prompt,
            "seed": self.seed,
        }

    def canonical_media(self) -> Dict[str, Any]:
        return {
            "image_path": self.canonical_image_path,
            "last_image_path": self.canonical_last_image_path,
            "reference_image_paths": list(
                self.canonical_reference_image_paths or []
            ),
            "reference_video_paths": list(
                self.canonical_reference_video_paths or []
            ),
            "audio_reference_url": self.canonical_audio_reference_url,
        }


@dataclass(frozen=True)
class BatchHandle:
    batch_id: str
    owner: str
    job_ids: Mapping[str, str]  # item_id -> job_id


class VideoGenerationService:
    """批量轻量入队与订阅结果；由 workflow/agent 消费，不在模型层实现。"""

    def __init__(self, dispatch: Any, *, poll_interval: float = 0.2) -> None:
        self._dispatch = dispatch
        self._poll_interval = max(0.01, float(poll_interval))

    def submit(
        self,
        *,
        owner: str,
        session_id: str,
        execution_id: str,
        batch_id: str,
        contract: Any,
        members: List[str],
        items: List[BatchVideoItem],
        enable_concurrency: bool = True,
        max_concurrency_per_execution: int = 8,
        queue_timeout_seconds: int = 3600,
        max_queued_jobs: int = 1000,
        owner_login: str = "",
        publish_result: bool = False,
    ) -> BatchHandle:
        base_snapshot = {
            "mode": contract.mode,
            "members": list(members),
            "profile_revision": contract.revision,
            "selection_source": contract.selection_source,
            "recommendation_reason": contract.recommendation_reason,
            "parameters": dict(contract.parameters),
            "enable_concurrency": bool(enable_concurrency),
            "max_concurrency_per_execution": int(max_concurrency_per_execution),
            "owner_login": str(owner_login or ""),
            "publish_result": bool(publish_result),
        }
        jobs = [
            {
                "request_key": str(execution_id),
                "item_id": item.item_id,
                "source_kind": "workflow",
                "source_id": str(session_id),
                "execution_id": str(execution_id),
                "dispatch_mode": "auto",
                "profile_id": contract.profile_id,
                "profile_revision": contract.revision,
                "snapshot_json": {
                    **base_snapshot,
                    "request": item.to_request(),
                    "canonical_media": item.canonical_media(),
                },
            }
            for item in items
        ]
        result = self._dispatch.enqueue_batch(
            owner=owner,
            batch_id=str(batch_id),
            jobs=jobs,
            queue_timeout_seconds=int(queue_timeout_seconds),
            max_queued_jobs=int(max_queued_jobs),
        )
        job_ids = {
            str(row["item_id"]): str(row["job_id"]) for row in result.jobs
        }
        return BatchHandle(batch_id=str(batch_id), owner=owner, job_ids=job_ids)

    def wait(
        self,
        handle: BatchHandle,
        *,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Dict[str, Any]]:
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        pending = dict(handle.job_ids)
        results: Dict[str, Dict[str, Any]] = {}
        last_state: Dict[str, str] = {}

        def emit(item_id: str, state: str, payload: Dict[str, Any]) -> None:
            if last_state.get(item_id) == state:
                return
            last_state[item_id] = state
            if on_event is not None:
                on_event({"item_id": item_id, "state": state, **payload})

        while pending:
            if cancel_check is not None and cancel_check():
                for item_id, job_id in list(pending.items()):
                    try:
                        self._dispatch.cancel_job(job_id)
                    except Exception:  # noqa: BLE001 - 停止必须继续收尾
                        logger.warning("cancel job failed: %s", job_id, exc_info=True)
                    results[item_id] = {"state": "cancelled", "error": {"error_type": "cancelled"}}
                    emit(item_id, "failed", {"error": {"error_type": "cancelled"}})
                    pending.pop(item_id, None)
                break
            if deadline is not None and time.monotonic() >= deadline:
                for item_id in list(pending):
                    stalled = last_state.get(item_id) in ("unknown", "reconciling")
                    error = {
                        "error_code": (
                            "needs_reconciliation" if stalled else "queue_timeout"
                        ),
                        "reason": (
                            "作业需人工核对，本地等待已超时"
                            if stalled
                            else "本地等待超时"
                        ),
                    }
                    results[item_id] = {"state": "failed", "error": error}
                    emit(item_id, "failed", {"error": error})
                    pending.pop(item_id, None)
                break

            for item_id, job_id in list(pending.items()):
                job = self._dispatch.get_job(job_id)
                if job is None:
                    continue
                state = str(job.get("state") or "queued")
                if state == "succeeded":
                    result = job.get("result_json") or {}
                    download = result.get("download") if isinstance(result, dict) else {}
                    save_path = (
                        download.get("save_path")
                        if isinstance(download, dict)
                        else job.get("save_path")
                    )
                    published = (
                        result.get("published")
                        if isinstance(result, dict)
                        else None
                    )
                    success = {"state": "succeeded", "save_path": save_path}
                    if published is not None:
                        success["published"] = published
                        paths = [
                            str(item.get("public_path") or "")
                            for item in (published.get("published") or [])
                            if isinstance(item, dict) and item.get("public_path")
                        ]
                        success["published_paths"] = paths
                    results[item_id] = success
                    event_payload = {"save_path": save_path}
                    if published is not None:
                        event_payload["published"] = published
                        event_payload["published_paths"] = success["published_paths"]
                    emit(item_id, "done", event_payload)
                    pending.pop(item_id, None)
                elif state == "unknown":
                    error = job.get("error_json") or {}
                    if str(error.get("error_code") or "") != "dispatch_unknown":
                        # 轮询 unknown 且已有 remote id：远端可能仍可恢复，
                        # 交给下一轮 reconcile；本地继续等待到成功/失败/超时。
                        emit(item_id, state, {})
                        continue
                    # 无 remote id 的提交 unknown：已在服务侧隔离，本地立即失败。
                    error = {
                        "error_code": "dispatch_unknown",
                        "reason": "提交结果未知，作业已进入人工核对",
                    }
                    results[item_id] = {"state": "failed", "error": error}
                    emit(item_id, "failed", {"error": error})
                    pending.pop(item_id, None)
                elif state in ("failed", "cancelled"):
                    error = job.get("error_json") or {}
                    results[item_id] = {"state": state, "error": error}
                    emit(item_id, "failed", {"error": error})
                    pending.pop(item_id, None)
                else:
                    emit(item_id, state, {})
            if pending:
                time.sleep(self._poll_interval)
        return results


__all__ = [
    "BatchHandle",
    "BatchVideoItem",
    "DispatchGrant",
    "GrantError",
    "GrantService",
    "VideoGenerationService",
    "default_grant_service",
    "managed_instances_for_model",
    "managed_meta_for_grant",
    "require_managed_dispatch",
    "resolve_managed_instance",
]
