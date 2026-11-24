"""单进程视频调度服务：DB 预留、准入分档、发送授权与异常核对。

职责边界：

- 池/实例来自运行时 YAML（由调用方注入 loader），资格证据与槽位账本在 PostgreSQL；
- 本模块只编排：短事务预留、提交前复查、HTTP 只经注入的 transport 执行；
- 任何数据库失败都会禁止新派发（``DispatchUnavailable``），绝不回退到纯内存
  互斥继续提交；
- unknown/取消未确认/心跳失联只隔离、不清槽；启动恢复只核对，不自动重发。

派发名额在每次 tick 按“快照成员 ∩ 当前启用/资格有效成员”的合格去重槽数、
快照策略、当前 profile 策略与全局上限共同收敛，入队时的零空闲不会固化为
整个执行的上限。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from psycopg import OperationalError

from core.video_dispatch import CapacitySnapshot, DispatchJob

from core.video_generation_service import (
    DispatchGrant,
    GrantService,
    default_grant_service,
)
from core.video_dispatch_errors import dispatch_error_payload
from core.video_batch_snapshot import BatchSnapshot, effective_members
from core.video_instance_admission import ObservationSample
from core.video_job_lifecycle import (
    SUBMIT_ACCEPTED,
    SUBMIT_REJECTED,
    SUBMIT_UNKNOWN,
    classify_submit_exception,
    may_release_slot,
)
from core.video_pool_selection import (
    PoolChoice,
    PreparedVideoContract,
    choose_pool,
)
from core.video_qualification import qualification_is_valid
from core.video_queue_policy import (
    QUEUE_TIMEOUT,
    WAITING_POOL_UNAVAILABLE,
    evaluate_queued_job,
)
from db.database import DatabaseUnavailable
from db.repositories.video_dispatch import (
    QueueFull,
    ReservationError,
    SlotBusy,
    VideoDispatchRepo,
)

logger = logging.getLogger(__name__)

DB_ERRORS = (OperationalError, DatabaseUnavailable)
JOB_TERMINAL_STATES = ("succeeded", "failed", "cancelled")
DOWNLOAD_FAILED = "download_failed"


class DispatchUnavailable(RuntimeError):
    """数据库/账本不可用或派发闸门拒绝；带 error_code 供 API 分类。"""

    def __init__(self, message: str, *, error_code: str = "dispatch_unavailable") -> None:
        super().__init__(message)
        self.error_code = error_code


class DispatchCrashInjected(RuntimeError):
    """测试用崩溃注入；生产不注册该钩子。"""


@dataclass(frozen=True)
class RuntimeInstance:
    instance_id: str
    model_id: str
    resource_key: str
    access_mode: str
    enabled: bool
    driver: str
    probe: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeProfile:
    profile_id: str
    revision: int
    enabled: bool
    members: Tuple[str, ...]
    contracts: Mapping[str, Any]
    max_concurrency_per_execution: int


@dataclass(frozen=True)
class PoolRuntime:
    revision: str
    dispatch_enabled: bool
    global_limit: int
    profiles: Mapping[str, RuntimeProfile]
    instances: Mapping[str, RuntimeInstance]
    qualified: FrozenSet[str]
    occupied_keys: FrozenSet[str]
    quarantined_keys: FrozenSet[str]
    batch_inflight: Mapping[str, int]
    last_dispatch: Mapping[str, int]
    default_profiles: Mapping[str, str] = field(default_factory=dict)
    queue_timeout_seconds: int = 3600
    max_queued_jobs: int = 1000
    draining_profiles: FrozenSet[str] = frozenset()
    draining_instances: FrozenSet[str] = frozenset()
    blocked_models: FrozenSet[str] = frozenset()

    def eligible_members(self, profile_id: Optional[str]) -> Tuple[RuntimeInstance, ...]:
        """该池当前可参与派发的实例：启用 + 资格有效 + 同资源键去重。"""
        profile = self.profiles.get(str(profile_id or ""))
        if profile is None:
            return ()
        seen: set[str] = set()
        result: List[RuntimeInstance] = []
        for instance_id in profile.members:
            instance = self.instances.get(instance_id)
            if instance is None or not instance.enabled:
                continue
            if profile_id in self.draining_profiles:
                continue
            if instance_id in self.draining_instances:
                continue
            if instance_id not in self.qualified:
                continue
            if instance.resource_key in seen:
                continue
            seen.add(instance.resource_key)
            result.append(instance)
        return tuple(result)

    def capacity(self, profile_id: Optional[str]) -> int:
        return len(self.eligible_members(profile_id))


@dataclass(frozen=True)
class PoolResolution:
    """新执行的选池结果：只进执行快照，不写回会话偏好。"""

    contract: PreparedVideoContract
    choice: PoolChoice
    members: Tuple[str, ...] = ()

    @property
    def error(self) -> str:
        return self.choice.error


@dataclass(frozen=True)
class SubmitOutcome:
    status: str  # accepted / rejected / unknown
    remote_job_id: str = ""
    detail: Any = None


@dataclass(frozen=True)
class PollOutcome:
    state: str  # pending / success / failure / unknown
    detail: Any = None
    drain_confirmed: bool = False


class DispatchTransport:
    """传输层接口：由 models/custom_video 适配或测试 Fake 实现。"""

    def submit(
        self,
        *,
        job: Mapping[str, Any],
        instance: RuntimeInstance,
        grant: DispatchGrant,
    ) -> SubmitOutcome:  # pragma: no cover - 接口声明
        raise NotImplementedError

    def poll(
        self,
        *,
        attempt: Mapping[str, Any],
        instance: RuntimeInstance,
        job: Mapping[str, Any],
    ) -> PollOutcome:  # pragma: no cover - 接口声明
        raise NotImplementedError

    def download(
        self,
        *,
        attempt: Mapping[str, Any],
        instance: RuntimeInstance,
        job: Mapping[str, Any],
    ) -> Any:  # pragma: no cover - 接口声明
        raise NotImplementedError

    def cancel(
        self,
        *,
        attempt: Mapping[str, Any],
        instance: RuntimeInstance,
        job: Mapping[str, Any],
    ) -> bool:  # pragma: no cover - 接口声明
        return False

    def close(self) -> None:  # pragma: no cover - 可选
        return None


@dataclass
class DispatchReport:
    events: List[Dict[str, Any]] = field(default_factory=list)
    posts: int = 0
    submitted: int = 0
    rejected: int = 0
    unknown: int = 0
    completed: int = 0
    failed: int = 0
    quarantined: int = 0
    downloaded: int = 0
    disabled: bool = False

    def add(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, **fields})


class SharedObservationLedger:
    """shared 实例的两段 idle 记录；保留独立时间戳，不允许同一次缓存伪造两次。"""

    def __init__(self, *, min_gap: float = 5.0, max_age: float = 15.0) -> None:
        self.min_gap = float(min_gap)
        self.max_age = float(max_age)
        self._samples: Dict[str, List[ObservationSample]] = {}

    def add(self, instance_id: str, sample: Optional[ObservationSample]) -> None:
        if sample is None:
            return
        items = self._samples.setdefault(instance_id, [])
        if any(abs(item.observed_at - sample.observed_at) < 1e-9 for item in items):
            return
        items.append(sample)
        items.sort(key=lambda item: item.observed_at)
        if len(items) > 20:
            del items[:-20]

    def recent(self, instance_id: str, now: float) -> List[ObservationSample]:
        return [
            item
            for item in self._samples.get(instance_id, [])
            if 0 <= float(now) - item.observed_at <= self.max_age
        ]

    def two_idle(self, instance_id: str, now: float) -> bool:
        idles = sorted(
            (item for item in self.recent(instance_id, now) if item.state == "idle"),
            key=lambda item: item.observed_at,
        )
        if len(idles) < 2:
            return False
        return idles[-1].observed_at - idles[0].observed_at >= self.min_gap

    def clear(self, instance_id: str) -> None:
        self._samples.pop(instance_id, None)


class DispatchService:
    """一个进程一个实例；由 lifespan 启动/停止（4.11 接线前由测试显式驱动）。"""

    def __init__(
        self,
        *,
        pool_loader: Callable[[], Tuple[Mapping[str, Any], str]],
        dispatch_enabled: Callable[[], bool] = lambda: True,
        blocked_models_provider: Optional[Callable[[Mapping[str, Any]], Iterable[str]]] = None,
        repo: Optional[VideoDispatchRepo] = None,
        grants: Optional[GrantService] = None,
        boot_id: str = "",
        clock: Callable[[], float] = time.time,
        shared_observer: Optional[Callable[[RuntimeInstance], Optional[ObservationSample]]] = None,
        exclusive_observer: Optional[Callable[[RuntimeInstance], Optional[ObservationSample]]] = None,
        probe_service: Optional[Any] = None,
        heartbeat_interval_seconds: float = 30.0,
        heartbeat_stale_seconds: float = 90.0,
        cancel_grace_seconds: float = 300.0,
        download_attempts: int = 3,
        max_safe_retries: int = 1,
        sleep: Callable[[float], None] = time.sleep,
        crash_hook: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
        shared_min_gap: float = 5.0,
        shared_max_age: float = 15.0,
        job_input_preparer: Optional[
            Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]
        ] = None,
        result_publisher: Optional[
            Callable[[Mapping[str, Any], Mapping[str, Any], Any], Any]
        ] = None,
    ) -> None:
        self._pool_loader = pool_loader
        self._dispatch_enabled = dispatch_enabled
        self._blocked_models_provider = blocked_models_provider or self._default_blocked_models
        self._repo = repo or VideoDispatchRepo()
        # 必须与 VideoClient/CustomVideoClient 验签使用同一个单例密钥，否则真实装配后
        # 服务签发的授权会被全部判为签名无效。测试可显式注入自己的 GrantService。
        self._grants = grants or default_grant_service()
        self._boot_id = str(boot_id or "")
        self._clock = clock
        self._probe_service = probe_service
        self._shared_observer = shared_observer or getattr(
            probe_service, "observe_shared", None
        )
        self._exclusive_observer = exclusive_observer or getattr(
            probe_service, "observe_exclusive", None
        )
        self._heartbeat_interval = float(heartbeat_interval_seconds)
        self._heartbeat_stale = float(heartbeat_stale_seconds)
        self._cancel_grace = float(cancel_grace_seconds)
        self._download_attempts = max(1, int(download_attempts))
        self._max_safe_retries = max(0, int(max_safe_retries))
        self._sleep = sleep
        self._crash_hook = crash_hook
        self._job_input_preparer = job_input_preparer
        self._result_publisher = result_publisher
        self._shared = SharedObservationLedger(min_gap=shared_min_gap, max_age=shared_max_age)
        self._stopped = False
        self._draining_profiles: set[str] = set()
        self._draining_instances: set[str] = set()
        self._state_lock = threading.RLock()
        self._deleting_sources: set[tuple[str, str]] = set()
        self._waker: Optional[Callable[[], None]] = None

    def set_waker(self, waker: Callable[[], None]) -> None:
        """由 DispatchRuntime 注入条件变量唤醒；纯服务测试可不注入。"""
        self._waker = waker

    def _wake(self) -> None:
        if self._waker is not None:
            try:
                self._waker()
            except Exception:  # noqa: BLE001 - 唤醒失败不能阻断业务
                logger.warning("dispatch wake failed", exc_info=True)

    # ── 入队 ──

    @property
    def probe_service(self) -> Optional[Any]:
        return self._probe_service

    @property
    def stopped(self) -> bool:
        return bool(self._stopped)

    @property
    def draining_profiles(self) -> frozenset[str]:
        return frozenset(self._draining_profiles)

    @property
    def draining_instances(self) -> frozenset[str]:
        return frozenset(self._draining_instances)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._repo.get_job(str(job_id))
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def job_phase(self, job_id: str) -> str:
        """队列阶段：queued / waiting_pool_unavailable；非排队直接返回状态。"""
        job = self.get_job(str(job_id))
        if job is None:
            return "unknown"
        state = str(job.get("state") or "unknown")
        if state != "queued":
            return state
        if self._stopped:
            return "waiting_pool_unavailable"
        runtime = self.runtime()
        profile_id = str(job.get("profile_id") or "")
        profile = runtime.profiles.get(profile_id)
        if (
            profile is None
            or not profile.enabled
            or profile_id in runtime.draining_profiles
        ):
            return "waiting_pool_unavailable"
        if not self._job_members(job, runtime):
            return "waiting_pool_unavailable"
        return "queued"

    def has_pending_work(self) -> bool:
        """后台 loop 空闲时不加载池快照；账本异常返回 True 以便下一轮暴露。"""
        try:
            if self._repo.count_queued():
                return True
            return any(
                not row.get("quarantine_reason")
                for row in self._repo.list_active_attempts()
            )
        except DB_ERRORS:
            return True

    def _default_blocked_models(self, data: Mapping[str, Any]) -> set[str]:
        """未显式确认默认 async 的模型不得进入就绪集合；API 之外的直调同样生效。"""
        from config import Config
        from core.video_async_impact import (
            ASYNC_CONFIRM_KEY,
            collect_async_impact,
            parse_confirmed_models,
            unconfirmed_async_models,
        )

        values = Config.CONFIG if isinstance(Config.CONFIG, dict) else {}
        providers = (
            values.get("api_providers")
            if isinstance(values.get("api_providers"), dict)
            else {}
        )
        models = {
            str(item.get("id")): item
            for item in values.get("custom_models") or []
            if isinstance(item, dict) and item.get("id")
        }
        confirmed = parse_confirmed_models(
            self._repo.get_system_meta(ASYNC_CONFIRM_KEY)
        )
        impacts = collect_async_impact(
            data,
            models=models,
            providers=providers,
            confirmed_models=confirmed,
        )
        return set(unconfirmed_async_models(impacts, confirmed_models=confirmed))

    def resolve_execution_pool(
        self,
        *,
        mode: str,
        explicit_profile_id: Optional[str] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        runtime: Optional[PoolRuntime] = None,
    ) -> PoolResolution:
        """新执行选池：默认池 busy 仍等、fallback 不粘会话、只返回快照对象。"""
        rt = runtime or self.runtime()
        ready = {
            profile_id
            for profile_id, profile in rt.profiles.items()
            if profile.enabled and rt.eligible_members(profile_id)
        }
        compatible = {
            profile_id
            for profile_id in ready
            if mode in rt.profiles[profile_id].contracts
        }
        choice = choose_pool(
            mode=mode,
            explicit_profile_id=explicit_profile_id,
            default_profiles=rt.default_profiles,
            ready_profiles=ready,
            compatible_profiles=compatible,
        )
        revision = (
            rt.profiles[choice.profile_id].revision
            if choice.profile_id and choice.profile_id in rt.profiles
            else 0
        )
        chosen_profile = rt.profiles.get(str(choice.profile_id or ""))
        contract = PreparedVideoContract(
            profile_id=choice.profile_id,
            revision=revision,
            mode=mode,
            selection_source=choice.source,
            recommendation_reason=choice.recommendation_reason,
            parameters=dict(parameters or {}),
            contract=dict(
                (chosen_profile.contracts.get(mode) if chosen_profile else {}) or {}
            ),
        )
        members = (
            tuple(
                member.instance_id
                for member in rt.eligible_members(choice.profile_id)
            )
            if choice.profile_id
            else ()
        )
        return PoolResolution(contract=contract, choice=choice, members=members)

    def enqueue_batch(
        self,
        *,
        owner: str,
        batch_id: str,
        jobs: Iterable[Dict[str, Any]],
        queue_timeout_seconds: int,
        max_queued_jobs: int,
    ):
        with self._state_lock:
            if self._stopped:
                raise DispatchUnavailable(
                    "受管派发已停用，暂不受理新作业",
                    error_code="dispatch_disabled",
                )
            job_rows = list(jobs)
            drained = {
                str(row.get("profile_id") or "")
                for row in job_rows
                if str(row.get("profile_id") or "") in self._draining_profiles
            }
            for row in job_rows:
                source_token = (
                    str(row.get("source_kind") or ""),
                    str(row.get("source_id") or ""),
                )
                if source_token in self._deleting_sources:
                    raise DispatchUnavailable(
                        "业务来源正在删除，拒绝新作业",
                        error_code="source_deleting",
                    )
                profile_id = str(row.get("profile_id") or "")
                members = {
                    str(item)
                    for item in ((row.get("snapshot_json") or {}).get("members") or [])
                }
                if not members:
                    raise DispatchUnavailable(
                        "作业快照缺少 members，拒绝入队",
                        error_code="pool_configuration_error",
                    )
                if members.issubset(self._draining_instances):
                    drained.add(profile_id)
            if drained:
                raise DispatchUnavailable(
                    "目标资源池正在排空，暂不受理新作业：" + ",".join(sorted(drained)),
                    error_code="pool_draining",
                )
            try:
                result = self._repo.enqueue_batch(
                    owner=owner,
                    batch_id=batch_id,
                    jobs=job_rows,
                    queue_timeout_seconds=queue_timeout_seconds,
                    max_queued_jobs=max_queued_jobs,
                )
                self._wake()
                return result
            except QueueFull as exc:
                raise DispatchUnavailable(
                    "调度队列已满，暂不受理新作业", error_code="queue_full"
                ) from exc
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用，暂不受理新作业：{exc}") from exc

    # ── 运行时快照 ──

    def runtime(self) -> PoolRuntime:
        try:
            data, revision = self._pool_loader()
            qualification_rows = self._repo.qualifications()
            occupied = self._repo.active_resource_keys()
            quarantined = self._repo.quarantined_resource_keys()
            batch_inflight = self._repo.batch_inflight_counts()
            last_dispatch = self._repo.last_dispatch_times()
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用，已停止新派发：{exc}") from exc

        now = self._clock()
        blocked_models: set[str] = set()
        if self._blocked_models_provider is not None:
            try:
                blocked_models = {
                    str(item) for item in self._blocked_models_provider(data)
                }
            except Exception as exc:  # noqa: BLE001 - 无法确认影响清单时停止派发
                raise DispatchUnavailable(
                    f"默认 async 影响清单不可用，已停止新派发：{exc}"
                ) from exc
        model_by_instance = {
            str(row.get("instance_id") or ""): str(row.get("model_id") or "")
            for row in data.get("video_instances") or []
            if isinstance(row, Mapping)
        }
        qualified = {
            instance_id
            for instance_id, evidence in qualification_rows.items()
            if model_by_instance.get(instance_id) not in blocked_models
            and qualification_is_valid(
                evidence,
                config_revision=revision,
                boot_id=self._boot_id,
                now=now,
            )
        }
        return build_pool_runtime(
            data,
            revision=revision,
            dispatch_enabled=bool(self._dispatch_enabled()) and not self._stopped,
            qualified=qualified,
            occupied=occupied,
            quarantined=quarantined,
            batch_inflight=batch_inflight,
            last_dispatch=last_dispatch,
            draining_profiles=frozenset(self._draining_profiles),
            draining_instances=frozenset(self._draining_instances),
            blocked_models=frozenset(blocked_models),
        )

    # ── 派发 ──

    def dispatch_once(
        self,
        transport_factory: Callable[[RuntimeInstance, Mapping[str, Any]], DispatchTransport],
    ) -> DispatchReport:
        report = DispatchReport()
        if self._stopped:
            report.disabled = True
            return report
        runtime = self.runtime()
        if not runtime.dispatch_enabled:
            report.disabled = True
            return report
        try:
            queued = self._repo.list_queued()
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用，已停止新派发：{exc}") from exc
        if self._probe_service is not None and queued:
            # 只探测有排队作业的池成员；无队列不扫描全部实例。
            profile_ids = {
                str(job.get("profile_id") or "") for job in queued
            }
            targets: List[RuntimeInstance] = []
            seen: set[str] = set()
            for profile_id in profile_ids:
                for instance in runtime.eligible_members(profile_id):
                    if instance.instance_id in seen:
                        continue
                    seen.add(instance.instance_id)
                    targets.append(instance)
            if targets:
                try:
                    self._probe_service.tick(targets, waiting=True)
                except Exception:  # noqa: BLE001 - 探针失败不阻断派发
                    logger.warning("probe tick failed", exc_info=True)

        waiting = self._expire_or_block(queued, runtime, report)
        capacity = self.capacity_snapshot(runtime, waiting)
        report.posts = 0
        owner_queues: "OrderedDict[str, Deque[Dict[str, Any]]]" = OrderedDict()
        for job in waiting:
            owner_queues.setdefault(str(job["owner"]), deque()).append(job)
        if not owner_queues:
            return report

        occupied = set(runtime.occupied_keys)
        blocked = set(runtime.quarantined_keys)
        batch_counts: Dict[str, int] = dict(runtime.batch_inflight)
        total_inflight = len(runtime.occupied_keys)
        blocked_jobs: set[str] = set()
        owners = deque(owner_queues.keys())

        while owners and total_inflight < runtime.global_limit:
            progressed = 0
            for _ in range(len(owners)):
                if total_inflight >= runtime.global_limit:
                    break
                owner = owners[0]
                owners.rotate(-1)
                job = self._next_job(
                    owner_queues[owner],
                    blocked_jobs,
                    batch_counts,
                    runtime,
                    capacity,
                )
                if job is None:
                    continue
                outcome = self._attempt_dispatch(
                    job,
                    runtime=runtime,
                    transport_factory=transport_factory,
                    occupied=occupied,
                    blocked=blocked,
                    batch_counts=batch_counts,
                    report=report,
                )
                if outcome == "granted":
                    capacity.consume(self._capacity_job(job))
                    progressed += 1
                    total_inflight += 1
                    self._pop_job(owner_queues[owner], job["job_id"])
                    continue
                if outcome in ("released", "terminal"):
                    self._pop_job(owner_queues[owner], job["job_id"])
                    continue
                blocked_jobs.add(str(job["job_id"]))
            if progressed == 0:
                break
        return report

    def _expire_or_block(
        self,
        queued: Sequence[Mapping[str, Any]],
        runtime: PoolRuntime,
        report: DispatchReport,
    ) -> List[Dict[str, Any]]:
        """先处理取消、期限与显式下线，其余保持原池等待。"""
        now_ms = int(self._clock() * 1000)
        waiting: List[Dict[str, Any]] = []
        for job in queued:
            job_id = str(job["job_id"])
            if job.get("cancel_requested"):
                self._safe_state(job_id, "cancelled", expected=("queued",))
                report.add("cancelled", job_id=job_id)
                continue
            profile = runtime.profiles.get(str(job.get("profile_id") or ""))
            if profile is None:
                # 配置缺失不是管理员下线：单独报配置错误，避免与 pool_unavailable 混同。
                self._fail_job(
                    job_id,
                    "pool_configuration_error",
                    "作业引用的资源池在当前配置中不存在",
                    job=job,
                )
                report.add(
                    "failed", job_id=job_id, error_code="pool_configuration_error"
                )
                continue
            if not profile.enabled:
                self._fail_job(
                    job_id,
                    "pool_unavailable",
                    "资源池已下线",
                    job=job,
                )
                report.add("failed", job_id=job_id, error_code="pool_unavailable")
                continue
            snapshot = job.get("snapshot_json") or {}
            members = snapshot.get("members") if isinstance(snapshot, dict) else None
            if not isinstance(members, (list, tuple)) or not members:
                # 缺失成员快照属于配置/受理缺陷，不能静默等待到一小时超时。
                self._fail_job(
                    job_id, "pool_configuration_error", "作业缺失成员快照", job=job
                )
                report.add(
                    "failed", job_id=job_id, error_code="pool_configuration_error"
                )
                continue
            decision = evaluate_queued_job(
                now=now_ms,
                queue_deadline_at=int(job.get("queue_deadline_at") or 0),
                pool_online=bool(self._job_members(job, runtime)),
                admin_offline=False,
            )
            if decision.state == QUEUE_TIMEOUT:
                self._fail_job(job_id, "queue_timeout", "等待资源超时", job=job)
                report.add("failed", job_id=job_id, error_code="queue_timeout")
                continue
            if decision.state == WAITING_POOL_UNAVAILABLE:
                # 临时无合格成员：保留原池与期限，仅标记等待原因。
                report.add("waiting_pool_unavailable", job_id=job_id)
            waiting.append(dict(job))
        return waiting

    def _next_job(
        self,
        queue: Deque[Dict[str, Any]],
        blocked_jobs: set[str],
        batch_counts: Mapping[str, int],
        runtime: PoolRuntime,
        capacity: Optional[CapacitySnapshot] = None,
    ) -> Optional[Dict[str, Any]]:
        for job in list(queue):
            job_id = str(job["job_id"])
            if job_id in blocked_jobs:
                continue
            limit = self._execution_limit(job, runtime)
            used = int(batch_counts.get(str(job.get("batch_id") or ""), 0))
            if limit <= 0 or used >= limit:
                continue
            if self._contract_missing(job, runtime):
                continue
            snapshot = job.get("snapshot_json") or {}
            if (
                capacity is not None
                and not (
                    isinstance(snapshot, dict)
                    and snapshot.get("diagnostic") is True
                )
                and capacity.free_for(self._capacity_job(job)) <= 0
            ):
                continue
            return job
        return None

    def _capacity_job(self, job: Mapping[str, Any]) -> DispatchJob:
        snapshot = job.get("snapshot_json") or {}
        mode = str(snapshot.get("mode") or "first_frame") if isinstance(snapshot, dict) else "first_frame"
        return DispatchJob(
            job_id=str(job["job_id"]),
            owner=str(job.get("owner") or ""),
            session_id=str(job.get("source_id") or ""),
            execution_id=str(job.get("execution_id") or ""),
            profile_id=str(job.get("profile_id") or ""),
            mode=mode,
            accepted_at=int(job.get("accepted_at") or 0) / 1000.0,
            queue_deadline_at=int(job.get("queue_deadline_at") or 0) / 1000.0,
        )

    def capacity_snapshot(
        self,
        runtime: PoolRuntime,
        jobs: Sequence[Mapping[str, Any]],
    ) -> CapacitySnapshot:
        """生产容量快照：池槽、模式、执行上限与全局在途同一口径。"""
        profile_slots = {
            profile_id: len(
                {member.resource_key for member in runtime.eligible_members(profile_id)}
            )
            for profile_id in runtime.profiles
        }
        profile_modes = {
            profile_id: set(profile.contracts) for profile_id, profile in runtime.profiles.items()
        }
        limits = {}
        for job in jobs:
            execution_id = str(job.get("execution_id") or "")
            if execution_id:
                limits[execution_id] = self._execution_limit(job, runtime)
        try:
            execution_active = self._repo.execution_inflight_counts()
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
        return CapacitySnapshot(
            profile_slots=profile_slots,
            profile_modes=profile_modes,
            per_execution_limits=limits,
            execution_active=execution_active,
            global_limit=runtime.global_limit,
            active_global=len(runtime.occupied_keys),
        )

    def _execution_limit(self, job: Mapping[str, Any], runtime: PoolRuntime) -> int:
        profile = runtime.profiles.get(str(job.get("profile_id") or ""))
        if profile is None or not profile.enabled:
            return 0
        members = self._job_members(job, runtime)
        capacity = len({member.resource_key for member in members})
        if capacity <= 0:
            return 0
        snapshot = job.get("snapshot_json") or {}
        if isinstance(snapshot, str):
            snapshot = {}
        enable = snapshot.get("enable_concurrency", True)
        if enable is False:
            return 1
        snapshot_cap = _clamp_int(
            snapshot.get("max_concurrency_per_execution"), default=capacity
        )
        return max(
            0,
            min(capacity, snapshot_cap, profile.max_concurrency_per_execution, runtime.global_limit),
        )

    def _contract_missing(self, job: Mapping[str, Any], runtime: PoolRuntime) -> bool:
        profile = runtime.profiles.get(str(job.get("profile_id") or ""))
        if profile is None:
            return True
        snapshot = job.get("snapshot_json") or {}
        if isinstance(snapshot, str):
            return True
        mode = str(snapshot.get("mode") or "").strip()
        if mode and mode not in profile.contracts:
            return True
        return False

    def _job_members(
        self, job: Mapping[str, Any], runtime: PoolRuntime
    ) -> Tuple[RuntimeInstance, ...]:
        profile_id = str(job.get("profile_id") or "")
        members = runtime.eligible_members(profile_id)
        snapshot = job.get("snapshot_json") or {}
        if isinstance(snapshot, str):
            return ()
        recorded = snapshot.get("members")
        if not isinstance(recorded, (list, tuple)) or not recorded:
            # 缺失不可变成员快照时不能退回“当前全部成员”，否则等于放开批次边界。
            return ()
        if snapshot.get("diagnostic") is True:
            # 5.4 管理员真实诊断只绕过“资格证据”筛选，不绕过启用/排空/槽位/契约。
            selected = []
            for instance_id in recorded:
                instance = runtime.instances.get(str(instance_id))
                if instance is None or not instance.enabled:
                    continue
                if str(instance_id) in runtime.draining_instances:
                    continue
                if instance.model_id in runtime.blocked_models:
                    # async 影响未确认是准入门禁，诊断也不能绕过。
                    continue
                selected.append(instance)
            return tuple(selected)
        profile = runtime.profiles.get(profile_id)
        batch_snapshot = BatchSnapshot(
            profile_id=profile_id,
            revision=int(job.get("profile_revision") or 0),
            members=tuple(str(item) for item in recorded),
        )
        keep = set(
            effective_members(
                batch_snapshot,
                current_profile_members=profile.members if profile else (),
                eligible_instances=[member.instance_id for member in members],
            )
        )
        return tuple(member for member in members if member.instance_id in keep)

    def _attempt_dispatch(
        self,
        job: Mapping[str, Any],
        *,
        runtime: PoolRuntime,
        transport_factory: Callable[[RuntimeInstance, Mapping[str, Any]], DispatchTransport],
        occupied: set[str],
        blocked: set[str],
        batch_counts: Dict[str, int],
        report: DispatchReport,
    ) -> str:
        members = [
            member
            for member in self._job_members(job, runtime)
            if member.resource_key not in occupied and member.resource_key not in blocked
        ]
        members.sort(
            key=lambda item: (
                0 if item.access_mode == "exclusive" else 1,
                int(runtime.last_dispatch.get(item.resource_key, 0)),
                item.instance_id,
            )
        )
        for instance in members:
            # shared 的两次 idle 必须在预留前判断，避免每 tick 先占位再释放、
            # 污染 attempt_no 与最近派发时间；最终确认仍在预留后执行。
            if instance.access_mode == "shared" and not self._shared_ready(instance, report):
                blocked.add(instance.resource_key)
                continue
            try:
                attempt = self._repo.reserve(
                    job_id=str(job["job_id"]),
                    resource_key=instance.resource_key,
                    instance_id=instance.instance_id,
                    boot_id=self._boot_id,
                    config_revision=runtime.revision,
                    endpoint_snapshot={
                        "model_id": instance.model_id,
                        "access_mode": instance.access_mode,
                        "driver": instance.driver,
                    },
                )
            except SlotBusy:
                occupied.add(instance.resource_key)
                continue
            except ReservationError as exc:
                report.add("reservation_refused", job_id=str(job["job_id"]), reason=str(exc))
                return "terminal"
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用，已停止新派发：{exc}") from exc

            if not self._post_reserve_admissible(instance, attempt, report):
                self._repo.mark_attempt_terminal(
                    attempt_id=str(attempt["attempt_id"]), reason="admission_refused"
                )
                self._release(attempt)
                self._safe_state(
                    str(job["job_id"]), "queued", expected=("reserved", "submitting")
                )
                blocked.add(instance.resource_key)
                continue

            dispatch_job = job
            if self._job_input_preparer is not None:
                try:
                    dispatch_job = self._job_input_preparer(job, attempt)
                except Exception as exc:  # noqa: BLE001 - 物化失败不得 POST
                    self._repo.cancel_prepared_attempt(
                        attempt_id=str(attempt["attempt_id"]),
                        reason="input_materialization_failed",
                    )
                    self._fail_job(
                        str(job["job_id"]),
                        "input_materialization_failed",
                        f"输入物化失败：{str(exc)[:300]}",
                        job=job,
                    )
                    report.failed += 1
                    report.add(
                        "failed",
                        job_id=str(job["job_id"]),
                        error_code="input_materialization_failed",
                    )
                    return "terminal"
            if not self._repo.begin_submission(
                job_id=str(job["job_id"]), attempt_id=str(attempt["attempt_id"])
            ):
                # 取消/状态变化在发送前赢了：不得 POST，也不得把 reserved 留在队列外。
                self._repo.mark_attempt_terminal(
                    attempt_id=str(attempt["attempt_id"]), reason="send_right_lost"
                )
                self._release(attempt)
                current = self._repo.get_job(str(job["job_id"]))
                if current is not None and not current.get("cancel_requested"):
                    self._safe_state(
                        str(job["job_id"]), "queued", expected=("reserved",)
                    )
                report.add(
                    "send_right_lost",
                    job_id=str(job["job_id"]),
                    attempt_id=str(attempt["attempt_id"]),
                )
                return "released"
            self._inject_crash("after_submitting", job, attempt)

            grant = self._grants.issue(
                job_id=str(job["job_id"]),
                attempt_id=str(attempt["attempt_id"]),
                instance_id=instance.instance_id,
                resource_key=instance.resource_key,
                ttl_seconds=60.0,
                now=self._clock(),
            )
            try:
                transport = transport_factory(instance, dispatch_job)
            except Exception as exc:  # noqa: BLE001 - 未创建传输层即证明未发送
                self._repo.mark_attempt_terminal(
                    attempt_id=str(attempt["attempt_id"]),
                    reason="transport_init_failed",
                    error={"error_type": type(exc).__name__, "error": str(exc)[:300]},
                )
                self._release(attempt)
                self._safe_state(
                    str(job["job_id"]), "queued", expected=("submitting",)
                )
                report.add(
                    "transport_init_failed",
                    job_id=str(job["job_id"]),
                    attempt_id=str(attempt["attempt_id"]),
                )
                return "released"
            try:
                outcome = transport.submit(
                    job=dispatch_job, instance=instance, grant=grant
                )
            except Exception as exc:  # noqa: BLE001 - 传输层异常必须分类，不能重发
                outcome = SubmitOutcome(
                    classify_submit_exception(exc),
                    detail={"error_type": type(exc).__name__, "error": str(exc)[:300]},
                )
            finally:
                _close_quietly(transport)
            report.posts += 1
            self._inject_crash("after_submit", job, attempt, outcome=outcome)
            return self._finish_submit(job, attempt, instance, outcome, batch_counts, report)
        return "wait"

    def _finish_submit(
        self,
        job: Mapping[str, Any],
        attempt: Mapping[str, Any],
        instance: RuntimeInstance,
        outcome: SubmitOutcome,
        batch_counts: Dict[str, int],
        report: DispatchReport,
    ) -> str:
        job_id = str(job["job_id"])
        attempt_id = str(attempt["attempt_id"])
        if outcome.status == SUBMIT_ACCEPTED and outcome.remote_job_id:
            try:
                self._repo.record_submitted(
                    attempt_id=attempt_id, remote_job_id=outcome.remote_job_id
                )
                self._safe_state(job_id, "remote_queued", expected=("reserved", "submitting", "reconciling"))
            except DB_ERRORS as exc:
                raise DispatchUnavailable(
                    f"远端已受理但账本不可用，停止新派发：{exc}"
                ) from exc
            batch = str(job.get("batch_id") or "")
            batch_counts[batch] = int(batch_counts.get(batch, 0)) + 1
            report.submitted += 1
            report.add(
                "submitted",
                job_id=job_id,
                attempt_id=attempt_id,
                instance_id=instance.instance_id,
                resource_key=instance.resource_key,
                remote_job_id=outcome.remote_job_id,
            )
            return "granted"
        if outcome.status == SUBMIT_REJECTED:
            try:
                self._repo.mark_attempt_terminal(
                    attempt_id=attempt_id, reason="rejected", error=outcome.detail
                )
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._release(attempt)
            attempt_no = int(attempt.get("attempt_no") or 1)
            retry_allowed = attempt_no <= self._max_safe_retries
            if retry_allowed:
                self._safe_state(job_id, "queued", expected=("reserved", "submitting"))
                report.rejected += 1
                report.add("rejected", job_id=job_id, attempt_id=attempt_id)
            else:
                self._fail_job(job_id, "submit_rejected", "服务端明确拒绝提交", job=job)
                report.rejected += 1
                report.add("failed", job_id=job_id, error_code="submit_rejected")
            return "released"
        if outcome.remote_job_id:
            # 提交结果未知但已拿到远端 ID：记下 ID，允许后续 poll 恢复，
            # 不能按无 ID 的 dispatch_unknown 直接终结。
            try:
                self._repo.record_submitted(
                    attempt_id=attempt_id, remote_job_id=outcome.remote_job_id
                )
                self._safe_state(
                    job_id, "unknown", expected=("reserved", "submitting", "reconciling")
                )
            except DB_ERRORS as exc:
                raise DispatchUnavailable(
                    f"远端已受理但账本不可用，停止新派发：{exc}"
                ) from exc
            batch = str(job.get("batch_id") or "")
            batch_counts[batch] = int(batch_counts.get(batch, 0)) + 1
            report.unknown += 1
            report.add(
                "unknown",
                job_id=job_id,
                attempt_id=attempt_id,
                remote_job_id=outcome.remote_job_id,
            )
            return "granted"
        # 未知提交：保留槽位，禁止自动换实例或换创建变体重发。
        try:
            self._repo.mark_attempt_unknown(
                attempt_id=attempt_id, reason="submit_unknown", error=outcome.detail
            )
            self._repo.quarantine_slot(
                resource_key=instance.resource_key,
                reason="submit_unknown",
                detail={"attempt_id": attempt_id, "job_id": job_id},
            )
            # 无 remote id 的提交无法再核对：直接落终态，避免进程在
            # dispatch/reconcile 之间崩溃后留下永远 unknown 的作业。
            self._fail_job(
                job_id,
                "dispatch_unknown",
                "提交结果未知且无远端 ID，作业已隔离待人工核对",
                job=job,
            )
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
        report.unknown += 1
        report.quarantined += 1
        batch = str(job.get("batch_id") or "")
        batch_counts[batch] = int(batch_counts.get(batch, 0)) + 1
        report.add(
            "unknown",
            job_id=job_id,
            attempt_id=attempt_id,
            resource_key=instance.resource_key,
        )
        return "granted"

    def _shared_ready(
        self,
        instance: RuntimeInstance,
        report: DispatchReport,
    ) -> bool:
        """预留前的 shared 闸门：只消费两次独立 idle 的证据。"""
        sample = self._shared_observer(instance) if self._shared_observer else None
        self._shared.add(instance.instance_id, sample)
        if not self._shared.two_idle(instance.instance_id, self._clock()):
            report.add("shared_wait", instance_id=instance.instance_id, reason="need_two_idle")
            return False
        return True

    def _post_reserve_admissible(
        self,
        instance: RuntimeInstance,
        attempt: Mapping[str, Any],
        report: DispatchReport,
    ) -> bool:
        """预留后的复查：exclusive 对账本矛盾隔离；shared 做提交前确认。"""
        now = self._clock()
        if instance.access_mode == "exclusive":
            sample = self._exclusive_observer(instance) if self._exclusive_observer else None
            if sample is not None and sample.state == "busy" and now - sample.observed_at <= 15:
                self._repo.quarantine_slot(
                    resource_key=instance.resource_key,
                    reason="exclusive_ledger_conflict",
                    detail={"instance_id": instance.instance_id, "observed_at": sample.observed_at},
                )
                report.quarantined += 1
                report.add("quarantined", instance_id=instance.instance_id)
                return False
            return True
        confirmation = (
            self._shared_observer(instance) if self._shared_observer is not None else None
        )
        self._shared.add(instance.instance_id, confirmation)
        if confirmation is None or confirmation.state != "idle":
            self._shared.clear(instance.instance_id)
            report.add(
                "shared_wait",
                instance_id=instance.instance_id,
                reason="final_confirmation_missing",
            )
            return False
        return True

    # ── 核对与心跳 ──

    def reconcile_once(
        self,
        transport_factory: Callable[[RuntimeInstance, Mapping[str, Any]], DispatchTransport],
    ) -> DispatchReport:
        report = DispatchReport()
        runtime = self.runtime()
        self.detect_stale(report=report)
        self.heartbeat_once(report=report)
        try:
            attempts = self._repo.list_active_attempts()
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用，已停止派发：{exc}") from exc

        for attempt in attempts:
            instance = runtime.instances.get(str(attempt.get("instance_id") or ""))
            if instance is None:
                # 实例被整体删除：保留槽位并隔离，不查询/不重发，等待管理员核对。
                self._quarantine_attempt(attempt, "instance_removed", report)
                continue
            state = str(attempt.get("submission_state") or "")
            remote_id = str(attempt.get("remote_job_id") or "")
            if not remote_id:
                if state in ("prepared", "submitting"):
                    # 崩溃残留：只隔离等待人工核对，不重发。
                    self._quarantine_attempt(attempt, "submission_unknown", report)
                elif state == "unknown":
                    # 提交结果未知且无远端 ID：无法再查询，转隔离并结束作业。
                    # 槽位保留在隔离区，不释放、不重发；本地等待必须有终点。
                    self._quarantine_attempt(attempt, "submission_unknown", report)
                    self._fail_job(
                        str(attempt["job_id"]),
                        "dispatch_unknown",
                        "提交结果未知且无远端 ID，作业已隔离待人工核对",
                        attempt=attempt,
                    )
                continue
            job = self._job_from_attempt(attempt)
            transport = transport_factory(instance, job)
            try:
                outcome = transport.poll(attempt=attempt, instance=instance, job=job)
            except Exception as exc:  # noqa: BLE001 - 查询失败不等于终态
                report.add(
                    "poll_failed",
                    attempt_id=str(attempt.get("attempt_id")),
                    error_type=type(exc).__name__,
                )
                outcome = PollOutcome("unknown", detail={"error": str(exc)[:300]})
            finally:
                _close_quietly(transport)
            if outcome.state == "pending":
                if state == "cancelled":
                    if self._cancel_grace_expired(attempt):
                        # 取消已确认但远端长期不终结：转隔离供管理员核对，不能无限静默占用。
                        self._quarantine_cancel_pending(attempt, report)
                else:
                    self._safe_state(
                        str(attempt["job_id"]),
                        "running",
                        expected=("remote_queued", "running", "submitting"),
                    )
                report.add("running", job_id=str(attempt["job_id"]))
                continue
            if outcome.state == "success":
                self._complete_attempt(attempt, instance, job, transport_factory, report)
                continue
            if outcome.state == "failure":
                cancel_confirmed = str(attempt.get("submission_state") or "") == "cancelled"
                if may_release_slot(
                    status="failed",
                    drain_confirmed=outcome.drain_confirmed,
                    cancel_confirmed=cancel_confirmed,
                ):
                    self._repo.mark_attempt_terminal(
                        attempt_id=str(attempt["attempt_id"]),
                        reason="remote_failed",
                        error=outcome.detail,
                    )
                    self._release(attempt)
                    if attempt.get("cancel_requested"):
                        self._safe_state(
                            str(attempt["job_id"]),
                            "cancelled",
                            expected=("reconciling", "running", "remote_queued", "unknown"),
                        )
                        report.add("cancelled", job_id=str(attempt["job_id"]))
                    else:
                        self._fail_job(
                            str(attempt["job_id"]),
                            "remote_failed",
                            "远端生成失败",
                            attempt=attempt,
                        )
                        report.failed += 1
                else:
                    # failed/deleted/404 不能证明引擎已停止：隔离并等待排空确认。
                    self._quarantine_attempt(attempt, "engine_drain_unconfirmed", report)
                continue
            # 查询失败不等于终态：保留槽位，把原因落账供管理员诊断，不重发。
            if str(attempt.get("submission_state") or "") == "cancelled":
                # 取消确认后持续 unknown 与 pending 同样受 grace 约束。
                if self._cancel_grace_expired(attempt):
                    self._quarantine_cancel_pending(attempt, report)
            else:
                self._repo.mark_attempt_unknown(
                    attempt_id=str(attempt["attempt_id"]),
                    reason="poll_unknown",
                    error=outcome.detail,
                )
                self._safe_state(
                    str(attempt["job_id"]),
                    "unknown",
                    expected=("remote_queued", "running", "unknown", "reconciling"),
                )
            report.add("unknown", attempt_id=str(attempt.get("attempt_id")))
        return report

    def _complete_attempt(
        self,
        attempt: Mapping[str, Any],
        instance: RuntimeInstance,
        job: Mapping[str, Any],
        transport_factory: Callable[[RuntimeInstance, Mapping[str, Any]], DispatchTransport],
        report: DispatchReport,
    ) -> None:
        # 先确认业务来源仍存在；查询失败时 fail-closed，保留槽等待下一轮核对。
        source_token = (
            str(job.get("source_kind") or ""),
            str(job.get("source_id") or ""),
        )
        try:
            source_deleted = (
                source_token in self._deleting_sources
                or self._repo.is_source_deleting(*source_token)
                or self._repo.is_source_deleted(*source_token)
            )
        except DB_ERRORS:
            self._safe_state(
                str(attempt["job_id"]),
                "reconciling",
                expected=("remote_queued", "running", "unknown"),
            )
            report.add("source_check_failed", job_id=str(attempt["job_id"]))
            return
        # 计算已确认结束：先释放推理槽，下载/发布不占 GPU。
        self._repo.mark_attempt_terminal(
            attempt_id=str(attempt["attempt_id"]), reason="remote_success"
        )
        self._release(attempt)
        if source_deleted:
            self._safe_state(
                str(attempt["job_id"]),
                "cancelled",
                error=dispatch_error_payload(
                    "source_deleted",
                    "业务对象已删除，迟到结果丢弃",
                    mode=str(job.get("dispatch_mode") or "auto"),
                    profile_id=job.get("profile_id"),
                    execution_id=job.get("execution_id"),
                ),
            )
            report.add("late_result_discarded", job_id=str(attempt["job_id"]))
            return
        if attempt.get("cancel_requested"):
            # 用户已停止：只回收资源，不把停止的业务重新标成完成。
            self._safe_state(
                str(attempt["job_id"]),
                "cancelled",
                expected=("reconciling", "downloading", "running", "remote_queued", "unknown"),
            )
            report.add("cancelled_after_success", job_id=str(attempt["job_id"]))
            return
        self._safe_state(str(attempt["job_id"]), "downloading", expected=("remote_queued", "running", "unknown"))
        last_error: Any = None
        for index in range(self._download_attempts):
            transport = None
            try:
                transport = transport_factory(instance, job)
                result = transport.download(attempt=attempt, instance=instance, job=job)
            except Exception as exc:  # noqa: BLE001 - 只重试下载，不重新生成
                last_error = {"error_type": type(exc).__name__, "error": str(exc)[:300]}
                if index + 1 < self._download_attempts:
                    self._sleep(_download_backoff(index))
                continue
            finally:
                if transport is not None:
                    _close_quietly(transport)
            published = None
            if self._result_publisher is not None:
                try:
                    published = self._result_publisher(job, attempt, result)
                except Exception as exc:  # noqa: BLE001 - 发布失败不能误标成功
                    self._discard_output(result)
                    self._safe_state(
                        str(attempt["job_id"]),
                        "failed",
                        error=dispatch_error_payload(
                            "publish_failed",
                            f"结果发布失败：{str(exc)[:300]}",
                            mode=str(job.get("dispatch_mode") or "auto"),
                            profile_id=job.get("profile_id"),
                            execution_id=job.get("execution_id"),
                        ),
                    )
                    report.failed += 1
                    report.add(
                        "failed",
                        job_id=str(attempt["job_id"]),
                        error_code="publish_failed",
                    )
                    return
            result_payload = {"download": result}
            if published is not None:
                result_payload["published"] = published
            self._safe_state(
                str(attempt["job_id"]), "succeeded", result=result_payload
            )
            report.downloaded += 1
            report.completed += 1
            report.add("succeeded", job_id=str(attempt["job_id"]))
            return
        self._safe_state(
            str(attempt["job_id"]),
            "failed",
            error=dispatch_error_payload(
                DOWNLOAD_FAILED,
                "结果下载失败",
                mode=str(attempt.get("dispatch_mode") or "auto"),
                profile_id=attempt.get("profile_id"),
                execution_id=attempt.get("execution_id"),
            ),
        )
        report.failed += 1
        report.add("failed", job_id=str(attempt["job_id"]), error_code=DOWNLOAD_FAILED)

    def heartbeat_once(self, *, report: Optional[DispatchReport] = None) -> int:
        """续租当前仍在受理的尝试；失联检测由 ``detect_stale`` 单独负责。"""
        now_ms = int(self._clock() * 1000)
        try:
            attempts = self._repo.list_active_attempts()
            updated = 0
            for attempt in attempts:
                if attempt.get("quarantine_reason"):
                    # 已隔离的槽等待人工核对，不再续租伪装成活任务。
                    continue
                heartbeat_at = attempt.get("heartbeat_at")
                if (
                    heartbeat_at is not None
                    and now_ms - int(heartbeat_at) < int(self._heartbeat_interval * 1000)
                ):
                    continue
                if self._repo.heartbeat(
                    resource_key=str(attempt["resource_key"]),
                    attempt_id=str(attempt["attempt_id"]),
                    token=str(attempt.get("slot_token") or ""),
                ):
                    updated += 1
            return updated
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def detect_stale(self, *, report: Optional[DispatchReport] = None) -> int:
        """心跳超时只隔离核对，不自动清槽；不先续租，避免把旧孤儿当成活任务。"""
        now_ms = int(self._clock() * 1000)
        try:
            stale = self._repo.stale_active_attempts(
                older_than_ms=int(self._heartbeat_stale * 1000), now=now_ms
            )
            quarantined = 0
            for row in stale:
                if self._repo.quarantine_slot(
                    resource_key=str(row["resource_key"]),
                    reason="heartbeat_stale",
                    detail={"attempt_id": str(row["attempt_id"])},
                ):
                    quarantined += 1
                    if report is not None:
                        report.quarantined += 1
                        report.add("quarantined", resource_key=str(row["resource_key"]))
            return quarantined
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def _cancel_grace_expired(self, attempt: Mapping[str, Any]) -> bool:
        confirmed_at = (attempt.get("error_json") or {}).get("confirmed_at")
        if confirmed_at is None:
            return False
        return int(self._clock() * 1000) - int(confirmed_at) >= int(
            self._cancel_grace * 1000
        )

    def _quarantine_cancel_pending(
        self, attempt: Mapping[str, Any], report: DispatchReport
    ) -> None:
        if attempt.get("quarantine_reason"):
            return
        if self._repo.quarantine_slot(
            resource_key=str(attempt["resource_key"]),
            reason="cancel_confirmed_pending",
            detail={
                "attempt_id": str(attempt["attempt_id"]),
                "confirmed_at": (attempt.get("error_json") or {}).get("confirmed_at"),
            },
        ):
            report.quarantined += 1
            report.add(
                "quarantined",
                reason="cancel_confirmed_pending",
                attempt_id=str(attempt["attempt_id"]),
            )

    def _quarantine_attempt(
        self,
        attempt: Mapping[str, Any],
        reason: str,
        report: DispatchReport,
    ) -> None:
        if attempt.get("quarantine_reason"):
            # 已隔离的槽/尝试不重复计数，也不重复改写状态。
            return
        try:
            self._repo.mark_attempt_unknown(
                attempt_id=str(attempt["attempt_id"]), reason=reason
            )
            self._repo.quarantine_slot(
                resource_key=str(attempt["resource_key"]),
                reason=reason,
                detail={"attempt_id": str(attempt["attempt_id"])},
            )
            self._safe_state(
                str(attempt["job_id"]),
                "reconciling",
                expected=("reserved", "submitting", "remote_queued", "running", "unknown"),
            )
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
        report.quarantined += 1
        report.add("quarantined", attempt_id=str(attempt["attempt_id"]), reason=reason)

    # ── 启动恢复与收尾动作 ──

    def recover_startup(self, *, interrupt_queued: bool = True) -> Dict[str, int]:
        """先恢复槽位与未决尝试，再开放派发；不自动重发、不清槽。"""
        try:
            quarantined = self._repo.recover_startup(boot_id=self._boot_id)
            residue = 0
            for attempt in self._repo.unresolved_attempts():
                state = str(attempt.get("submission_state") or "")
                if state == "unknown" and not str(attempt.get("remote_job_id") or ""):
                    # 旧版本遗留：无 remote id 的 unknown 无法核对，启动时收口。
                    self._fail_job(
                        str(attempt["job_id"]),
                        "dispatch_unknown",
                        "提交结果未知且无远端 ID，作业已隔离待人工核对",
                        attempt=attempt,
                    )
                    continue
                if state not in ("prepared", "submitting"):
                    continue
                self._repo.mark_attempt_unknown(
                    attempt_id=str(attempt["attempt_id"]), reason="startup_residue"
                )
                self._repo.quarantine_slot(
                    resource_key=str(attempt["resource_key"]),
                    reason="startup_residue",
                    detail={"attempt_id": str(attempt["attempt_id"])},
                )
                self._repo.set_job_state(
                    job_id=str(attempt["job_id"]),
                    state="reconciling",
                    expected_states=("reserved", "submitting"),
                )
                residue += 1
            interrupted = 0
            if interrupt_queued:
                interrupted = self._repo.fail_queued_jobs(
                    job_ids=[
                        str(row["job_id"])
                        for row in self._repo.list_queued()
                    ],
                    error_code="interrupted",
                    reason="后端重启，未派发作业由用户确认重做",
                    payload=dispatch_error_payload(
                        "interrupted", "后端重启，未派发作业由用户确认重做"
                    ),
                )
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用，不能开放派发：{exc}") from exc
        return {
            "quarantined_old_boot": quarantined,
            "quarantined_residue": residue,
            "interrupted_queued": interrupted,
        }

    def begin_source_delete(self, source_kind: str, source_id: str) -> None:
        key = (str(source_kind), str(source_id))
        # DB+内存必须在同一把锁内完成；enqueue 持有同一把锁检查内存。
        with self._state_lock:
            try:
                self._repo.mark_source_deleting(*key)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._deleting_sources.add(key)

    def end_source_delete(self, source_kind: str, source_id: str) -> None:
        key = (str(source_kind), str(source_id))
        with self._state_lock:
            try:
                self._repo.clear_source_deleting(*key)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._deleting_sources.discard(key)

    def reconcile_pending_deletions(self, session_exists: Callable[[str], bool]) -> Dict[str, int]:
        """启动恢复：残留 deleting 若业务对象仍在则回滚，否则补写 tombstone。"""
        restored = 0
        tombstoned = 0
        for kind, source_id in self._repo.list_source_deleting():
            if kind == "workflow" and session_exists(source_id):
                self.end_source_delete(kind, source_id)
                restored += 1
            else:
                self.mark_source_deleted(kind, source_id)
                tombstoned += 1
            with self._state_lock:
                self._deleting_sources.discard((kind, source_id))
        return {"restored": restored, "tombstoned": tombstoned}

    def mark_source_deleted(self, source_kind: str, source_id: str) -> None:
        try:
            self._repo.mark_source_deleted(source_kind, source_id)
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
        self.end_source_delete(source_kind, source_id)

    def cancel_source(
        self,
        source_kind: str,
        source_id: str,
        *,
        transport_factory: Optional[
            Callable[[RuntimeInstance, Mapping[str, Any]], DispatchTransport]
        ] = None,
    ) -> Dict[str, Any]:
        """业务对象删除/停止：逐个取消，失败不阻断其余作业。"""
        jobs = self._repo.list_jobs_by_source(source_kind, source_id)
        stats: Dict[str, Any] = {
            "total": len(jobs),
            "cancelled": 0,
            "reconciling": 0,
            "already_terminal": 0,
            "failed": 0,
            "errors": [],
        }
        for job in jobs:
            try:
                result = self.cancel_job(
                    str(job["job_id"]), transport_factory=transport_factory
                )
                state = str(result.get("state") or "")
                if state == "cancelled":
                    stats["cancelled"] += 1
                elif state == "reconciling":
                    stats["reconciling"] += 1
                elif state in ("failed", "succeeded"):
                    stats["already_terminal"] += 1
            except Exception as exc:  # noqa: BLE001 - 尽量取消全部，统计失败
                stats["failed"] += 1
                stats["errors"].append(
                    {"job_id": str(job["job_id"]), "error": str(exc)[:300]}
                )
        return stats

    def cancel_job(
        self,
        job_id: str,
        *,
        transport_factory: Optional[
            Callable[[RuntimeInstance, Mapping[str, Any]], DispatchTransport]
        ] = None,
    ) -> Dict[str, Any]:
        """停止：未派发立即取消；已提交只尽力取消并保留槽位到可证明结束。"""
        try:
            job = self._repo.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            self._repo.mark_job_cancel_requested(job_id)
            attempt = self._repo.latest_attempt(job_id)
            if attempt is None:
                self._safe_state(job_id, "cancelled", expected=("queued",))
                return self._repo.get_job(job_id) or {}
            state = str(attempt.get("submission_state") or "")
            if state == "prepared":
                slot = self._repo.slot_for_attempt(str(attempt["attempt_id"]))
                self._repo.mark_attempt_terminal(
                    attempt_id=str(attempt["attempt_id"]), reason="cancelled_before_submit"
                )
                if slot is not None:
                    self._repo.release(
                        resource_key=str(slot["resource_key"]),
                        attempt_id=str(attempt["attempt_id"]),
                        token=str(slot.get("token") or ""),
                    )
                self._safe_state(job_id, "cancelled", expected=("queued", "reserved", "submitting"))
            elif state == "submitted" and attempt.get("remote_job_id"):
                confirmed = False
                if transport_factory is not None:
                    runtime = self.runtime()
                    instance = runtime.instances.get(str(attempt.get("instance_id") or ""))
                    if instance is not None:
                        transport = transport_factory(instance, job)
                        try:
                            confirmed = bool(
                                transport.cancel(attempt=attempt, instance=instance, job=job)
                            )
                        except Exception:  # noqa: BLE001 - 取消未确认继续持槽
                            logger.warning(
                                "cancel unconfirmed for job %s; keeping slot", job_id, exc_info=True
                            )
                        finally:
                            _close_quietly(transport)
                if confirmed:
                    self._repo.mark_attempt_cancelled(
                        attempt_id=str(attempt["attempt_id"]),
                        reason="cancel_confirmed",
                        confirmed_at=int(self._clock() * 1000),
                    )
                # 取消确认不等于 GPU 已排空：保持 reconciling，槽位等到核对证明结束。
                self._safe_state(
                    job_id,
                    "reconciling",
                    expected=(
                        "reserved",
                        "submitting",
                        "unknown",
                        "remote_queued",
                        "running",
                        "downloading",
                    ),
                )
            else:
                # submitting/unknown 或已提交但无通道：保留槽位，等待核对。
                self._safe_state(
                    job_id,
                    "reconciling",
                    expected=(
                        "reserved",
                        "submitting",
                        "unknown",
                        "remote_queued",
                        "running",
                    ),
                )
            return self._repo.get_job(job_id) or {}
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def restore_runtime_state(self) -> Dict[str, Any]:
        with self._state_lock:
            try:
                self._stopped = bool(self._repo.is_dispatch_stopped())
                self._draining_profiles = set(self._repo.list_draining_profiles())
                self._draining_instances = set(self._repo.list_draining_instances())
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            drained = 0
            for profile_id in self._draining_profiles:
                drained += self.fail_profile_queued(profile_id)
            if self._draining_instances:
                drained += self._fail_queued_for_draining_instances()
            return {
                "dispatch_stopped": self._stopped,
                "draining_profiles": len(self._draining_profiles),
                "draining_instances": len(self._draining_instances),
                "failed_queued": drained,
            }

    def _fail_queued_for_draining_instances(self) -> int:
        queued_ids = []
        failed = 0
        for job in self._repo.list_jobs_by_states(
            ("queued", "reserved", "submitting")
        ):
            job_id = str(job["job_id"])
            snapshot = job.get("snapshot_json")
            if not isinstance(snapshot, dict):
                release_prepared = self._release_prepared_attempt(
                    job_id, reason="pool_configuration_error"
                )
                if release_prepared or str(job.get("state")) == "queued":
                    self._fail_job(
                        job_id,
                        "pool_configuration_error",
                        "任务快照损坏，无法判定资源池归属",
                        job=job,
                    )
                    failed += 1
                continue
            members = {str(item) for item in (snapshot.get("members") or [])}
            if not members or not members.issubset(self._draining_instances):
                continue
            if str(job.get("state")) == "queued":
                self._release_prepared_attempt(job_id, reason="pool_unavailable")
                queued_ids.append(job_id)
            elif self._release_prepared_attempt(
                job_id, reason="pool_unavailable"
            ):
                self._fail_job(
                    job_id,
                    "pool_unavailable",
                    "资源池实例正在排空",
                    job=job,
                )
                failed += 1
        failed += self._repo.fail_queued_jobs(
            job_ids=queued_ids,
            error_code="pool_unavailable",
            reason="资源池实例正在排空",
            payload=dispatch_error_payload(
                "pool_unavailable", "资源池实例正在排空"
            ),
        )
        return failed

    def stop_all(self, *, error_code: str = "dispatch_disabled") -> Dict[str, Any]:
        with self._state_lock:
            try:
                self._repo.mark_dispatch_stopped(True)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._stopped = True
            stopped = self.stop_undispatched(error_code=error_code)
            return {
                "stopped_queued": stopped,
                "stopped_jobs": stopped,
                "dispatch_enabled": False,
            }

    def resume_all(self) -> Dict[str, Any]:
        with self._state_lock:
            try:
                self._repo.mark_dispatch_stopped(False)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._stopped = False
            return {"dispatch_enabled": True}

    def drain_profile(self, profile_id: str) -> Dict[str, Any]:
        profile_id = str(profile_id or "")
        if not profile_id:
            raise ValueError("profile_id 不能为空")
        with self._state_lock:
            try:
                self._repo.mark_profile_draining(profile_id, True)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._draining_profiles.add(profile_id)
            stopped = self.fail_profile_queued(profile_id)
        return {
            "profile_id": profile_id,
            "draining": True,
            "stopped_queued": stopped,
        }

    def undrain_profile(self, profile_id: str) -> Dict[str, Any]:
        with self._state_lock:
            try:
                self._repo.mark_profile_draining(str(profile_id or ""), False)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._draining_profiles.discard(str(profile_id or ""))
        return {"profile_id": profile_id, "draining": False}

    def drain_instance(self, instance_id: str, profile_ids: Iterable[str]) -> Dict[str, Any]:
        instance_id = str(instance_id or "")
        if not instance_id:
            raise ValueError("instance_id 不能为空")
        with self._state_lock:
            try:
                self._repo.mark_instance_draining(instance_id, True)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._draining_instances.add(instance_id)
            stopped = 0
            for profile_id in profile_ids:
                stopped += self.fail_profile_queued(str(profile_id))
        return {
            "instance_id": instance_id,
            "draining": True,
            "stopped_queued": stopped,
        }

    def undrain_instance(self, instance_id: str) -> Dict[str, Any]:
        with self._state_lock:
            try:
                self._repo.mark_instance_draining(str(instance_id or ""), False)
            except DB_ERRORS as exc:
                raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc
            self._draining_instances.discard(str(instance_id or ""))
        return {"instance_id": instance_id, "draining": False}

    def stop_undispatched(self, *, error_code: str = "dispatch_disabled") -> int:
        """关闭全局调度：只结束可证明未发送的部分，在途/未知继续核对。"""
        try:
            queued = self._repo.list_queued()
            ids = [str(row["job_id"]) for row in queued]
            stopped = self._repo.fail_queued_jobs(
                job_ids=ids,
                error_code=error_code,
                reason="受管派发已停用",
                payload=dispatch_error_payload(error_code, "受管派发已停用"),
            )
            for row in queued:
                attempt = self._repo.latest_attempt(str(row["job_id"]))
                if attempt is None:
                    continue
                if str(attempt.get("submission_state")) != "prepared":
                    continue
                slot = self._repo.slot_for_attempt(str(attempt["attempt_id"]))
                if slot is None:
                    continue
                self._repo.release(
                    resource_key=str(slot["resource_key"]),
                    attempt_id=str(attempt["attempt_id"]),
                    token=str(slot.get("token") or ""),
                )
                self._repo.mark_attempt_terminal(
                    attempt_id=str(attempt["attempt_id"]), reason=error_code
                )
            # reserved/submitting 中尚未真正 POST 的 prepared attempt 也必须随停用收口；
            # submitted/unknown 仍可能已在远端运行，保持隔离/核对，不提前释放槽。
            for job in self._repo.list_jobs_by_states(("reserved", "submitting")):
                job_id = str(job["job_id"])
                attempt = self._repo.latest_attempt(job_id)
                if attempt is not None:
                    if str(attempt.get("submission_state")) != "prepared":
                        continue
                    slot = self._repo.slot_for_attempt(str(attempt["attempt_id"]))
                    if slot is not None:
                        self._repo.release(
                            resource_key=str(slot["resource_key"]),
                            attempt_id=str(attempt["attempt_id"]),
                            token=str(slot.get("token") or ""),
                        )
                    self._repo.mark_attempt_terminal(
                        attempt_id=str(attempt["attempt_id"]), reason=error_code
                    )
                self._fail_job(
                    job_id,
                    error_code,
                    "受管派发已停用",
                    job=job,
                )
                stopped += 1
            return stopped
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def _release_prepared_attempt(self, job_id: str, *, reason: str) -> bool:
        """原子撤销可证明尚未提交的 prepared attempt；在途/unknown 不在此处理。"""
        attempt = self._repo.latest_attempt(job_id)
        if attempt is None:
            return False
        return bool(
            self._repo.cancel_prepared_attempt(
                attempt_id=str(attempt["attempt_id"]), reason=reason
            )
        )

    def fail_profile_queued(self, profile_id: str) -> int:
        """管理员显式下线整池：提前结束未派发项，不碰在途。"""
        try:
            profile = str(profile_id)
            ids = []
            failed = 0
            for row in self._repo.list_jobs_by_states(
                ("queued", "reserved", "submitting")
            ):
                if str(row.get("profile_id") or "") != profile:
                    continue
                job_id = str(row["job_id"])
                if str(row.get("state")) == "queued":
                    self._release_prepared_attempt(job_id, reason="pool_unavailable")
                    ids.append(job_id)
                elif self._release_prepared_attempt(
                    job_id, reason="pool_unavailable"
                ):
                    self._fail_job(
                        job_id,
                        "pool_unavailable",
                        "资源池已由管理员下线",
                        job=row,
                    )
                    failed += 1
            failed += self._repo.fail_queued_jobs(
                job_ids=ids,
                error_code="pool_unavailable",
                reason="资源池已由管理员下线",
                payload=dispatch_error_payload("pool_unavailable", "资源池已由管理员下线"),
            )
            return failed
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    # ── 内部工具 ──

    def record_shared_observation(
        self, instance_id: str, sample: Optional[ObservationSample]
    ) -> None:
        """探针服务回填观测的入口；测试与周期探测共用同一账本。"""
        self._shared.add(instance_id, sample)


    def _job_from_attempt(self, attempt: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "job_id": attempt.get("job_id"),
            "owner": attempt.get("owner"),
            "batch_id": attempt.get("batch_id"),
            "execution_id": attempt.get("execution_id"),
            "profile_id": attempt.get("profile_id"),
            "dispatch_mode": attempt.get("dispatch_mode"),
            "source_kind": attempt.get("source_kind"),
            "source_id": attempt.get("source_id"),
            "snapshot_json": attempt.get("snapshot_json") or {},
            "state": attempt.get("job_state"),
        }

    def _fail_job(
        self,
        job_id: str,
        error_code: str,
        reason: str,
        *,
        job: Optional[Mapping[str, Any]] = None,
        attempt: Optional[Mapping[str, Any]] = None,
    ) -> None:
        source = job or attempt or {}
        self._repo.set_job_state(
            job_id=job_id,
            state="failed",
            error=dispatch_error_payload(
                error_code,
                reason,
                mode=str(source.get("dispatch_mode") or "auto"),
                profile_id=source.get("profile_id"),
                execution_id=source.get("execution_id"),
            ),
            expected_states=(
                "queued",
                "reserved",
                "submitting",
                "remote_queued",
                "running",
                "unknown",
                "reconciling",
            ),
        )

    @staticmethod
    def _discard_output(result: Any) -> None:
        if not isinstance(result, Mapping):
            return
        path = str(result.get("save_path") or "")
        if not path or not os.path.isfile(path):
            return
        try:
            os.remove(path)
        except OSError:
            logger.warning("failed to remove unpublished output: %s", path, exc_info=True)

    def _safe_state(
        self,
        job_id: str,
        state: str,
        *,
        expected: Optional[Iterable[str]] = None,
        error: Any = None,
        result: Any = None,
    ) -> bool:
        try:
            return self._repo.set_job_state(
                job_id=job_id, state=state, error=error, result=result, expected_states=expected
            )
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def _release(self, attempt: Mapping[str, Any]) -> bool:
        try:
            return self._repo.release(
                resource_key=str(attempt["resource_key"]),
                attempt_id=str(attempt["attempt_id"]),
                token=str(attempt.get("token") or attempt.get("slot_token") or ""),
            )
        except DB_ERRORS as exc:
            raise DispatchUnavailable(f"调度账本不可用：{exc}") from exc

    def _inject_crash(
        self,
        stage: str,
        job: Mapping[str, Any],
        attempt: Mapping[str, Any],
        *,
        outcome: Optional[SubmitOutcome] = None,
    ) -> None:
        if self._crash_hook is None:
            return
        payload = {"job_id": job.get("job_id"), "attempt_id": attempt.get("attempt_id")}
        if outcome is not None:
            payload["outcome"] = outcome.status
        self._crash_hook(stage, payload)

    @staticmethod
    def _pop_job(queue: Deque[Dict[str, Any]], job_id: str) -> None:
        for index, item in enumerate(queue):
            if str(item.get("job_id")) == str(job_id):
                del queue[index]
                return


def build_pool_runtime(
    data: Mapping[str, Any],
    *,
    revision: str,
    dispatch_enabled: bool,
    qualified: Iterable[str],
    occupied: Iterable[str],
    quarantined: Iterable[str],
    batch_inflight: Optional[Mapping[str, int]] = None,
    last_dispatch: Optional[Mapping[str, int]] = None,
    draining_profiles: Iterable[str] = (),
    draining_instances: Iterable[str] = (),
    blocked_models: Iterable[str] = (),
) -> PoolRuntime:
    """把运行时 YAML 与账本摘要折叠成派发可直接消费的只读快照。"""
    dispatch = data.get("video_dispatch") or {}
    profiles: Dict[str, RuntimeProfile] = {}
    for row in data.get("video_profiles") or []:
        if not isinstance(row, Mapping):
            continue
        profile_id = str(row.get("id") or "")
        if not profile_id:
            continue
        profiles[profile_id] = RuntimeProfile(
            profile_id=profile_id,
            revision=int(row.get("revision") or 1),
            enabled=bool(row.get("enabled", True)),
            members=tuple(str(item) for item in row.get("instances") or []),
            contracts=dict(row.get("contracts") or {}),
            max_concurrency_per_execution=_clamp_int(
                row.get("max_concurrency_per_execution"), default=8
            ),
        )
    instances: Dict[str, RuntimeInstance] = {}
    for row in data.get("video_instances") or []:
        if not isinstance(row, Mapping):
            continue
        instance_id = str(row.get("instance_id") or "")
        if not instance_id:
            continue
        instances[instance_id] = RuntimeInstance(
            instance_id=instance_id,
            model_id=str(row.get("model_id") or ""),
            resource_key=str(row.get("resource_key") or ""),
            access_mode=str(row.get("access_mode") or ""),
            enabled=bool(row.get("enabled")),
            driver=str(row.get("driver") or ""),
            probe=dict(row.get("probe") or {}),
        )
    return PoolRuntime(
        revision=str(revision),
        dispatch_enabled=bool(dispatch_enabled),
        global_limit=_clamp_int(dispatch.get("max_running_jobs"), default=8),
        profiles=profiles,
        instances=instances,
        qualified=frozenset(str(item) for item in qualified),
        occupied_keys=frozenset(str(item) for item in occupied),
        quarantined_keys=frozenset(str(item) for item in quarantined),
        batch_inflight=dict(batch_inflight or {}),
        last_dispatch=dict(last_dispatch or {}),
        default_profiles={
            str(mode): str(pool_id)
            for mode, pool_id in (dispatch.get("default_profiles") or {}).items()
        },
        queue_timeout_seconds=_positive_int(
            dispatch.get("queue_timeout_seconds"), default=3600, maximum=604800
        ),
        max_queued_jobs=_positive_int(
            dispatch.get("max_queued_jobs"), default=1000, maximum=100000
        ),
        draining_profiles=frozenset(str(item) for item in draining_profiles),
        draining_instances=frozenset(str(item) for item in draining_instances),
        blocked_models=frozenset(str(item) for item in blocked_models),
    )


def _positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int) -> int:
    if isinstance(value, bool) or value is None:
        return max(minimum, min(maximum, int(default)))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _clamp_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 8) -> int:
    if isinstance(value, bool) or value is None:
        return max(minimum, min(maximum, int(default)))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _download_backoff(index: int) -> float:
    return min(5.0, 0.5 * (2 ** index))


def _close_quietly(transport: Any) -> None:
    close = getattr(transport, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - 关闭失败不影响账本
        logger.debug("dispatch transport close failed", exc_info=True)


__all__ = [
    "DB_ERRORS",
    "DispatchCrashInjected",
    "DispatchReport",
    "DispatchService",
    "DispatchTransport",
    "DispatchUnavailable",
    "JOB_TERMINAL_STATES",
    "PoolRuntime",
    "PoolResolution",
    "PollOutcome",
    "RuntimeInstance",
    "RuntimeProfile",
    "SharedObservationLedger",
    "SubmitOutcome",
    "build_pool_runtime",
]
