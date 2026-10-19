"""Chat SSE endpoint: ``POST /agents/{slug}/chat`` and helper JSON APIs.

The SSE handler stays close to the original server.py implementation: a
background ``asyncio.ensure_future(_run_agent())`` task drains the agent's
stream into a queue, and the response generator yields each event as an
SSE frame. Detaching the agent run from the response generator means the
turn completes (and gets recorded) even if the client disconnects mid-stream.

``media_type="text/event-stream"`` and the ``X-Accel-Buffering: no``
header are preserved verbatim — nginx in front of the app will buffer SSE
responses without the header, which kills incremental rendering in the UI.

PR 7E (SPA chat port) adds the JSON helper ``GET /api/agents/{slug}/turns``
so the Svelte ``<Chat>`` component can hydrate history without a
template-injected payload. The legacy SSR ``GET /agents/{slug}`` route in
``agents.py`` still works for the Jinja chat page until PR 7K deletes it.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from telos import Agent
from telos.api import app as _app
from telos.api.deps import current_user, get_agent
from telos.api.presenters.turns import routine_display_label, routine_full_description
from telos.costs import CostLog
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.local_time import format_local_timestamp
from telos.log import ConversationLog

router = APIRouter()
_KEEPALIVE_S = 15.0
log = logging.getLogger("telos.api")


@router.post("/agents/{slug}/chat")
async def chat_stream(
    slug: str,
    request: Request,
    uid: str = Depends(current_user),
    agent: Agent = Depends(get_agent),
):
    body = await request.json()
    message = body.get("message", "").strip()
    image_url = body.get("image")  # optional base64 data URI
    if not message and not image_url:
        raise HTTPException(status_code=400, detail="Empty message")

    if slug != HOUSEKEEPER_SLUG:
        _app._registry.touch(uid, slug)
    log.info("chat_stream start uid=%s slug=%s", uid, slug)

    ctx = _app._agent_runtime.make_context(uid, image_url=image_url)

    # Run astream in a background task so it completes even if client disconnects.
    # Events are pushed to a queue; the SSE generator reads from it.
    queue: asyncio.Queue = asyncio.Queue()

    async def _run_agent():
        try:
            async for kind, data in agent.astream(message, ctx):
                if kind == "text":
                    await queue.put(f"data: {json.dumps({'text': data})}\n\n")
                elif kind == "tool_call":
                    await queue.put(f"data: {json.dumps({'tool_call': data})}\n\n")
                elif kind == "tool_result":
                    await queue.put(f"data: {json.dumps({'tool_result': data})}\n\n")
                elif kind == "pending_routine":
                    await queue.put(f"data: {json.dumps({'pending_routine': data})}\n\n")
                elif kind == "pending_agent":
                    await queue.put(f"data: {json.dumps({'pending_agent': data})}\n\n")
                elif kind == "usage":
                    await queue.put(f"data: {json.dumps({'usage': data})}\n\n")
                elif kind == "turn_id":
                    await queue.put(f"data: {json.dumps({'turn_id': data})}\n\n")
                elif kind == "thinking":
                    # Recordatio-path only: transient extended-thinking delta.
                    # The SPA shows a subdued live status line on the pending
                    # assistant bubble and drops it on the first text token —
                    # never persisted, never rendered as message content.
                    await queue.put(f"data: {json.dumps({'thinking': data})}\n\n")
                elif kind in ("delegation_text", "delegation_tool", "delegation_tool_result"):
                    await queue.put(f"data: {json.dumps({kind: data})}\n\n")
                else:
                    await queue.put(f"data: {json.dumps({'recordatio': data})}\n\n")
        except Exception as e:
            log.exception("chat_stream error uid=%s slug=%s", uid, slug)
            await queue.put(f"data: {json.dumps({'text': f'[error: {e}]'})}\n\n")
        finally:
            # PR 7E: ``final_html`` is gone — the SPA renders markdown via
            # ``lib/markdown.ts`` using the same accumulated raw stream.
            # The legacy ``chat-stream.js`` falls back to its own
            # accumulated render when the field is missing, so this is a
            # behaviour-preserving drop for both surfaces.
            await queue.put(
                f"data: {json.dumps({'done': True, 'has_pending': agent.has_pending_improvement})}\n\n"
            )
            await queue.put(None)  # sentinel

    asyncio.ensure_future(_run_agent())

    async def generate():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_S)
            except TimeoutError:
                # SSE comment: ignored by the SPA's parser, but keeps idle
                # proxies and mobile browsers from dropping the connection
                # during long tool runs or thinking.
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            yield event

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agents/{slug}/turns")
async def list_turns(
    slug: str,
    limit: int = 50,
    uid: str = Depends(current_user),
):
    """Return recent turns for the agent's chat history.

    Replaces the template-injected ``turns`` list from the legacy SSR
    chat page. Response shape mirrors what the Jinja template received
    so the SPA renders the same fields (raw markdown for the assistant
    response — the client renders via ``lib/markdown.ts``).

    Housekeeper-slug semantics match :mod:`telos.api.routes.housekeeper`:
    the agent storage path uses the agent's internal slug (``_slug``)
    so refreshing across versions doesn't lose history.
    """
    if limit < 1:
        limit = 1
    elif limit > 200:
        # Cap so a misbehaving client can't pull the entire DB.
        limit = 200

    if slug == HOUSEKEEPER_SLUG:
        agent = await _app._get_housekeeper(uid)
        db_path = _app.settings.storage_root / uid / agent.slug / "history.db"
    else:
        meta = _app._registry.get(uid, slug)
        if not meta:
            raise HTTPException(status_code=404, detail="Agent not found")
        db_path = _app.settings.storage_root / uid / slug / "history.db"

    rendered: list[dict] = []
    if db_path.exists():
        clog = ConversationLog(db_path)
        turns = clog.recent_turns(limit)
        costs_path = db_path.parent / "costs.db"
        costs_by_turn: dict[str, float] = {}
        if costs_path.exists() and turns:
            costs_by_turn = CostLog(costs_path).events.costs_by_turn(
                [t.turn_id for t in turns]
            )
        for t in turns:
            is_routine = t.is_routine
            rendered.append(
                {
                    "turn_id": t.turn_id,
                    "has_request_id": bool(t.request_id),
                    "trace_id": t.trace_id,
                    "user_message": t.user_message,
                    "routine_label": routine_display_label(t.user_message) if is_routine else "",
                    "routine_description": routine_full_description(t.user_message) if is_routine else "",
                    "assistant_response": t.assistant_response,  # raw markdown — client renders
                    "timestamp": format_local_timestamp(t.timestamp),
                    "is_routine": is_routine,
                    "cost_usd": costs_by_turn.get(t.turn_id, 0.0),
                }
            )
    return JSONResponse({"turns": rendered})


# PR 7E originally landed `GET /api/agents/{slug}` (meta bundle) and
# `GET /api/agents` (sidebar list) here as a provisional measure to
# avoid a shared-file edit. Both moved out in response to reviewer
# feedback:
# - `GET /api/agents/{slug}` → ``telos.api.routes.agents`` (canonical
#   per file-ownership.md row 7E).
# - `GET /api/agents` → already owned by PR 7F in ``agents.py``; this
#   PR drops its duplicate. The 7F endpoint returns ``list_for_user``
#   (housekeeper included); the sidebar filters client-side.
