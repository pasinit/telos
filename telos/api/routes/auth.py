"""Auth + profile JSON API.

Single source of truth for authentication. Login returns ``{token, user}``
AND sets the cookie, so the same user can drive the API via cookie
(browser) or Bearer (CLI). See :mod:`telos.api.auth.tokens` and
:mod:`telos.api.auth.cookies`.
"""
import logging

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from telos.api import app as _app
from telos.api.auth import cookies as auth_cookies
from telos.api.auth import tokens as auth_tokens
from telos.api.deps import current_user
from telos.utils import SAFE_ID_DESCRIPTION, is_safe_id

log = logging.getLogger("telos.api.routes.auth")

router = APIRouter()


def _normalize_username(value: str) -> str:
    """Normalise a login username to its canonical user_id form
    (whitespace-stripped, lowercased).

    Pure normalisation — no charset validation here. Telos has no
    separate registration endpoint: the FIRST login mints the user_id
    (implicit signup), so the charset gate lives in :func:`login_api`
    where it can distinguish "minting a new id" (strict — see
    :data:`telos.utils.SAFE_ID_RE`) from "an existing user logging back
    in" (never rejected on charset, or a pre-rule id would be locked
    out of its own account).
    """
    return value.strip().lower()


class LoginBody(BaseModel):
    """JSON body for ``POST /api/auth/login``."""

    username: str = Field(..., min_length=1)
    email: str | None = None


class LoginResponse(BaseModel):
    token: str
    user: dict[str, str]


@router.post("/api/auth/login", response_model=LoginResponse)
async def login_api(body: LoginBody, response: Response):
    """Issue a token + set the cookie.

    The response body's ``token`` is the same value the cookie carries —
    CLIs hold it as a Bearer; browsers ignore it and rely on the cookie.
    """
    username = _normalize_username(body.username)
    if not username:
        return JSONResponse(
            status_code=400,
            content={"detail": f"username required: {SAFE_ID_DESCRIPTION}"},
        )
    if not is_safe_id(username):
        # Signup-time charset gate: the first login mints the user_id
        # (it doubles as an MCP path segment ``/mcp/{user_id}/{slug}``
        # and a directory under ``storage_root``), so a NEW id outside
        # SAFE_ID_RE is rejected. An id already present in the registry
        # predates the rule — admit it (its MCP surface stays strictly
        # validated server-side; see ``telos.mcp.server._validate_id``)
        # and leave a trail for the operator.
        if not _app._registry.user_exists(username):
            return JSONResponse(
                status_code=400,
                content={"detail": f"username required: {SAFE_ID_DESCRIPTION}"},
            )
        log.warning(
            "login for existing user %r whose id violates the current "
            "charset rule (%s); admitted (grandfathered), but the MCP "
            "layer will reject this id",
            username, SAFE_ID_DESCRIPTION,
        )
    if body.email and body.email.strip():
        _app._registry.set_user_email(username, body.email.strip())
    token = auth_tokens.issue(_app._signer, username)
    auth_cookies.set_auth_cookie(
        response,
        token,
        public_host=_app.settings.public_host,
    )
    return LoginResponse(token=token, user={"uid": username})


@router.get("/api/auth/me")
async def me(uid: str = Depends(current_user)):
    """Return the current user. Cheap probe for "still authenticated?"."""
    return {"uid": uid}


@router.post("/api/auth/logout")
async def logout_api(response: Response):
    """Clear the cookie. Returns 200 JSON.

    NOT gated by ``current_user`` so a client with an expired token can
    still clean up its cookie without first getting a 401.
    """
    auth_cookies.delete_auth_cookie(response)
    return {"ok": True}
