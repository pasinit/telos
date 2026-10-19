"""Cost & usage JSON endpoints.

- ``GET /api/agents/{slug}/costs`` — per-agent cost summary (SQL aggregate).
- ``GET /api/agents/{slug}/costs/recent`` — most-recent cost events.
- ``GET /api/usage`` — cross-agent rollup for the authenticated user.

Rollup uses :class:`telos.services.cost_rollup.CostRollupService` so the
heavy lifting stays out of the route layer.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from telos.api import app as _app
from telos.api.deps import cost_rollup_service, current_user
from telos.services import CostRollupService
from telos.services.cost_rollup import resolve_since

router = APIRouter()
log = logging.getLogger("telos.api")


@router.get("/api/agents/{slug}/costs")
async def agent_costs_endpoint(
    slug: str,
    since: str | None = None,
    uid: str = Depends(current_user),
):
    """Return the per-agent cost summary."""
    agent = await _app._get_agent(uid, slug)
    try:
        summary = agent.cost_log.cost_summary(since=since)
    except Exception:
        log.exception("cost_summary failed for %s/%s", uid, slug)
        raise HTTPException(status_code=500, detail="cost_summary failed") from None
    return JSONResponse({"slug": slug, "since": since, **summary})


@router.get("/api/agents/{slug}/costs/recent")
async def agent_costs_recent_endpoint(
    slug: str,
    limit: int = 10,
    uid: str = Depends(current_user),
):
    """Return the most-recent cost events for an agent.

    ``limit`` is clamped to [1, 100].
    """
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = 10
    limit_int = max(1, min(limit_int, 100))
    agent = await _app._get_agent(uid, slug)
    try:
        events = agent.cost_log.recent_events(limit=limit_int)
    except Exception:
        log.exception("recent_events failed for %s/%s", uid, slug)
        raise HTTPException(status_code=500, detail="recent_events failed") from None
    return JSONResponse({"slug": slug, "limit": limit_int, "events": events})


@router.get("/api/usage")
async def usage_rollup_endpoint(
    since: str | None = None,
    uid: str = Depends(current_user),
    rollup_svc: CostRollupService = Depends(cost_rollup_service),
):
    """Cross-agent cost rollup for the authenticated user.

    The ``since`` query may be an ISO 8601 timestamp or one of the short
    tokens ``24h``/``7d``/``30d``/``all``.
    """
    iso = since
    if since and since.strip().lower() in {"", "24h", "7d", "30d", "all", "all-time"}:
        iso, _ = resolve_since(since)
    rollup = rollup_svc.rollup(uid, iso)
    return JSONResponse(rollup)
