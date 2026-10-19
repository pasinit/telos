"""Nightly-feedback read endpoints (report-only; the SPA renders these).

- ``GET /api/nightly/runs``            — recent nightly runs.
- ``GET /api/nightly/runs/{run_id}``   — one run + its per-agent feedback.
- ``GET /api/agents/{slug}/nightly``   — latest feedback for one of the
  current user's agents (drives the per-agent panel).

Feedback is scoped to the authenticated user: the per-agent endpoint only
returns rows for ``(uid, slug)``; the run endpoints are operator views but
still filter agent rows to the caller's own agents so one user can't read
another's feedback text.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from telos.api import app as _app
from telos.api.deps import current_user

router = APIRouter()
log = logging.getLogger("telos.api")


def _agent_row_json(row) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "user_id": row.user_id,
        "slug": row.slug,
        "status": row.status,
        "feedback": row.feedback,
        "error": row.error,
    }


def _run_json(run) -> dict:
    return {
        "id": run.id,
        "day": run.day,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "memory_summary": run.memory_summary,
        "error": run.error,
        "cost_usd": run.cost_usd,
    }


@router.get("/api/nightly/runs")
async def list_runs(limit: int = 30, uid: str = Depends(current_user)):
    """Recent nightly runs (metadata only)."""
    limit = max(1, min(int(limit), 100))
    runs = _app._nightly_store.list_runs(limit=limit)
    return JSONResponse({"runs": [_run_json(r) for r in runs]})


@router.get("/api/nightly/runs/{run_id}")
async def get_run(run_id: int, uid: str = Depends(current_user)):
    """One run plus the caller's own per-agent feedback rows for it."""
    run = _app._nightly_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    rows = [r for r in _app._nightly_store.list_agent_feedback(run_id) if r.user_id == uid]
    return JSONResponse({"run": _run_json(run), "agents": [_agent_row_json(r) for r in rows]})


@router.get("/api/agents/{slug}/nightly")
async def agent_nightly(slug: str, limit: int = 10, uid: str = Depends(current_user)):
    """Latest nightly feedback rows for one of the caller's agents."""
    limit = max(1, min(int(limit), 50))
    rows = _app._nightly_store.latest_for_agent(uid, slug, limit=limit)
    return JSONResponse({"slug": slug, "feedback": [_agent_row_json(r) for r in rows]})


__all__ = ["router"]
