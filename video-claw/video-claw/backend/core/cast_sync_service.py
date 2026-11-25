# -*- coding: utf-8 -*-
"""剧本正文变化后的人物/场景重解析调度器。"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

from core.agents.script_agent import ScriptWriterAgent
from db.repositories.commits import CommitConflict

logger = logging.getLogger(__name__)


@dataclass
class _Job:
    session_id: str
    token: threading.Event = field(default_factory=threading.Event)
    task: Optional[asyncio.Task] = None


class CastSyncService:
    """双 worker、每会话单飞、1.5 秒 settle；所有 DB 读写离开事件循环。"""

    def __init__(
        self,
        engine: Any,
        *,
        agent_factory: Callable[[], Any] = ScriptWriterAgent,
        concurrency: int = 2,
        settle_seconds: float = 1.5,
        supervisor_interval: float = 1.0,
    ) -> None:
        self.engine = engine
        self.agent_factory = agent_factory
        self.concurrency = max(1, min(2, int(concurrency)))
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.supervisor_interval = max(0.1, float(supervisor_interval))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._workers: Set[asyncio.Task] = set()
        self._supervisor: Optional[asyncio.Task] = None
        self._active: Dict[str, _Job] = {}
        self._queued: Set[str] = set()
        self._dirty: Set[str] = set()
        self._ready: Set[str] = set()
        self._cancelled: Set[str] = set()
        self._settle_tasks: Dict[str, asyncio.Task] = {}
        self._pending_notifications: Set[str] = set()
        self._registry_lock = threading.RLock()
        self._stopping = False
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._stopping = False
        self._started = True
        with self._registry_lock:
            self._cancelled.clear()
            self._dirty.clear()
            self._ready.clear()
        self._spawn_worker()
        self._spawn_worker()
        self._supervisor = asyncio.create_task(self._supervise(), name="cast-sync-supervisor")
        with self._registry_lock:
            pending = list(self._pending_notifications)
            self._pending_notifications.clear()
        for session_id in pending:
            self._schedule(session_id)
        logger.info(
            "Cast sync started: workers=%d concurrency=%d settle=%.3fs",
            len(self._workers), self.concurrency, self.settle_seconds,
        )

    def notify(self, session_id: str) -> None:
        """线程安全唤醒；正文与 pending 已由调用方先提交。"""
        loop = self._loop
        if not self._started or loop is None or loop.is_closed():
            with self._registry_lock:
                self._pending_notifications.add(session_id)
            return
        try:
            loop.call_soon_threadsafe(self._schedule, session_id)
        except RuntimeError:
            with self._registry_lock:
                self._pending_notifications.add(session_id)

    def has_active_or_pending(self, session_id: str) -> bool:
        with self._registry_lock:
            if session_id in self._cancelled:
                return False
            return bool(
                session_id in self._active
                or session_id in self._queued
                or session_id in self._settle_tasks
                or session_id in self._dirty
                or session_id in self._ready
                or session_id in self._pending_notifications
            )

    def stop_session(self, session_id: str) -> None:
        """停止当前会话任务；DB 状态由引擎先写 stopped。"""
        loop = self._loop
        if not self._started or loop is None or loop.is_closed():
            with self._registry_lock:
                self._cancelled.add(session_id)
                self._pending_notifications.discard(session_id)
            return
        with self._registry_lock:
            self._cancelled.add(session_id)
            job = self._active.get(session_id)
            if job is not None:
                job.token.set()
        try:
            loop.call_soon_threadsafe(self._stop_session_on_loop, session_id)
        except RuntimeError:
            pass

    def _stop_session_on_loop(self, session_id: str) -> None:
        with self._registry_lock:
            self._cancelled.add(session_id)
            self._queued.discard(session_id)
            self._dirty.discard(session_id)
            self._ready.discard(session_id)
            settle = self._settle_tasks.pop(session_id, None)
            job = self._active.get(session_id)
        if settle is not None and not settle.done():
            settle.cancel()
        if job is not None:
            job.token.set()
            if job.task is not None and not job.task.done():
                job.task.cancel()

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        with self._registry_lock:
            settle_tasks = list(self._settle_tasks.values())
            self._settle_tasks.clear()
            jobs = list(self._active.values())
            workers = list(self._workers)
            supervisor = self._supervisor
        for task in settle_tasks:
            task.cancel()
        for job in jobs:
            job.token.set()
            if job.task is not None and not job.task.done():
                job.task.cancel()
        for task in workers:
            task.cancel()
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
        tasks = [*settle_tasks, *workers, *([supervisor] if supervisor is not None else [])]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._supervisor = None
        self._active.clear()
        self._queued.clear()
        self._dirty.clear()
        self._ready.clear()
        self._cancelled.clear()
        self._pending_notifications.clear()
        self._started = False
        self._loop = None
        logger.info("Cast sync stopped")

    def _spawn_worker(self) -> None:
        if self._stopping or self._loop is None:
            return
        task = self._loop.create_task(self._worker(), name="cast-sync-worker")
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)

    async def _supervise(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.supervisor_interval)
            with self._registry_lock:
                alive = sum(1 for task in self._workers if not task.done())
            for _ in range(2 - alive):
                self._spawn_worker()

    def _schedule(self, session_id: str) -> None:
        if self._stopping:
            return
        with self._registry_lock:
            self._cancelled.discard(session_id)
            # 已排队任务会在真正执行时读取最新 pending；无需再开 settle 造成重复唤醒。
            if session_id in self._queued and session_id not in self._active:
                return
            previous = self._settle_tasks.pop(session_id, None)
            if session_id in self._active:
                self._dirty.add(session_id)
                # 新通知重置 settle 窗口，旧的 ready 不得让当前任务提前重排。
                self._ready.discard(session_id)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._settle_and_enqueue(session_id), name=f"cast-sync-settle:{session_id}"
        )
        with self._registry_lock:
            self._settle_tasks[session_id] = task

    async def _settle_and_enqueue(self, session_id: str) -> None:
        try:
            if self.settle_seconds:
                await asyncio.sleep(self.settle_seconds)
            with self._registry_lock:
                if self._settle_tasks.get(session_id) is asyncio.current_task():
                    self._settle_tasks.pop(session_id, None)
                if session_id in self._cancelled:
                    return
                if session_id in self._active:
                    self._dirty.add(session_id)
                    self._ready.add(session_id)
                    return
            self._enqueue(session_id)
        except asyncio.CancelledError:
            raise

    def _enqueue(self, session_id: str) -> None:
        if self._queue is None:
            return
        with self._registry_lock:
            if session_id in self._cancelled or session_id in self._active or session_id in self._queued:
                if session_id in self._active:
                    self._dirty.add(session_id)
                return
            self._queued.add(session_id)
        self._queue.put_nowait(session_id)

    async def _worker(self) -> None:
        while not self._stopping:
            try:
                session_id = await self._queue.get()
            except asyncio.CancelledError:
                raise
            try:
                with self._registry_lock:
                    self._queued.discard(session_id)
                    if session_id in self._cancelled:
                        continue
                    if session_id in self._active:
                        self._dirty.add(session_id)
                        continue
                await self._run_session(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单 job 异常不能杀 worker
                logger.exception("cast sync worker job failed: session=%s", session_id)
            finally:
                self._queue.task_done()

    async def _run_session(self, session_id: str) -> None:
        async with self._semaphore:
            state = await asyncio.to_thread(self.engine.get_state, session_id)
            if state is None:
                return
            record = self.engine._cast_reparse_record(state)
            if record.get("status") == "superseded":
                promoted = await asyncio.to_thread(
                    self.engine.promote_cast_reparse,
                    session_id,
                    int(record.get("attempt") or 0),
                    str(record.get("base_version") or ""),
                )
                if not promoted:
                    return
                state = await asyncio.to_thread(self.engine.get_state, session_id)
                if state is None:
                    return
                record = self.engine._cast_reparse_record(state)
            if record.get("status") != "pending":
                return
            job_id = uuid.uuid4().hex
            job = _Job(session_id=session_id)
            job.task = asyncio.current_task()
            with self._registry_lock:
                if session_id in self._active:
                    self._dirty.add(session_id)
                    return
                self._active[session_id] = job
            try:
                payload = await asyncio.to_thread(
                    self.engine.begin_cast_reparse,
                    session_id,
                    job_id,
                    int(record.get("attempt") or 0),
                    str(record.get("base_version") or ""),
                )
                if payload is None:
                    latest = await asyncio.to_thread(self.engine.get_state, session_id)
                    if latest is not None and self.engine._cast_reparse_record(latest).get("status") in (
                        "pending", "superseded"
                    ):
                        self._schedule(session_id)
                    return
                if job.token.is_set():
                    return
                if not payload.get("llm_model"):
                    raise ValueError("缺少人物/场景重解析所需的大模型配置")
                agent = self.agent_factory()
                candidates = await agent.reparse_cast(
                    payload["script_artifact"],
                    payload["changed_episodes"],
                    payload["llm_model"],
                    job.token.is_set,
                )
                if job.token.is_set():
                    return
                outcome = await asyncio.to_thread(
                    self.engine.complete_cast_reparse,
                    session_id,
                    payload["job_id"],
                    payload["attempt"],
                    payload["base_version"],
                    candidates,
                )
                if outcome == "superseded":
                    self._schedule(session_id)
            except asyncio.CancelledError:
                job.token.set()
                raise
            except CommitConflict as exc:
                await self._recover_commit_conflict(session_id, job_id, record, exc)
            except Exception as exc:  # noqa: BLE001 - 失败写回后继续服务其它会话
                if not job.token.is_set():
                    try:
                        failure = await asyncio.to_thread(
                            self.engine.fail_cast_reparse,
                            session_id,
                            job_id,
                            int(record.get("attempt") or 0),
                            str(record.get("base_version") or ""),
                            str(exc),
                        )
                    except CommitConflict as conflict:
                        await self._recover_commit_conflict(
                            session_id, job_id, record, conflict
                        )
                        failure = None
                    if failure in ("superseded", "stale"):
                        self._schedule(session_id)
                logger.warning("cast reparse failed: session=%s error=%s", session_id, exc)
            finally:
                with self._registry_lock:
                    self._active.pop(session_id, None)
                    ready = session_id in self._ready
                    if ready:
                        self._ready.discard(session_id)
                        self._dirty.discard(session_id)
                if ready and not job.token.is_set():
                    self._enqueue(session_id)

    async def _recover_commit_conflict(
        self, session_id: str, job_id: str, record: Dict[str, Any], exc: CommitConflict
    ) -> None:
        try:
            recovered = await asyncio.to_thread(
                self.engine.recover_cast_reparse_conflict,
                session_id, job_id,
                int(record.get("attempt") or 0),
                str(record.get("base_version") or ""),
            )
        except KeyError:
            recovered = False
        if recovered:
            self._schedule(session_id)
        logger.warning("cast reparse commit conflict, rescheduled=%s: %s", recovered, exc)


__all__ = ["CastSyncService"]
