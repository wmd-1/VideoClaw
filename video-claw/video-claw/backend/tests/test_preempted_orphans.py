"""被抢占/未合入执行留下的对象在清理器中的资格（tasks 6.4 取证）。

结论口径：本进程发布的 `prepared` 对象不会被本进程清理器回收，
要等下次启动换 `boot_id` 后才具备资格——这是已知限制，不是"已回收"。
"""
from __future__ import annotations

import os

from api.dependencies import workflow_engine
from core import workflow_store
from core.orchestrator import WorkflowStage, _StageRun
from db import database
from db.repositories.operations import ObjectOperationRepo

SID = "probe_orphans_1"
EXECUTION_ID = "exec-orphan-probe"


def _operation_rows(execution_id: str):
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bucket, object_key, state, boot_id FROM object_operations WHERE execution_id = %s",
                (execution_id,),
            )
            return list(cur.fetchall())


def test_interrupted_execution_objects_are_not_eligible_in_current_boot():
    workflow_engine.create_session(SID, {"idea": "probe orphan"}, owner="alice")
    run = _StageRun(
        session_id=SID, stage=WorkflowStage.CHARACTER_DESIGN,
        execution_id=EXECUTION_ID, background=False, intervention=None,
    )
    run.owner = "alice"
    run.publisher = workflow_store.ExecutionPublisher(
        owner="alice", session_id=SID, stage="character_design",
        execution_id=EXECUTION_ID, boot_id=workflow_engine._boot_id,
    )
    os.makedirs(run.publisher.work_root, exist_ok=True)
    local = os.path.join(run.publisher.work_root, "probe.png")
    with open(local, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    payload = {"characters": [{"id": "c1", "name": "probe", "selected": local, "versions": [local]}]}
    _mapped, published = run.publisher.publish({"character_design": payload}, set())
    assert published and published.get("published"), "探针必须先真的发布出一个对象"

    rows = _operation_rows(EXECUTION_ID)
    assert rows, "发布后必须留下 object_operations 行"
    assert all(str(row["state"]) == "prepared" for row in rows), rows
    assert all(str(row["boot_id"] or "") == workflow_engine._boot_id for row in rows), rows

    repo = ObjectOperationRepo()
    same_boot = {str(r["object_key"]) for r in repo.list_eligible(workflow_engine._boot_id, limit=500)}
    other_boot = {str(r["object_key"]) for r in repo.list_eligible("another-boot-id", limit=500)}
    keys = {str(row["object_key"]) for row in rows}
    assert not (keys & same_boot), "当前进程的 prepared 不得被本进程清理器回收"
    assert keys <= other_boot, "换 boot_id 后才具备回收资格（最迟下次启动）"
