"""Improvement-run history JSON API (issue #44).

``GET /api/agents/{slug}/improvements`` returns a paginated JSON page of
improvement-run rows. Filters: ``decision`` (one of
pending/approved/rejected/no_improvement/error), ``since``/``until``
(ISO 8601), ``limit`` (1..200), ``offset`` (>=0).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from telos.api import app as _app
from telos.api.deps import current_user
from telos.log import ConversationLog

router = APIRouter()
log = logging.getLogger("telos.api")

_IMPROVEMENT_DECISIONS = {"pending", "approved", "rejected", "no_improvement", "error"}


@router.get("/api/agents/{slug}/improvements")
async def agent_improvements_api(
    slug: str,
    decision: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
    uid: str = Depends(current_user),
):
    meta = _app._registry.get(uid, slug)
    if not meta:
        raise HTTPException(status_code=404)
    if decision is not None and decision not in _IMPROVEMENT_DECISIONS:
        raise HTTPException(status_code=400, detail=f"invalid decision: {decision!r}")
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = 50
    try:
        offset_int = int(offset)
    except (TypeError, ValueError):
        offset_int = 0
    limit_int = max(1, min(limit_int, 200))
    offset_int = max(0, offset_int)

    clog = ConversationLog(_app.settings.storage_root / uid / slug / "history.db")
    try:
        runs = clog.list_runs(
            agent_slug=slug,
            decision=decision,
            since=since,
            until=until,
            limit=limit_int,
            offset=offset_int,
        )
    except Exception:
        log.exception("list_runs failed for %s/%s", uid, slug)
        raise HTTPException(status_code=500, detail="list_runs failed") from None
    return JSONResponse({
        "slug": slug,
        "limit": limit_int,
        "offset": offset_int,
        "runs": runs,
    })
