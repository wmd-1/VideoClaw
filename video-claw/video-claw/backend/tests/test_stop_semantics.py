"""停止语义：覆盖全部运行态阶段、幂等、读模型清理、执行级令牌与抢占。

覆盖 openspec change `stage-stop-and-rewrite-resilience` 的 workflow-stage-stop 需求。
全部在既有 Docker 容器内对 videoclaw_test 库执行（fixtures 在 conftest）。
"""
from __future__ import annotations

import pytest

from api.dependencies import workflow_engine
from core.orchestrator import WorkflowStage, _StageRun


SID = "stop_semantics_1"
OWNER = "alice"


def _new_session(sid: str = SID) -> None:
    workflow_engine.create_session(sid, {"idea": "x"}, owner=OWNER)


def _set_status(sid: str, **status: str) -> None:
    def apply(after):
        for key, value in status.items():
            after.status[key] = value

    workflow_engine._mutate(sid, apply)


def _make_run(sid: str, stage: WorkflowStage = WorkflowStage.PROMPT_REWRITE,
              execution_id: str = "exec-1", background: bool = False) -> _StageRun:
    return _StageRun(
        session_id=sid, stage=stage, execution_id=execution_id,
        background=background, intervention=None,
    )


def test_stop_covers_every_running_stage():
    _new_session()
    _set_status(SID, reference_generation="running", prompt_rewrite="running")
    workflow_engine._mutate(SID, lambda after: setattr(after, "current_stage", WorkflowStage.REFERENCE_GENERATION))

    workflow_engine.stop_session(SID)

    state = workflow_engine.get_state(SID)
    assert state.status["reference_generation"] == "stopped"
    assert state.status["prompt_rewrite"] == "stopped"
    assert state.stage_progress["prompt_rewrite"]["message"] == "已停止"


def test_repeated_stop_is_idempotent():
    _new_session()
    _set_status(SID, prompt_rewrite="running")
    workflow_engine.stop_session(SID)
    first = workflow_engine.get_state(SID).revision

    workflow_engine.stop_session(SID)
    workflow_engine.stop_session(SID)

    assert workflow_engine.get_state(SID).revision == first


def test_stop_clears_running_overlay_entries():
    _new_session()
    _set_status(SID, prompt_rewrite="running")
    overlay = workflow_engine._overlay(SID, "prompt_rewrite")
    overlay["progress"] = {"message": "分镜 seg_01 改写中", "percent": 40}
    overlay["items"][("items", "seg_01")] = {"type": "items", "id": "seg_01", "status": "running"}

    workflow_engine.stop_session(SID)

    view = workflow_engine._view_dict(workflow_engine.get_state(SID), annotate=False)
    items = (view["artifacts"].get("prompt_rewrite") or {}).get("items") or []
    assert all(item.get("status") != "running" for item in items)
    assert view["stage_progress"]["prompt_rewrite"]["message"] == "已停止"


def test_stop_sets_execution_level_token_and_survives_request_reset():
    _new_session()
    run = _make_run(SID, execution_id="exec-live")
    workflow_engine._register_run(run)
    try:
        workflow_engine.stop_session(SID)
        assert run.stop_event.is_set()
        # 新请求会复位会话级事件（允许重做），但执行级令牌必须保持置位
        workflow_engine.reset_stop_event(SID)
        assert not workflow_engine.get_stop_event(SID).is_set()
        assert run.stop_event.is_set()
    finally:
        workflow_engine._unregister_run(run)


def test_begin_stage_preempts_stopped_run_without_releasing_new_guard():
    _new_session()
    old = _make_run(SID, execution_id="exec-old")
    workflow_engine._register_run(old)
    workflow_engine._active_sessions.add(SID)
    old.active_registered = True
    old.stop_event.set()

    new = _make_run(SID, execution_id="exec-new")
    workflow_engine._register_run(new)
    try:
        workflow_engine._begin_stage(new)
        assert old.preempted is True
        assert new.active_registered is True

        # 旧执行 finally 的释放按归属跳过：新执行仍持有闸门
        if old.active_registered and not old.preempted:
            workflow_engine._active_sessions.discard(SID)
        assert SID in workflow_engine._active_sessions

        # 未停止的第三执行仍被拒绝（不出现并行链）
        third = _make_run(SID, execution_id="exec-third")
        with pytest.raises(RuntimeError):
            workflow_engine._begin_stage(third)
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(old)
        workflow_engine._unregister_run(new)


def test_rejected_start_does_not_touch_status_or_errors():
    _new_session()
    _set_status(SID, prompt_rewrite="running")
    rejected = _make_run(SID, execution_id="exec-rejected")
    assert rejected.active_registered is False

    workflow_engine._fail_stage(rejected, f"Session {SID} is already running a stage.")

    state = workflow_engine.get_state(SID)
    assert state.status["prompt_rewrite"] == "running"
    assert workflow_engine._errors.get(SID) in (None, "")


def test_finish_after_stop_keeps_stopped_and_marks_execution_interrupted():
    _new_session()
    run = _make_run(SID, execution_id="exec-finish")
    workflow_engine._register_run(run)
    try:
        workflow_engine._begin_stage(run)
        workflow_engine.stop_session(SID)

        state = workflow_engine._finish_stage(
            run, {"requires_intervention": True, "stage_completed": True},
            {"items": [{"id": "seg_01", "status": "done"}]}, None,
        )

        assert state.status["prompt_rewrite"] == "stopped"
        assert state.stage_progress["prompt_rewrite"]["message"] == "已停止"
        assert state.executions["prompt_rewrite"]["status"] == "interrupted"
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(run)


def test_finish_after_post_stop_write_does_not_restore_stopped():
    _new_session()
    run = _make_run(SID, execution_id="exec-normalized")
    workflow_engine._register_run(run)
    try:
        workflow_engine._begin_stage(run)
        workflow_engine.stop_session(SID)
        # 停止后用户写入产物 → 既有重算把它归一为 completed（“停止不是粘滞状态”）
        workflow_engine.update_artifact(
            SID, "prompt_rewrite", {"items": [{"id": "seg_01", "status": "done"}]},
        )
        assert workflow_engine.get_state(SID).status["prompt_rewrite"] == "completed"

        state = workflow_engine._finish_stage(
            run, {"requires_intervention": True, "stage_completed": True},
            {"items": [{"id": "seg_01", "status": "done"}]}, None,
        )

        assert state.status["prompt_rewrite"] != "stopped"
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(run)


def test_progress_and_running_events_are_gated_by_execution_token():
    _new_session()
    live = _make_run(SID, execution_id="exec-live-progress")
    received = []
    workflow_engine._on_progress(
        live, lambda *args: received.append(args), "提示词改写", "分镜 seg_01 完成", 50, None,
    )
    assert received, "未停止时进度事件应正常转发"
    assert workflow_engine._overlay(SID, "prompt_rewrite")["progress"] is not None

    stopped = _make_run(SID, execution_id="exec-stopped-progress")
    stopped.stop_event.set()
    received.clear()
    workflow_engine._on_progress(
        stopped, lambda *args: received.append(args), "提示词改写", "分镜 seg_02 完成", 60, None,
    )
    assert received == [], "停止后不得再转发运行中进度"
    assert workflow_engine._overlay(SID, "prompt_rewrite")["progress"]["percent"] == 50


def test_fail_after_post_stop_write_keeps_normalized_status():
    _new_session()
    run = _make_run(SID, execution_id="exec-fail-normalized")
    workflow_engine._register_run(run)
    try:
        workflow_engine._begin_stage(run)
        workflow_engine.stop_session(SID)
        workflow_engine.update_artifact(
            SID, "prompt_rewrite", {"items": [{"id": "seg_01", "status": "done"}]},
        )
        assert workflow_engine.get_state(SID).status["prompt_rewrite"] == "completed"

        workflow_engine._fail_stage(run, "boom")

        assert workflow_engine.get_state(SID).status["prompt_rewrite"] == "completed"
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(run)


def test_finish_discards_when_execution_is_superseded():
    _new_session()
    old = _make_run(SID, execution_id="exec-old-finish")
    workflow_engine._register_run(old)
    workflow_engine._begin_stage(old)
    old.stop_event.set()  # 用户已停止 → 允许新执行抢占
    new = _make_run(SID, execution_id="exec-new-finish")
    workflow_engine._register_run(new)
    try:
        workflow_engine._begin_stage(new)
        assert old.preempted is True
        before_status = workflow_engine.get_state(SID).status["prompt_rewrite"]

        state = workflow_engine._finish_stage(
            old, {"requires_intervention": True, "stage_completed": True},
            {"items": [{"id": "seg_01", "status": "failed"}]}, None,
        )

        assert state is not None
        assert state.status["prompt_rewrite"] == before_status, "迟到收尾不得覆盖新执行状态"
        assert old.discarded is True
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(old)
        workflow_engine._unregister_run(new)


def test_preview_events_are_gated_after_stop():
    _new_session()
    run = _make_run(SID, execution_id="exec-preview")
    run.stop_event.set()
    received = []
    workflow_engine._on_progress(
        run, lambda *args: received.append(args), "分镜设计", "逐集生成中", 30,
        {"assets_preview": {"episodes": [{"episode_number": 1, "segments": []}]}, "persist": True},
    )
    assert received == [], "停止后不得转发预览事件"
    assert workflow_engine._overlay(SID, "storyboard")["preview"] is None, "停止后不得重建预览展示层"
    assert workflow_engine.get_state(SID).artifacts.get("storyboard") is None, "停止后不得写工作层"


def test_fail_stage_writes_stopped_when_stage_still_running():
    """停止窗口内新执行被取消：阶段仍是 running 时必须写 stopped，不能留在运行中。"""
    _new_session()
    run = _make_run(SID, execution_id="exec-fail-running")
    workflow_engine._register_run(run)
    try:
        workflow_engine._begin_stage(run)
        assert workflow_engine.get_state(SID).status["prompt_rewrite"] == "running"
        workflow_engine.get_stop_event(SID).set()

        workflow_engine._fail_stage(run, "cancelled")

        assert workflow_engine.get_state(SID).status["prompt_rewrite"] == "stopped"
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(run)


def test_stream_task_suppresses_terminal_event_for_discarded_run():
    """被抢占的旧执行：事件流层不推 stage_complete，也不推 error（只关流）。"""
    import asyncio
    import queue
    import types

    from api.services.project_helpers import stream_workflow_task

    class FakeRequest:
        async def is_disconnected(self):
            return False

    class FakeEngine:
        async def execute_stage(self, *args, **kwargs):
            return {"discarded": True, "payload": None, "requires_intervention": False}

        def persist_session_snapshot(self, session_id):  # pragma: no cover - 不应被调用
            raise AssertionError("被丢弃的执行不应读取状态快照")

    class FastTrigger:
        def clear(self):
            pass

        async def wait(self):
            # 必须显式让出事件循环：3.12 的 wait_for 会内联 await 协程，
            # 若本方法没有 await 点，上层 while not task.done() 会变成忙等死循环。
            await asyncio.sleep(0)
            return True

    chunks = []

    async def collect():
        generator = stream_workflow_task(
            request=FakeRequest(),
            workflow_engine=FakeEngine(),
            state=types.SimpleNamespace(session_id="probe_discarded"),
            stage="prompt_rewrite",
            input_data={},
            cancellation_check=lambda: False,
            progress_callback=lambda *a, **k: None,
            progress_events=queue.Queue(),
            event_trigger=FastTrigger(),
        )
        async for chunk in generator:
            chunks.append(chunk)

    asyncio.run(collect())
    assert chunks == [], f"被丢弃的执行不得推送任何事件，实际：{chunks}"


def test_stream_task_forwards_cast_rollback_summary():
    import asyncio
    import json
    import queue
    import types

    from api.services.project_helpers import stream_workflow_task

    class FakeRequest:
        async def is_disconnected(self):
            return False

    class FakeEngine:
        async def execute_stage(self, *args, **kwargs):
            return {
                "payload": {},
                "requires_intervention": False,
                "cast_restored": False,
                "cast_diff": {"characters": {"added": [], "removed": [], "updated": []}},
                "cast_legacy": False,
            }

        def persist_session_snapshot(self, session_id):
            return {"script_generation": "completed"}

    class FastTrigger:
        def clear(self):
            pass

        async def wait(self):
            await asyncio.sleep(0)
            return True

    chunks = []

    async def collect():
        generator = stream_workflow_task(
            request=FakeRequest(),
            workflow_engine=FakeEngine(),
            state=types.SimpleNamespace(session_id="probe_cast_rollback"),
            stage="script_generation",
            input_data={},
            cancellation_check=lambda: False,
            progress_callback=lambda *a, **k: None,
            progress_events=queue.Queue(),
            event_trigger=FastTrigger(),
        )
        async for chunk in generator:
            chunks.append(chunk)

    asyncio.run(collect())
    event = json.loads(chunks[-1])
    assert event["type"] == "stage_complete"
    assert event["cast_restored"] is False
    assert event["cast_legacy"] is False
    assert event["cast_diff"]["characters"]["updated"] == []


def test_rejected_start_does_not_leak_active_run(monkeypatch):
    """执行权登记失败时不得残留活跃 run：否则后续每次重试都会被拒，直到点停止或重启。"""
    import asyncio

    _new_session()
    stage = WorkflowStage.PROMPT_REWRITE

    class InstantAgent:
        name = "probe-instant"
        execution_root = ""
        version_history = {}

        def set_cancellation_check(self, fn):
            self._cancel = fn

        def set_progress_callback(self, fn):
            self._progress = fn

        async def process(self, *args, **kwargs):
            return {"payload": {"items": []}, "requires_intervention": True, "stage_completed": True}

    monkeypatch.setitem(workflow_engine.agent_factories, stage, lambda: InstantAgent())

    # 占住执行权：另一条执行在跑且未被停止
    blocker = _make_run(SID, execution_id="exec-blocker")
    workflow_engine._register_run(blocker)
    workflow_engine._active_sessions.add(SID)
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(workflow_engine.execute_stage(workflow_engine.get_state(SID), stage, {}))

        registry = workflow_engine._active_runs.get(SID) or set()
        assert registry == {blocker}, f"被拒绝的执行不得残留：{registry}"

        # 解除占用后，下一次执行必须能正常开始（不因上次被拒而永久锁死）
        blocker.stop_event.set()
        workflow_engine._unregister_run(blocker)
        result = asyncio.run(workflow_engine.execute_stage(workflow_engine.get_state(SID), stage, {}))
        assert isinstance(result, dict)
        assert workflow_engine.get_state(SID).status[stage.value] in ("waiting", "completed")
    finally:
        workflow_engine._active_sessions.discard(SID)
        workflow_engine._unregister_run(blocker)
