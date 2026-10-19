"""Per-agent settings JSON API + prompt-candidate + tool-picker routes.

- ``GET  /api/agents/{slug}/settings`` returns the settings-page payload
  the Svelte ``AgentSettings.svelte`` consumes.
- ``POST /api/agents/{slug}/{approve,reject,rollback}`` return JSON
  ``{ok, new_prompt}``.
- ``POST /api/agents/{slug}/tools`` updates the per-agent tool whitelist.

The tool-picker uses :mod:`telos.api.presenters.tools` to bucket the
available tools by category.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from telos.api import app as _app
from telos.api.deps import current_user
from telos.api.presenters.tools import group_available_tools
from telos.api.state import routine_runner
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.log import ConversationLog
from telos.tools import tool_repository

router = APIRouter()


async def _collect(uid: str, slug: str, request: Request) -> dict:
    """Gather the raw inputs that feed the settings JSON payload."""
    meta = _app._registry.get(uid, slug)
    if not meta:
        raise HTTPException(status_code=404)
    agent = await _app._get_agent(uid, slug)
    clog = ConversationLog(_app.settings.storage_root / uid / slug / "history.db")
    prompt_history = clog.prompt_history()
    signal_summary = clog.signal_summary()
    candidate = agent.pending_candidate()
    agent_tools = json.loads(meta.get("tools") or "[]")
    # Hide housekeeper-only tools from the picker for specialists.
    is_hk = slug == HOUSEKEEPER_SLUG
    available_tools = [
        {"name": t.name, "description": t.description, "type": t.type}
        for t in tool_repository.list_all()
        if is_hk or not t.housekeeper_only
    ]
    runner = routine_runner(request)
    routines = [{**r.__dict__, "next_run": runner.get_next_run(r.id) if runner else None} for r in _app._routine_store.list_for_agent(uid, slug)]
    return {
        "meta": meta,
        "agent": agent,
        "current_prompt": agent.system_prompt,
        "prompt_history": prompt_history,
        "signal_summary": signal_summary,
        "has_pending": agent.has_pending_improvement,
        "candidate": candidate,
        "agent_tools": agent_tools,
        "tool_groups": group_available_tools(available_tools),
        "routines": routines,
    }


def _candidate_to_dict(candidate) -> dict | None:
    if candidate is None:
        return None
    return {
        "sha": candidate.sha,
        "content": candidate.content,
        "rationale": candidate.rationale,
        "score_delta": candidate.score_delta,
        "run_id": candidate.run_id,
    }


@router.get("/api/agents/{slug}/settings")
async def settings_api(slug: str, request: Request, uid: str = Depends(current_user)):
    """Return the settings-page payload as JSON."""
    data = await _collect(uid, slug, request)
    return JSONResponse({
        "user_id": uid,
        "agent": data["meta"],
        "current_prompt": data["current_prompt"],
        "prompt_history": data["prompt_history"],
        "signal_summary": data["signal_summary"],
        "has_pending": data["has_pending"],
        "candidate": _candidate_to_dict(data["candidate"]),
        "agent_tools": data["agent_tools"],
        "tool_groups": data["tool_groups"],
        "routines": data["routines"],
    })


@router.post("/api/agents/{slug}/approve")
async def approve(slug: str, uid: str = Depends(current_user)):
    agent = await _app._get_agent(uid, slug)
    agent.approve()
    return JSONResponse({"ok": True, "new_prompt": agent.system_prompt})


@router.post("/api/agents/{slug}/reject")
async def reject(slug: str, uid: str = Depends(current_user)):
    agent = await _app._get_agent(uid, slug)
    agent.reject()
    return JSONResponse({"ok": True, "new_prompt": agent.system_prompt})


@router.post("/api/agents/{slug}/rollback")
async def rollback(slug: str, request: Request, uid: str = Depends(current_user)):
    """Revert to a previously deployed prompt version. ``sha`` from form data."""
    form = await request.form()
    sha = form.get("sha", "")
    if not sha:
        raise HTTPException(status_code=400, detail="sha required")
    agent = await _app._get_agent(uid, slug)
    agent.rollback(sha)
    return JSONResponse({"ok": True, "new_prompt": agent.system_prompt})


@router.post("/api/agents/{slug}/tools")
async def update_tools(slug: str, request: Request, uid: str = Depends(current_user)):
    body = await request.json()
    tools = body.get("tools", [])
    _app._registry.update_tools(uid, slug, tools)
    _app._agent_runtime.evict(uid, slug)
    return JSONResponse({"ok": True, "tools": tools})
