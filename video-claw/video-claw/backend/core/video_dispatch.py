"""单进程视频调度队列：owner→session 轮转与池容量授予（纯内存、可虚拟时钟）。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import threading
from typing import Callable, Deque, Dict, Iterable, List, Optional, Set

DEFAULT_GLOBAL_LIMIT = 8
DEFAULT_PER_EXECUTION_LIMIT = 8


@dataclass
class DispatchJob:
    job_id: str
    owner: str
    session_id: str
    execution_id: str
    profile_id: str
    mode: str
    accepted_at: float
    queue_deadline_at: float
    status: str = "queued"


@dataclass(frozen=True)
class Grant:
    job_id: str
    owner: str
    session_id: str
    execution_id: str
    profile_id: str
    mode: str


@dataclass
class CapacitySnapshot:
    """调度器只消费快照；active_global 必须包含 unknown/隔离占用。"""

    profile_slots: Dict[str, int] = field(default_factory=dict)
    profile_modes: Dict[str, Set[str]] = field(default_factory=dict)
    per_execution_limits: Dict[str, int] = field(default_factory=dict)
    execution_active: Dict[str, int] = field(default_factory=dict)
    global_limit: int = DEFAULT_GLOBAL_LIMIT
    active_global: int = 0

    def free_for(self, job: DispatchJob) -> int:
        if job.profile_id not in self.profile_modes:
            return 0
        if job.mode not in self.profile_modes[job.profile_id]:
            return 0
        slots = max(0, int(self.profile_slots.get(job.profile_id, 0)))
        global_left = max(0, int(self.global_limit) - int(self.active_global))
        limit = int(
            self.per_execution_limits.get(
                job.execution_id, DEFAULT_PER_EXECUTION_LIMIT
            )
        )
        execution_left = max(0, limit - int(self.execution_active.get(job.execution_id, 0)))
        return min(slots, global_left, execution_left)

    def consume(self, job: DispatchJob) -> None:
        self.profile_slots[job.profile_id] = max(
            0, int(self.profile_slots.get(job.profile_id, 0)) - 1
        )
        self.active_global = int(self.active_global) + 1
        self.execution_active[job.execution_id] = (
            int(self.execution_active.get(job.execution_id, 0)) + 1
        )


class VideoDispatchScheduler:
    """按 owner 轮转，单 owner 每轮最多一单；session 内队头不兼容则跳过。"""

    def __init__(self) -> None:
        self._jobs: Dict[str, DispatchJob] = {}
        self._owners: Dict[str, Deque[str]] = {}
        self._owner_order: Deque[str] = deque()
        self._sessions: Dict[str, Deque[str]] = {}
        # 入队/释放/授予都会唤醒等待者；调度 loop 不忙等。
        self._condition = threading.Condition()

    def notify(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def wait_for_change(
        self,
        timeout: float = 1.0,
        predicate: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """等待队列变化；predicate 为真才返回，避免丢通知/虚假唤醒。"""
        with self._condition:
            if predicate is None:
                return self._condition.wait(timeout)
            return self._condition.wait_for(predicate, timeout)

    def enqueue(self, job: DispatchJob) -> None:
        with self._condition:
            if job.job_id in self._jobs:
                return
            self._jobs[job.job_id] = job
            if job.owner not in self._owners:
                self._owners[job.owner] = deque()
                self._sessions[job.owner] = deque()
                self._owner_order.append(job.owner)
            self._owners[job.owner].append(job.job_id)
            sessions = self._sessions.setdefault(job.owner, deque())
            if job.session_id not in sessions:
                sessions.append(job.session_id)
            self._condition.notify_all()

    def remove(self, job_id: str) -> Optional[DispatchJob]:
        with self._condition:
            job = self._jobs.pop(job_id, None)
            if job is None:
                return None
            queue = self._owners.get(job.owner)
            if queue is not None:
                try:
                    queue.remove(job_id)
                except ValueError:
                    pass
            self._condition.notify_all()
            return job

    def pending(self) -> int:
        with self._condition:
            return len(self._jobs)

    def grant(self, capacity: CapacitySnapshot) -> List[Grant]:
        with self._condition:
            grants: List[Grant] = []
            misses = 0
            while self._owner_order and capacity.active_global < capacity.global_limit:
                owner = self._owner_order[0]
                job = self._next_compatible(owner, capacity)
                self._owner_order.rotate(-1)
                if job is None:
                    misses += 1
                    if misses >= len(self._owner_order):
                        break
                    continue
                misses = 0
                capacity.consume(job)
                self._jobs.pop(job.job_id, None)
                queue = self._owners.get(owner)
                if queue is not None:
                    try:
                        queue.remove(job.job_id)
                    except ValueError:
                        pass
                    if not queue:
                        self._owners.pop(owner, None)
                        self._sessions.pop(owner, None)
                        try:
                            self._owner_order.remove(owner)
                        except ValueError:
                            pass
                grants.append(
                    Grant(
                        job_id=job.job_id,
                        owner=job.owner,
                        session_id=job.session_id,
                        execution_id=job.execution_id,
                        profile_id=job.profile_id,
                        mode=job.mode,
                    )
                )
            if grants:
                self._condition.notify_all()
            return grants

    def _next_compatible(
        self,
        owner: str,
        capacity: CapacitySnapshot,
    ) -> Optional[DispatchJob]:
        queue = self._owners.get(owner)
        sessions = self._sessions.get(owner)
        if not queue or not sessions:
            return None
        # session 轮转：每轮先看最早的 session；选中后移到队尾。
        for _ in range(len(sessions)):
            session_id = sessions[0]
            for job_id in list(queue):
                job = self._jobs.get(job_id)
                if job is None or job.session_id != session_id:
                    continue
                if capacity.free_for(job) <= 0:
                    continue
                sessions.rotate(-1)  # 选中 session 移到队尾
                return job
            # 该 session 当前没有兼容 job，移到队尾后继续看下一个。
            sessions.rotate(-1)
        return None

    def queued_for_owner(self, owner: str) -> List[DispatchJob]:
        with self._condition:
            queue = self._owners.get(owner) or ()
            return [self._jobs[job_id] for job_id in queue if job_id in self._jobs]

    def jobs(self) -> Iterable[DispatchJob]:
        with self._condition:
            return tuple(self._jobs.values())


__all__ = [
    "CapacitySnapshot",
    "DEFAULT_GLOBAL_LIMIT",
    "DEFAULT_PER_EXECUTION_LIMIT",
    "DispatchJob",
    "Grant",
    "VideoDispatchScheduler",
]
