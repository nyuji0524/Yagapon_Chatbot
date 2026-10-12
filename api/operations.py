"""Authenticated operational health and recovery status endpoints."""

from fastapi import APIRouter, Depends, Query, Request

from api.security import require_api_token

router = APIRouter(dependencies=[Depends(require_api_token)])


@router.get("/admin/operations/summary")
def operations_summary(
    request: Request,
    hours: int = Query(default=24, ge=1, le=24 * 90),
):
    return request.app.state.bot.operations.summary(hours)


@router.get("/admin/operations/events")
def operations_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
):
    entries = request.app.state.bot.operations.recent_events(limit)
    return {"entries": entries, "total": len(entries)}


@router.get("/admin/operations/dead-letters")
def operations_dead_letters(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
):
    entries = request.app.state.bot.operations.pending_dead_letters(limit)
    return {"entries": entries, "total": len(entries)}
