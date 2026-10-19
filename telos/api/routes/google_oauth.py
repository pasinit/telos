"""Google OAuth + per-user-settings routes.

- ``GET /api/oauth/google/start`` — SPA-driven full-page nav to Google's consent.
- ``GET /oauth/google/callback`` — Google's redirect target (URI hardcoded
  in Google Cloud Console; keep at this path). Lands the user back at the
  SPA via ``/profile?google=connected|error``.
- ``POST /api/oauth/google/disconnect``
- ``POST /api/user/settings``, ``POST /api/user/email``, ``GET /api/user/email``
- ``GET /api/user/google`` — connection state for the Profile page.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature

from telos.api import app as _app
from telos.api.deps import current_user
from telos.google_auth import build_authorize_url, exchange_code, invalidate

router = APIRouter()
log = logging.getLogger("telos.api")


@router.get("/api/oauth/google/start")
async def api_oauth_google_start(uid: str = Depends(current_user)):
    """SPA entry point. Triggered by a full-page navigation from
    ``Profile.svelte`` — Google's consent screen refuses to render in an
    iframe / XHR, so the SPA must relinquish the page to the browser for
    the round-trip.
    """
    if not _app.settings.google_oauth_client_id or not _app.settings.google_oauth_redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="Google OAuth not configured on this Telos instance.",
        )
    state = _app._signer.dumps({"uid": uid, "purpose": "google_oauth"})
    return RedirectResponse(
        build_authorize_url(
            state,
            _app.settings.google_oauth_client_id,
            _app.settings.google_oauth_redirect_uri,
        ),
        status_code=303,
    )


@router.get("/oauth/google/callback")
async def oauth_google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Google's redirect target. The path is hardcoded in Google Cloud Console."""
    if error:
        log.warning("Google OAuth callback error: %s", error)
        return RedirectResponse("/profile?google=error", status_code=303)
    if not code or not state:
        return RedirectResponse("/profile?google=error", status_code=303)
    try:
        payload = _app._signer.loads(state)
        if not isinstance(payload, dict) or payload.get("purpose") != "google_oauth":
            raise BadSignature("bad payload")
        uid = payload["uid"]
    except (BadSignature, KeyError):
        log.warning("Google OAuth callback: bad state signature")
        return RedirectResponse("/profile?google=error", status_code=303)

    try:
        refresh_token, email = exchange_code(
            code,
            _app.settings.google_oauth_client_id,
            _app.settings.google_oauth_client_secret,
            _app.settings.google_oauth_redirect_uri,
        )
    except Exception:
        log.exception("Google OAuth code exchange failed")
        return RedirectResponse("/profile?google=error", status_code=303)
    _app._registry.set_google_credentials(uid, refresh_token, email)
    invalidate(uid)
    return RedirectResponse("/profile?google=connected", status_code=303)


@router.post("/api/oauth/google/disconnect")
async def api_oauth_google_disconnect(uid: str = Depends(current_user)):
    _app._registry.clear_google_credentials(uid)
    invalidate(uid)
    return JSONResponse({"ok": True})


@router.post("/api/user/settings")
async def update_user_settings(request: Request, uid: str = Depends(current_user)):
    body = await request.json()
    allowed = {"email", "display_name", "timezone", "email_notifications"}
    updates = {k: v for k, v in body.items() if k in allowed}
    _app._registry.update_user_settings(uid, **updates)
    return JSONResponse({"ok": True})


@router.post("/api/user/email")
async def set_user_email(request: Request, uid: str = Depends(current_user)):
    body = await request.json()
    email = body.get("email", "").strip()
    _app._registry.set_user_email(uid, email)
    return JSONResponse({"ok": True, "email": email})


@router.get("/api/user/email")
async def get_user_email(uid: str = Depends(current_user)):
    return JSONResponse({"email": _app._registry.get_user_email(uid)})


@router.get("/api/user/google")
async def get_user_google(uid: str = Depends(current_user)):
    _refresh_token, email = _app._registry.get_google_credentials(uid)
    configured = bool(
        _app.settings.google_oauth_client_id and _app.settings.google_oauth_redirect_uri
    )
    return JSONResponse({"configured": configured, "email": email or None})
