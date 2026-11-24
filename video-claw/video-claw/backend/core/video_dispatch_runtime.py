"""单进程调度运行时：后台 loop + 批量生成服务入口（由 lifespan 启动）。"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping, Optional

from core.video_dispatch import VideoDispatchScheduler
from core.video_dispatch_service import DispatchService
from core.video_generation_service import VideoGenerationService

logger = logging.getLogger(__name__)

_RUNTIME: Optional["DispatchRuntime"] = None


class DispatchRuntime:
    def __init__(
        self,
        service: DispatchService,
        transport_factory: Callable[[Any, Mapping[str, Any]], Any],
        *,
        interval: float = 0.5,
        on_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        self.service = service
        self.transport_factory = transport_factory
        self.generation = VideoGenerationService(service)
        self.interval = max(0.02, float(interval))
        self._on_stop = on_stop
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.scheduler = VideoDispatchScheduler()
        setter = getattr(service, "set_waker", None)
        if setter is not None:
            setter(self.scheduler.notify)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="video-dispatch-runtime", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.scheduler.notify()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None
        if self._on_stop is not None:
            try:
                self._on_stop()
            except Exception:  # noqa: BLE001 - 关闭清理不能阻塞退出
                logger.warning("dispatch runtime on_stop failed", exc_info=True)
        if get_dispatch_runtime() is self:
            set_dispatch_runtime(None)

    def tick(self) -> None:
        try:
            self.service.dispatch_once(self.transport_factory)
        except Exception:  # noqa: BLE001 - loop 不能因单轮异常退出
            logger.warning("dispatch tick failed", exc_info=True)
        try:
            self.service.reconcile_once(self.transport_factory)
        except Exception:  # noqa: BLE001 - 核对失败下一轮继续
            logger.warning("dispatch reconcile failed", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            pending = self.service.has_pending_work()
            # 无队列/无在途时不做池加载与 reconcile，避免空转争用 DB。
            if pending:
                self.tick()
            if pending:
                # 有心跳/在途时需要定时复核；条件唤醒只用于入队。
                self.scheduler.wait_for_change(timeout=self.interval)
            else:
                self.scheduler.wait_for_change(
                    timeout=self.interval,
                    predicate=lambda: self._stop.is_set()
                    or self.service.has_pending_work(),
                )


def set_dispatch_runtime(runtime: Optional[DispatchRuntime]) -> None:
    global _RUNTIME
    _RUNTIME = runtime


def get_dispatch_runtime() -> Optional[DispatchRuntime]:
    return _RUNTIME


__all__ = [
    "DispatchRuntime",
    "get_dispatch_runtime",
    "set_dispatch_runtime",
]
