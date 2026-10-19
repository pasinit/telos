"""Memories endpoint: hydrate the recalled-memories panel for a single turn.

``GET /api/agents/{slug}/turns/{turn_id}/memories`` returns Recordatio's
briefing-event payload for the turn (recalled units, entities, relations,
plus the optional public dashboard URL). Pure relocation from
:mod:`telos.api.app`.
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from telos.api import app as _app
from telos.api.deps import current_user
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.log import ConversationLog

router = APIRouter()
log = logging.getLogger("telos.api")


@router.get("/api/agents/{slug}/turns/{turn_id}/memories")
async def agent_turn_memories(
    slug: str,
    turn_id: str,
    request: Request,
    uid: str = Depends(current_user),
):
    """Return the recalled memories Recordatio injected into a turn's system prompt.

    Three response shapes:

    - ``200 {"recalled": null, "reason": "no_request_id"}`` — the turn has no
      Recordatio ``request_id`` (e.g. it predates this feature, Recordatio was
      disabled, or the briefing was empty). Back-compat path; not an error.
    - ``404`` — Recordatio has no briefing-event row for the recorded
      ``request_id`` (the row was pruned, or never written).
    - ``200 <hydrated payload>`` — recalled units / entities / relations,
      with ``query_text`` and ``topic_hint`` from the briefing event, plus a
      ``recordatio_public_url`` field when the dashboard URL is configured so
      the UI can offer a "view in knowledge graph" link.
    """
    if slug == HOUSEKEEPER_SLUG:
        agent_dir = _app.settings.storage_root / uid / HOUSEKEEPER_SLUG
    else:
        meta = _app._registry.get(uid, slug)
        if not meta:
            raise HTTPException(status_code=404, detail="Agent not found")
        agent_dir = _app.settings.storage_root / uid / slug

    db_path = agent_dir / "history.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="Turn not found")

    clog = ConversationLog(db_path)
    if not clog.turn_exists(turn_id):
        raise HTTPException(status_code=404, detail="Turn not found")

    request_id = clog.get_request_id(turn_id)
    public_url = _app.settings.recordatio_public_url.rstrip("/") if _app.settings.recordatio_public_url else ""
    if not request_id:
        # Back-compat: old turns and turns where briefing didn't fire have no
        # request_id. Return 200 with an explicit shape so the UI can render
        # an empty state instead of a confusing error.
        return JSONResponse({"recalled": None, "reason": "no_request_id"})

    upstream = f"{_app.settings.recordatio_url.rstrip('/')}/v1/briefing-events/{request_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(upstream)
    except httpx.HTTPError:
        log.exception("recordatio briefing fetch failed for %s", request_id)
        raise HTTPException(status_code=502, detail="Recordatio unreachable") from None

    if resp.status_code == 404:
        # Pass Recordatio's 404 through with a clear body so the UI can show a
        # "memories no longer available" message instead of a generic error.
        raise HTTPException(
            status_code=404,
            detail=f"No briefing event recorded for request_id={request_id}",
        )
    if resp.status_code >= 400:
        log.warning("recordatio briefing fetch returned %s for %s", resp.status_code, request_id)
        raise HTTPException(status_code=502, detail="Recordatio returned an error")

    try:
        payload = resp.json()
    except ValueError:
        log.exception("recordatio briefing returned non-JSON for %s", request_id)
        raise HTTPException(status_code=502, detail="Recordatio returned invalid JSON") from None

    payload["recordatio_public_url"] = public_url  # may be empty string
    return JSONResponse(payload)
