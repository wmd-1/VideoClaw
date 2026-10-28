"""Personal-space API: derived works, counts and saved-item operations."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from core.identity import CurrentUser, get_current_user
from core.space_service import counts, delete_saved, list_saved, public_works, save_work

router = APIRouter(tags=["Space"])


@router.get("/api/space/works")
async def get_works(
    domain: str = Query("all"),
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        return await run_in_threadpool(public_works, user.login, domain, limit, offset)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get("/api/space/counts")
async def get_counts(user: CurrentUser = Depends(get_current_user)):
    return await run_in_threadpool(counts, user.login)


@router.get("/api/space/saved")
async def get_saved(
    limit: int = Query(24, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(get_current_user),
):
    return await run_in_threadpool(list_saved, user.login, limit, offset)


@router.post("/api/space/saved")
async def create_saved(payload: dict, user: CurrentUser = Depends(get_current_user)):
    work_id = str(payload.get("work_id") or "").strip()
    if not work_id:
        raise HTTPException(400, detail="work_id 不能为空")
    try:
        return await run_in_threadpool(save_work, user.login, work_id)
    except KeyError as exc:
        raise HTTPException(404, detail="Work not found") from exc
    except OverflowError as exc:
        raise HTTPException(409, detail="saved_limit_exceeded") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="Source media missing") from exc


@router.delete("/api/space/saved/{saved_id}")
async def remove_saved(saved_id: str, user: CurrentUser = Depends(get_current_user)):
    try:
        await run_in_threadpool(delete_saved, user.login, saved_id)
    except PermissionError as exc:
        raise HTTPException(404, detail="Saved item not found") from exc
    return {"status": "deleted", "saved_id": saved_id}
