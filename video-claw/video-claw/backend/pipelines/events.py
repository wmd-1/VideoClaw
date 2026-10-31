import asyncio
import json
import threading
from collections import defaultdict
from typing import Any, AsyncIterator, Dict, Set, Tuple

# 订阅者 = (所属事件循环, 队列)：事件可能在线程池里提交后发出，必须回到订阅者的循环入队
_subscribers: Dict[str, Set[Tuple[asyncio.AbstractEventLoop, "asyncio.Queue[dict[str, Any]]"]]] = defaultdict(set)
_lock = threading.Lock()


def _put(queue: "asyncio.Queue[dict[str, Any]]", payload: dict[str, Any]) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(payload)


def publish_task_event(task_id: str, event: dict[str, Any]) -> None:
    with _lock:
        subscribers = list(_subscribers.get(task_id, set()))
    if not subscribers:
        return
    payload = {"task_id": task_id, **event}
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    for loop, queue in subscribers:
        if loop is current:
            _put(queue, payload)
        elif not loop.is_closed():
            loop.call_soon_threadsafe(_put, queue, payload)


async def task_event_stream(task_id: str, initial_event: dict[str, Any] | None = None) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    entry = (asyncio.get_running_loop(), queue)
    with _lock:
        _subscribers[task_id].add(entry)
    try:
        if initial_event:
            yield _format_sse(initial_event)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
                yield _format_sse(event)
                if event.get("type") in {"completed", "failed"}:
                    return
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        with _lock:
            _subscribers[task_id].discard(entry)
            if not _subscribers[task_id]:
                _subscribers.pop(task_id, None)


def _format_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
