"""Agent CRUD + status/upload/clear JSON endpoints.

Covers everything except the SSE chat stream (``chat.py``), the per-agent
settings page (``settings_page.py``), the routine endpoints
(``routines.py``), the cost endpoints (``costs.py``), and the
improvements page/api (``improvements.py``).
"""
import logging
import shutil
from datetime import UTC, datetime, timedelta

import fitz  # pymupdf
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from telos import Goal
from telos.api import app as _app
from telos.api.deps import current_user
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.services.goal_costs import make_goal_gen_cost_recorder

router = APIRouter()
log = logging.getLogger("telos.api")


@router.get("/api/agents")
async def agents_list_json(uid: str = Depends(current_user)) -> JSONResponse:
    """List every registry row for the user, including the housekeeper.

    Consumers needing the specialist-only view (e.g. ``AgentsIndex.svelte``)
    filter on ``slug``.
    """
    rows = _app._registry.list_for_user(uid)
    agents = [
        {
            "slug": r["slug"],
            "name": r["name"],
            "description": r.get("description", ""),
            "created_at": r.get("created_at"),
            "last_active": r.get("last_active"),
        }
        for r in rows
    ]
    return JSONResponse(agents)


@router.post("/api/agents")
async def create_agent_json(request: Request, uid: str = Depends(current_user)) -> JSONResponse:
    """Create a specialist agent from the SPA "new agent" form.

    Distinct from ``POST /api/agents/create`` — that endpoint is the
    housekeeper-proposal acceptance path which also notifies the
    housekeeper. This route is the plain user-initiated creation flow.
    """
    body = await request.json()
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    if not name or not description:
        raise HTTPException(status_code=400, detail="Name and description are required")
    goal = await Goal.agenerate(
        name=name,
        description=description,
        model=_app.settings.codex_model if _app.settings.telos_subscription_provider == "codex" else (_app.settings.prompt_gen_model or _app.settings.model),
        subscription_provider=_app.settings.telos_subscription_provider,
        backend=_app.settings.prompt_gen_backend or "openai",
        base_url=_app.settings.prompt_gen_base_url or None,
        api_key=_app.settings.chat_api_key or None,
        on_cost=make_goal_gen_cost_recorder(_app.settings.storage_root, uid, subscription=_app.settings.telos_subscription_provider == "codex"),
    )
    meta = _app._registry.create(uid, goal.name, goal.description, goal.system_prompt, goal.tools)
    return JSONResponse({"slug": meta["slug"], "name": meta["name"]})


@router.post("/api/agents/create")
async def create_agent_api(request: Request, uid: str = Depends(current_user)):
    """Create a specialist agent from a Housekeeper proposal."""
    body = await request.json()
    name = body.get("name", "").strip()
    description = body.get("goal", "").strip()
    if not name or not description:
        raise HTTPException(status_code=400, detail="Name and goal are required")
    goal = await Goal.agenerate(
        name=name,
        description=description,
        model=_app.settings.codex_model if _app.settings.telos_subscription_provider == "codex" else (_app.settings.prompt_gen_model or _app.settings.model),
        subscription_provider=_app.settings.telos_subscription_provider,
        backend=_app.settings.prompt_gen_backend or "openai",
        base_url=_app.settings.prompt_gen_base_url or None,
        api_key=_app.settings.chat_api_key or None,
        on_cost=make_goal_gen_cost_recorder(_app.settings.storage_root, uid, subscription=_app.settings.telos_subscription_provider == "codex"),
    )
    meta = _app._registry.create(uid, goal.name, goal.description, goal.system_prompt, goal.tools)
    housekeeper = await _app._get_housekeeper(uid)
    housekeeper.append_user_context(
        f'[The user confirmed creation of specialist agent "{meta["name"]}" ({meta["slug"]}). '
        f'It is now available for delegation.]'
    )
    return JSONResponse({"ok": True, "slug": meta["slug"], "name": meta["name"]})


@router.post("/agents/{slug}/upload")
async def upload_pdf(
    slug: str,
    file: UploadFile = File(...),
    uid: str = Depends(current_user),  # auth gate
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    data = await file.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        pages = [doc[i].get_text() for i in range(len(doc))]
        doc.close()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not extract text from PDF: {e}") from e
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise HTTPException(status_code=422, detail="No extractable text found in PDF")
    return JSONResponse({"filename": file.filename, "text": text, "pages": len(pages)})


@router.get("/api/agents/{slug}/status")
async def agent_status_endpoint(slug: str, uid: str = Depends(current_user)):
    agent = await _app._get_agent(uid, slug)
    if slug == HOUSEKEEPER_SLUG:
        meta = None
    else:
        meta = _app._registry.get(uid, slug)
    candidate = agent.pending_candidate()
    since_30d = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    try:
        cost_summary_30d = agent.cost_log.cost_summary(since=since_30d)
    except Exception:
        log.exception("cost_summary_30d failed for %s/%s", uid, slug)
        cost_summary_30d = {"total_usd": 0.0, "by_source": {}, "by_model": {}}
    return JSONResponse({
        "has_pending": agent.has_pending_improvement,
        "last_active": meta["last_active"] if meta else None,
        "turn_count": agent.turn_count(),
        "last_turn_complete": agent.last_turn_complete(),
        "candidate": {
            "sha": candidate.sha,
            "rationale": candidate.rationale,
            "score_delta": candidate.score_delta,
        } if candidate else None,
        "cost_summary_30d": cost_summary_30d,
    })


@router.post("/agents/{slug}/clear")
async def clear_history(slug: str, uid: str = Depends(current_user)):
    agent = await _app._get_agent(uid, slug)
    agent.reset()
    db_path = _app.settings.storage_root / uid / slug / "history.db"
    if db_path.exists():
        db_path.unlink()
    _app._agent_runtime.evict(uid, slug)
    return JSONResponse({"ok": True})


@router.delete("/api/agents/{slug}")
async def delete_agent(slug: str, uid: str = Depends(current_user)):
    """Delete a specialist agent (registry row + on-disk state)."""
    _app._agent_runtime.evict(uid, slug)
    _app._registry.delete(uid, slug)
    agent_dir = _app.settings.storage_root / uid / slug
    if agent_dir.exists():
        shutil.rmtree(agent_dir)
    return JSONResponse({"ok": True})


@router.get("/api/agents/{slug}")
async def get_agent_meta(slug: str, uid: str = Depends(current_user)):
    """Return the chat-page hydration bundle for ``slug``."""
    meta = _app._registry.get(uid, slug)
    if not meta:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = await _app._get_agent(uid, slug)
    return JSONResponse(
        {
            "agent": meta,
            "has_pending": agent.has_pending_improvement,
            "usage": agent.cumulative_usage(),
            "is_housekeeper": slug == HOUSEKEEPER_SLUG,
        }
    )
