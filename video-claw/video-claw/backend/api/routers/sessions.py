from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from api.dependencies import workflow_engine
from core.identity import CurrentUser, get_current_user, require_admin
from core.ownership import assert_session_owner

router = APIRouter(tags=["Sessions"])


@router.get("/api/sessions")
async def list_sessions(user: CurrentUser = Depends(get_current_user)):
    return {"sessions": await run_in_threadpool(workflow_engine.list_saved_sessions, owner=user.login)}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, user: CurrentUser = Depends(get_current_user)):
    """删除当前 owner 的历史记录；他人会话按不存在处理。"""
    await run_in_threadpool(assert_session_owner, workflow_engine, session_id, user)
    deleted = await run_in_threadpool(workflow_engine.delete_session, session_id)
    if not deleted:
        raise HTTPException(404, "Session not found")
    return {"status": "deleted", "session_id": session_id}


@router.delete("/api/sessions")
async def cleanup_orphan_files(
    dry_run: bool = False, _: CurrentUser = Depends(require_admin)
):
    """清理孤立的结果文件（管理员维护操作；dry_run 只报告）。"""
    return await run_in_threadpool(workflow_engine.cleanup_orphan_results, dry_run=dry_run)
