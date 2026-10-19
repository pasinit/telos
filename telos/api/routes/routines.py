"""Routine endpoints: activate, dismiss, update, delete.

Each handler reads/writes the active ``RoutineRunner`` from
``request.app.state.routine_runner``. The runner is started by the
lifespan in :mod:`telos.api.app` and cleared on shutdown.
Notification messages are appended to the agent's context so the
assistant can acknowledge the user action on its next turn.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from telos.api import app as _app
from telos.api.deps import current_user
from telos.api.state import routine_runner

router = APIRouter()
log = logging.getLogger("telos.api")


@router.post("/api/agents/{slug}/routines/{routine_id}/activate")
async def activate_routine(slug: str, routine_id: str, request: Request, uid: str = Depends(current_user)):
    routine = _app._routine_store.get(routine_id)
    if not routine or routine.user_id != uid:
        raise HTTPException(status_code=404)
    # Update notify_email from request body if provided
    try:
        body = await request.json()
        if "notify_email" in body:
            _app._routine_store.set_notify_email(routine_id, bool(body["notify_email"]))
            routine.notify_email = bool(body["notify_email"])
    except Exception:
        pass
    _app._routine_store.toggle(routine_id, True)
    routine.enabled = True
    log.info("activating routine %s: %r", routine_id, routine.description)
    runner = routine_runner(request)
    if runner:
        runner.add(routine)
    else:
        log.warning("no active routine runner — routine will not be scheduled")
    # Notify the agent
    agent = await _app._get_agent(uid, slug)
    agent.append_user_context(f'[The user activated the routine "{routine.description}" (id={routine.id}). It is now scheduled.]')
    return JSONResponse({"ok": True})


@router.post("/api/agents/{slug}/routines/{routine_id}/dismiss")
async def dismiss_routine(slug: str, routine_id: str, request: Request, uid: str = Depends(current_user)):
    routine = _app._routine_store.get(routine_id)
    if not routine or routine.user_id != uid:
        raise HTTPException(status_code=404)
    desc = routine.description
    _app._routine_store.delete(routine_id)
    # Defensive: if the runner had a job for this routine (e.g. a stale entry
    # from before the create-routine bug fix), remove it so it doesn't fire
    # after the DB row is gone.
    runner = routine_runner(request)
    if runner:
        runner.remove(routine_id)
    # Notify the agent
    agent = await _app._get_agent(uid, slug)
    agent.append_user_context(f'[The user dismissed the proposed routine "{desc}". Do not re-propose it unless asked.]')
    return JSONResponse({"ok": True})


@router.patch("/api/agents/{slug}/routines/{routine_id}")
async def update_routine_endpoint(slug: str, routine_id: str, request: Request, uid: str = Depends(current_user)):
    routine = _app._routine_store.get(routine_id)
    if not routine or routine.user_id != uid:
        raise HTTPException(status_code=404)
    body = await request.json()
    _app._routine_store.update(routine_id, **body)
    # Re-register with scheduler if interval or time changed
    runner = routine_runner(request)
    if routine.enabled and runner and ("interval_seconds" in body or "time_of_day" in body):
        runner.remove(routine_id)
        updated = _app._routine_store.get(routine_id)
        if updated:
            runner.add(updated)
    return JSONResponse({"ok": True})


@router.delete("/api/agents/{slug}/routines/{routine_id}")
async def delete_routine_endpoint(slug: str, routine_id: str, request: Request, uid: str = Depends(current_user)):
    routine = _app._routine_store.get(routine_id)
    desc = routine.description if routine else "unknown"
    runner = routine_runner(request)
    if runner:
        runner.remove(routine_id)
    _app._routine_store.delete(routine_id)
    # Notify the agent
    agent = await _app._get_agent(uid, slug)
    short = desc.split('\n')[0][:80]
    agent.append_user_context(f'[The user deleted the routine "{short}" (id={routine_id}). It is no longer scheduled.]')
    return JSONResponse({"ok": True})
