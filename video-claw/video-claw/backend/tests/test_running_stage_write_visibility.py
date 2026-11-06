"""运行中用户写入的读模型可见性（openspec change: running-stage-write-visibility）。

覆盖 spec `running-stage-read-model` 的 S1-S17，以及 tasks 1.2 的 `_merge_overlay_preview`
表驱动单元断言。只替换模型产出（假 Agent），执行链走真实编排器：**无真实模型调用、无费用**。
必须在既有 backend 容器内执行（命令见 docs/reference/用户空间验收矩阵.md）。

前端条目级闸门（S18-S21）无自动化测试基础，按 tasks 4.4 用浏览器实测收口。
场景编号 ↔ 断言对应关系见 tasks 3.2 映射表。
"""
from __future__ import annotations

import asyncio
import io
import threading
from typing import Any, Dict, List, Optional

import pytest

from api.dependencies import workflow_engine
from core.orchestrator import WorkflowEngine, WorkflowStage
from db.repositories import StageArtifactRepo

from test_core_workflow_integration import (  # noqa: F401  (fixtures 与实现复用，不复制口径)
    ScriptedAgent,
    _bytes,
    _new_session,
    _storyboard,
    _write,
    cleanup_objects,
    isolated_roots,
)

CHARACTER_SPEC: Dict[str, Any] = {
    "stage": WorkflowStage.CHARACTER_DESIGN,
    "stage_key": "character_design",
    "container": "characters",
    "item_type": "characters",
    "ok_id": "c_ok",
    "bad_id": "c_bad",
    "new_id": "c_new",
    "phase": "角色设计",
    "dir": "Assets/characters",
    "extra_keys": {"settings": []},
}
REFERENCE_SPEC: Dict[str, Any] = {
    "stage": WorkflowStage.REFERENCE_GENERATION,
    "stage_key": "reference_generation",
    "container": "scenes",
    "item_type": "scenes",
    "ok_id": "seg_01_01",
    "bad_id": "seg_01_02",
    "new_id": "seg_01_03",
    "phase": "参考图",
    "dir": "Scenes",
    "extra_keys": {},
}
STAGES = pytest.mark.parametrize(
    "spec", [CHARACTER_SPEC, REFERENCE_SPEC], ids=["character_design", "reference_generation"]
)


def _item(container: str, item_id: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """从对外产物快照里定位条目（快照可能是 /status 的 artifacts，也可能是 /artifact 或写响应）。"""
    for item in (snapshot.get(container) or []):
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    raise AssertionError(f"{container} 里没有条目 {item_id}：{snapshot!r}")


class RunningStage:
    """真实执行链里"上报完预览、停在等待点"的阶段，主线程在此期间并发写入。

    脚本语义与真实 Agent 对齐：
    - 执行开始处上报 `assets_preview`，条目字段取自那一刻的已提交产物（相对并发写入是过期的）；
    - 为 `bad_id` 先发 `running` 事件；放行后再发 `done`（本轮成功）或 `failed`（模型不可用）事件；
    - `done_before_gate` 用于"本轮结果先到、用户没手改"的 S8 反向断言；
    - `preview_ids` 可指定预览覆盖的条目（"本轮主动排除"与"预览未列出"两类场景）。
    """

    def __init__(
        self,
        spec: Dict[str, Any],
        *,
        done_result: bool = True,
        done_before_gate: bool = False,
        preview_ids: Optional[List[str]] = None,
        second_preview: bool = False,
    ) -> None:
        self.spec = spec
        self.done_result = done_result
        self.done_before_gate = done_before_gate
        self.preview_ids = preview_ids
        self.second_preview = second_preview
        self.preview_ready = threading.Event()
        self.resume = threading.Event()
        self.errors: List[BaseException] = []
        self.thread: Optional[threading.Thread] = None
        self.emitted_preview_ids: List[str] = []
        self.start_ids: List[str] = []
        self.model_file: Optional[str] = None
        self._sid = ""

    # ── 生命周期 ──
    def start(self, sid: str) -> None:
        self._sid = sid
        workflow_engine.agent_factories[self.spec["stage"]] = lambda: ScriptedAgent(self._behavior())
        state = workflow_engine.get_state(sid)

        def worker() -> None:
            try:
                asyncio.run(workflow_engine.execute_stage(state, self.spec["stage"], {}, progress_callback=None))
            except BaseException as exc:  # noqa: BLE001 - 原样带回主线程断言
                self.errors.append(exc)

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()
        assert self.preview_ready.wait(30), "阶段执行未上报条目清单预览"

    def finish(self) -> None:
        assert self.thread is not None
        self.resume.set()
        self.thread.join(60)
        assert not self.thread.is_alive(), "阶段执行未在 60s 内收尾"
        assert not self.errors, f"阶段执行异常：{self.errors!r}"

    # ── 主线程侧：并发写入与四种对外读入口 ──
    def upload(self, item_id: str, payload: bytes = b"user-upload") -> Dict[str, Any]:
        return workflow_engine.upload_artifact_image(
            session_id=self._sid,
            stage=self.spec["stage_key"],
            item_type=self.spec["item_type"],
            item_id=item_id,
            file_obj=io.BytesIO(payload),
            filename="pick.png",
        )

    def patch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return workflow_engine.update_artifact(self._sid, self.spec["stage_key"], body)

    def view(self) -> Dict[str, Any]:
        """运行中 `/status` 的该阶段产物（前端 2s 轮询所取，与写响应同一漏斗）。"""
        return (workflow_engine.get_status_snapshot(self._sid).get("artifacts") or {}).get(
            self.spec["stage_key"]
        ) or {}

    def status_map(self) -> Dict[str, Any]:
        return workflow_engine.get_status_snapshot(self._sid)["status"]

    def committed(self) -> Dict[str, Any]:
        """已提交层：优先读检查点行，未晋升时回退引擎内已提交视图（均不带覆盖层）。"""
        row = StageArtifactRepo().get(self._sid, self.spec["stage_key"])
        if row and isinstance(row.get("payload_json"), dict) and row["payload_json"].get(self.spec["container"]):
            return row["payload_json"]
        return workflow_engine.get_state(self._sid).artifacts.get(self.spec["stage_key"]) or {}

    # ── 脚本化执行体 ──
    def _preview(self, start: Dict[str, Any]) -> Dict[str, Any]:
        container = self.spec["container"]
        wanted = self.preview_ids if self.preview_ids is not None else [self.spec["ok_id"], self.spec["bad_id"]]
        items = []
        for item in start.get(container) or []:
            if item.get("id") not in wanted:
                continue
            entry = dict(item)
            if entry.get("id") == self.spec["bad_id"]:
                entry["status"] = "running"
            items.append(entry)
        self.emitted_preview_ids = [item.get("id") for item in items]
        return {container: items, **self.spec["extra_keys"]}

    def _event(self, agent, versions: List[str]) -> Dict[str, Any]:
        container, bad_id = self.spec["container"], self.spec["bad_id"]
        if self.done_result:
            return {"type": container, "id": bad_id, "status": "done",
                    "selected": self.model_file, "versions": versions}
        return {"type": container, "id": bad_id, "status": "failed",
                "selected": "", "versions": versions,
                "error_type": "model_unavailable", "error": "mock: timed out"}

    def _behavior(self):
        spec, sid = self.spec, self._sid

        def behavior(agent, _input_data, _intervention):
            container = spec["container"]
            start = workflow_engine.get_state(sid).artifacts.get(spec["stage_key"]) or {}
            self.start_ids = [i.get("id") for i in start.get(container) or [] if isinstance(i, dict)]

            agent._report_progress(spec["phase"], "加载条目列表", 8, data={"assets_preview": self._preview(start)})
            history = agent._history_versions(container, spec["bad_id"])
            agent._report_progress(spec["phase"], f"正在生成: {spec['bad_id']}", 10, data={
                "asset_complete": {"type": container, "id": spec["bad_id"], "status": "running", "versions": history},
            })

            if self.done_before_gate:
                self.model_file = _write(agent, f"{spec['dir']}/{spec['bad_id']}.png", b"model-result")
                agent._report_progress(spec["phase"], "完成", 90,
                                       data={"asset_complete": self._event(agent, [*history, self.model_file])})

            self.preview_ready.set()
            assert self.resume.wait(30), "主线程未在 30s 内放行假 Agent"

            if self.second_preview:
                agent._report_progress(spec["phase"], "再报一次条目列表", 60,
                                       data={"assets_preview": self._preview(start)})

            if self.model_file is None:
                self.model_file = _write(agent, f"{spec['dir']}/{spec['bad_id']}.png", b"model-result")
            agent._report_progress(spec["phase"], "完成" if self.done_result else "失败", 90,
                                   data={"asset_complete": self._event(agent, [*history, self.model_file])})

            # payload 由执行开始视图构造：真实 Agent 同样看不见执行期间的并发写入
            payload_items = []
            for item in start.get(container) or []:
                entry = dict(item)
                if entry.get("id") == spec["bad_id"]:
                    if self.done_result:
                        entry["selected"] = self.model_file
                        entry["versions"] = [*history, self.model_file]
                        entry["status"] = "done"
                    else:
                        # 真实失败路径：本轮没产出，payload 仍按执行开始的视图报 failed
                        entry["selected"] = ""
                        entry["versions"] = list(history)
                        entry["status"] = "failed"
                payload_items.append(entry)
            agent._report_progress(spec["phase"], "完成", 100)
            return {"payload": {container: payload_items, **spec["extra_keys"]}, "stage_completed": True}

        return behavior


def _seed(spec: Dict[str, Any]) -> str:
    """一条目已出图（done）、一条目模型失败（failed、无图）——与案例会话当时的形态一致。"""
    sid = _new_session()
    stage_key, container = spec["stage_key"], spec["container"]
    if stage_key == "reference_generation":
        _storyboard(sid)  # 铺 seg_01_01 / seg_01_02，并由跨阶段同步生成 scenes 条目

    ok_image = workflow_engine.upload_artifact_image(
        session_id=sid, stage=stage_key, item_type=spec["item_type"], item_id=spec["ok_id"],
        file_obj=io.BytesIO(b"ok-image"), filename="ok.png",
    )["path"]

    workflow_engine.update_artifact(sid, stage_key, {
        container: [
            {"id": spec["ok_id"], "name": "已有图条目", "description": "开场",
             "selected": ok_image, "versions": [ok_image], "status": "done"},
            {"id": spec["bad_id"], "name": "失败条目", "description": "收尾",
             "selected": "", "versions": [], "status": "failed"},
        ],
        **spec["extra_keys"],
    })
    return sid


# ────────────────────────────────────────────────────────────────────────────
# 运行中可见性（tasks 3.2：A1、A2/A5、A4、A6/A6b、A8、A9、A10、A7、A11、A13）
# ────────────────────────────────────────────────────────────────────────────

@STAGES
def test_upload_during_run_is_visible_in_every_entry(isolated_roots, cleanup_objects, spec):
    """S1 + S6 + S14 + S15 + A13：写响应、/artifact、/status 三处立即含本次上传。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=False)  # 复刻案例：模型不可用，用户靠上传兜底
    run.start(sid)

    try:
        result = run.upload(spec["bad_id"])
        path = result["path"]
        assert path.startswith("/code/result/_media/"), f"S14: 运行中上传响应不得为空路径，实得 {path!r}"
        assert _bytes(path) == b"user-upload", "S1: 上传对象必须真实可读"

        item = _item(spec["container"], spec["bad_id"], result["artifact"])
        assert item["selected"] == path and path in item["versions"], "S1: 写响应自带产物须含本次写入"
        # S6 口径：选中非空时不得仍显示为失败/待生成；本轮确实还在跑，running 属预期呈现
        assert item["status"] not in ("failed", "pending"), f"S6: 上传后条目状态不得回退：{item!r}"

        for name, snapshot in (
            ("GET /artifact", workflow_engine.get_artifact_snapshot(sid, spec["stage_key"])),
            ("GET /status", run.view()),
        ):
            got = _item(spec["container"], spec["bad_id"], snapshot)
            assert got["selected"] == path, f"{name} 运行中未反映本次上传：{got!r}"
            assert path in (got.get("versions") or []), f"{name} 运行中丢了版本历史：{got!r}"
            assert got.get("status") not in ("failed", "pending"), f"S6: {name} 选中非空却显示失败/待生成：{got!r}"

        assert result["status_map"] == run.status_map(), "S15: 响应状态表与只读快照的重算结果不一致"

        # A13：预览覆盖的条目必须是执行开始基线的子集（测试自洽前提；不构成对 R6 的缓解）
        assert set(run.emitted_preview_ids) <= set(run.start_ids), "A13: 预览条目须来自执行开始基线"
    finally:
        run.resume.set()
        run.thread.join(60)


@STAGES
def test_patch_during_run_is_visible_and_consistent_on_reload(isolated_roots, cleanup_objects, spec):
    """S2 + S5：运行中 PATCH（改描述）响应即含本次修改；丢弃进程内缓存后重读仍一致。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=False)
    run.start(sid)

    try:
        saved = run.patch({spec["container"]: [{"id": spec["ok_id"], "description": "用户新描述"}]})
        assert _item(spec["container"], spec["ok_id"], saved["artifact"])["description"] == "用户新描述", (
            "S2: PATCH 响应未含本次修改"
        )
        assert _item(spec["container"], spec["ok_id"], run.view())["description"] == "用户新描述", (
            "S2: 运行中 /status 未反映本次 PATCH"
        )

        # S5：模拟"刷新页面"——丢掉进程内缓存，从 DB 重装后仍走同一覆盖层视图
        workflow_engine.sessions.clear()
        reloaded = _item(spec["container"], spec["ok_id"], run.view())
        assert reloaded["description"] == "用户新描述", f"S5: 重读后与写响应不一致：{reloaded!r}"
    finally:
        run.resume.set()
        run.thread.join(60)


@STAGES
def test_upload_after_stop_click_before_cleanup_is_visible(isolated_roots, cleanup_objects, spec):
    """S4：点了停止（阶段状态已 stopped）但执行未收尾、覆盖层仍存活 ⇒ 写入照样立即可见。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=False)
    run.start(sid)

    try:
        workflow_engine.stop_session(sid)  # 只置停止标志并归一阶段状态，覆盖层要到收尾才清
        assert run.status_map().get(spec["stage_key"]) == "stopped", "前置：停止后阶段状态应为 stopped"

        path = run.upload(spec["bad_id"])["path"]
        assert path, "S4: 停止后未收尾期间上传响应不得为空"
        item = _item(spec["container"], spec["bad_id"], run.view())
        assert item["selected"] == path, f"S4: 覆盖层存活期间的用户写入被遮蔽：{item!r}"
    finally:
        run.resume.set()
        run.thread.join(60)


@STAGES
def test_untouched_item_still_follows_the_run(isolated_roots, cleanup_objects, spec):
    """S7 + S8（反向断言）：未手改条目的进度与选中仍随本轮结果更新，不被回让规则冻住。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=True, done_before_gate=True)
    run.start(sid)

    try:
        item = _item(spec["container"], spec["bad_id"], run.view())
        model_path = item["selected"]
        assert model_path.startswith("/code/result/_media/"), f"S8 反向断言失败（事件层带基线过头）：{item!r}"
        assert _bytes(model_path) == b"model-result", f"S8: 未手改条目的选中应为本轮结果：{item!r}"
        assert item["status"] == "done", f"S8 反向断言：未手改条目状态应随本轮结果：{item!r}"
        # S7：同页另一条目未被本轮结果影响，仍按预览形态呈现
        ok_item = _item(spec["container"], spec["ok_id"], run.view())
        assert ok_item["selected"] and ok_item["status"] == "done", f"S7: 未参与本轮的条目形态失真：{ok_item!r}"
    finally:
        run.finish()

    after = _item(spec["container"], spec["bad_id"], run.view())
    assert _bytes(after["selected"]) == b"model-result", f"S8: 收尾后本轮结果被丢掉：{after!r}"


@STAGES
def test_late_model_result_does_not_override_user_upload(isolated_roots, cleanup_objects, spec):
    """S9 + S10 + S16 + S17：运行中与收尾后，用户选择的上传图都不被晚到的本轮结果顶掉。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=True)
    run.start(sid)

    path = run.upload(spec["bad_id"])["path"]

    during = _item(spec["container"], spec["bad_id"], run.view())
    assert during["selected"] == path, f"S10: 运行中用户选择被本轮结果顶掉：{during!r}"

    run.finish()

    after = _item(spec["container"], spec["bad_id"], run.view())
    assert after["selected"] == path, f"S16: 收尾后用户选择被晚到结果覆盖：{after!r}"
    versions = after.get("versions") or []
    assert path in versions, f"S9: 用户上传的版本被丢掉：{versions!r}"
    # 本轮新结果以已发布对象形态追加（本地工作根路径不会进入对外产物）
    appended = [v for v in versions if v != path and _bytes(v) == b"model-result"]
    assert appended, f"S9: 本轮新结果应作为追加版本保留：{versions!r}"
    committed = _item(spec["container"], spec["bad_id"], run.committed())
    assert committed["selected"] == path, f"S17: 落库选中与对外读模型不一致：{committed!r}"


@STAGES
def test_second_preview_does_not_hide_earlier_upload(isolated_roots, cleanup_objects, spec):
    """S11：第二次预览（逐条/逐集重发）不得把第一次预览之后发生的用户写入退回旧值。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=True, second_preview=True)
    run.start(sid)

    path = run.upload(spec["bad_id"])["path"]
    run.finish()  # 收尾会走第二次预览；断言在收尾前的视图里更有针对性，故先取运行中快照

    during_or_after = _item(spec["container"], spec["bad_id"], run.view())
    assert during_or_after["selected"] == path, f"S11: 第二次预览遮蔽了更早的用户写入：{during_or_after!r}"
    assert path in (during_or_after.get("versions") or []), f"S11: 版本历史未保留：{during_or_after!r}"


@STAGES
def test_item_created_during_run_is_visible(isolated_roots, cleanup_objects, spec):
    """S12：向"预览与执行开始基线都没有"的条目 id 写入 ⇒ 运行中与收尾后都可见。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=True)
    run.start(sid)
    assert spec["new_id"] not in run.emitted_preview_ids, "前置：预览未覆盖 new_id"
    assert spec["new_id"] not in run.start_ids, "前置：执行开始基线也没有 new_id"

    path = run.upload(spec["new_id"])["path"]
    item = _item(spec["container"], spec["new_id"], run.view())
    assert item["selected"] == path, f"S12: 执行期间新写入的条目未进入读模型：{item!r}"

    run.finish()
    ids = [i.get("id") for i in (run.committed().get(spec["container"]) or [])]
    assert spec["new_id"] in ids, f"S12: 收尾后该条目被本轮 payload 丢弃：{ids!r}"


@STAGES
def test_item_excluded_by_preview_is_not_resurrected(isolated_roots, cleanup_objects, spec):
    """S13：基线有、本轮预览刻意排除的条目 ⇒ 运行中不回填（本约束不扩张条目集合）。"""
    sid = _seed(spec)
    run = RunningStage(spec, done_result=True, preview_ids=[spec["bad_id"]])
    run.start(sid)

    try:
        assert spec["ok_id"] in run.start_ids, "前置：ok_id 在执行开始基线里"
        assert spec["ok_id"] not in run.emitted_preview_ids, "前置：本轮预览刻意排除 ok_id"
        ids = [i.get("id") for i in (run.view().get(spec["container"]) or []) if isinstance(i, dict)]
        assert spec["ok_id"] not in ids, f"S13: 本轮排除的条目被合入回填：{ids!r}"
    finally:
        run.finish()


@STAGES
def test_finish_does_not_mark_uploaded_item_failed(isolated_roots, cleanup_objects, spec):
    """S22 / A14：执行开始前无图 → 运行中上传 → 本轮该条失败并正常收尾 ⇒ 收尾不得标回失败。

    这正是参考图页浏览器实测挖出的缺口：有图却显示"失败 + 重试"，会诱导不必要的模型调用。
    """
    sid = _seed(spec)
    run = RunningStage(spec, done_result=False)
    run.start(sid)

    path = run.upload(spec["bad_id"])["path"]
    during = _item(spec["container"], spec["bad_id"], run.view())
    assert during["selected"] == path, f"S22 前置：运行中选中应为上传图：{during!r}"
    assert during["status"] not in ("failed", "pending"), f"S6: 运行中不得显示失败/待生成：{during!r}"

    run.finish()

    after = _item(spec["container"], spec["bad_id"], run.committed())
    assert after["selected"] == path, f"S22: 收尾后选中被本轮失败结果清掉：{after!r}"
    assert _bytes(after["selected"]) == b"user-upload", "S22: 落库选中应仍是用户上传的对象"
    assert after.get("status") != "failed", f"S22: 收尾把已有选中的条目标回失败：{after!r}"
    view_item = _item(spec["container"], spec["bad_id"], run.view())
    assert view_item.get("status") != "failed", f"S22: 对外读模型仍报失败：{view_item!r}"


# ────────────────────────────────────────────────────────────────────────────
# tasks 1.2：`_merge_overlay_preview` 纯函数表驱动断言（无 DB、无线程）
# ────────────────────────────────────────────────────────────────────────────

_BASE: Dict[str, Any] = {
    "characters": [
        {"id": "c1", "name": "甲", "description": "旧描述", "selected": "", "versions": [], "status": "failed"},
        {"id": "c2", "name": "乙", "description": "旧描述2", "selected": "/code/a.png",
         "versions": ["/code/a.png"], "status": "done"},
    ],
    "settings": [],
}


def _cases():
    """(用例名, preview 里的 c1, committed 里的 c1, 期望字段值, 期望不出现的字段)."""
    base_c1 = _BASE["characters"][0]
    return [
        # 手改 selected ⇒ 取已提交
        ("手改 selected", {**base_c1}, {**base_c1, "selected": "/code/up.png",
                                        "versions": ["/code/up.png"]},
         {"selected": "/code/up.png"}, []),
        # 手改 status（上传把 failed 改判 done）⇒ 取已提交
        ("手改 status", {**base_c1, "status": "running"}, {**base_c1, "status": "done",
                                                          "selected": "/code/up.png"},
         {"status": "done"}, []),
        # 未改动 ⇒ 保留预览的 running（实时进度不回退）
        ("未改动保留进度", {**base_c1, "status": "running"}, {**base_c1, "status": "failed"},
         {"status": "running"}, []),
        # 基线缺该字段且值非 None ⇒ 视为用户新增
        ("基线缺字段新增", {**base_c1}, {**base_c1, "manual_prompt": "人工正文"},
         {"manual_prompt": "人工正文"}, []),
        # 基线缺该字段且值为 None ⇒ 不写进对外产物（同收尾层口径）
        ("基线缺字段但为None", {**base_c1}, {**base_c1, "species": None},
         {}, ["species"]),
        # 未参与本轮的 c2：与基线一致 ⇒ 原样保留预览值
        ("未涉及条目不动", {**base_c1}, {**base_c1}, {}, []),
    ]


@pytest.mark.parametrize("name, preview_c1, committed_c1, expect_present, expect_absent", _cases())
def test_merge_overlay_preview_field_rules(name, preview_c1, committed_c1, expect_present, expect_absent):
    preview = {"session_id": "s1", "characters": [preview_c1, _BASE["characters"][1]], "settings": []}
    committed = {"characters": [committed_c1, _BASE["characters"][1]], "settings": []}

    merged = WorkflowEngine._merge_overlay_preview("character_design", committed, preview, _BASE)
    got = _item("characters", "c1", merged)

    for field, value in expect_present.items():
        assert got.get(field) == value, f"{name}: 期望 {field}={value!r}，实得 {got!r}"
    for field in expect_absent:
        assert field not in got, f"{name}: 不应把 {field} 写进对外产物：{got!r}"
    # 入参不得被就地修改（预览合入必须产出新对象）
    assert merged is not preview and merged["characters"] is not preview["characters"]


def test_merge_overlay_preview_versions_union_and_item_set():
    """版本并集 + committed-only 追加 + 本轮排除不回填 + 非容器键与非列表键原样。"""
    preview = {
        "session_id": "s1",
        # storyboard 的 episodes 不属条目容器：必须原样走预览，不做字段比较（design D4）
        "episodes": [{"episode_number": 1, "segments": [{"segment_id": "seg_01_01"}]}],
        "characters": [dict(_BASE["characters"][0])],
        "settings": [],
    }
    committed = {
        "characters": [
            {**_BASE["characters"][0], "selected": "/code/up.png",
             "versions": ["/code/up.png"], "status": "done"},
            {"id": "c_new", "name": "新条目", "description": "", "selected": "/code/new.png",
             "versions": ["/code/new.png"], "status": "done"},
        ],
        "settings": [],
    }

    merged = WorkflowEngine._merge_overlay_preview("character_design", committed, preview, _BASE)
    ids = [item["id"] for item in merged["characters"]]
    c1 = _item("characters", "c1", merged)

    assert c1["versions"] == ["/code/up.png"], "c1 基线无版本 ⇒ 并集取已提交"
    assert c1["selected"] == "/code/up.png" and c1["status"] == "done"
    assert "c_new" in ids, "committed 有、预览无、基线也无 ⇒ 执行期间新写入须追加"
    assert "c2" not in ids, "基线有而预览无（本轮主动排除）⇒ MUST NOT 回填"
    assert merged["episodes"] == preview["episodes"], "非条目容器须原样走预览"
    assert merged["session_id"] == "s1", "非列表键须原样走预览"
    assert preview["characters"][0]["versions"] == [], "入参 preview 不得被就地修改"


def test_merge_overlay_preview_without_base_degrades_to_preview():
    """无基线（旧覆盖层/降级路径）⇒ 行为与改动前一致：整体呈现预览，不抛错。"""
    preview = {"session_id": "s1", "characters": [dict(_BASE["characters"][0])], "settings": []}
    committed = {"characters": [{**_BASE["characters"][0], "selected": "/code/up.png",
                                 "versions": ["/code/up.png"]}], "settings": []}

    merged = WorkflowEngine._merge_overlay_preview("character_design", committed, preview, None)

    assert _item("characters", "c1", merged)["selected"] == "", "无基线时不得判定手改"
    assert "versions" not in _item("characters", "c1", merged) or \
        _item("characters", "c1", merged)["versions"] == [], "无基线时保持预览形态"
