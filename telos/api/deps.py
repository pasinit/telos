"""FastAPI dependency callables for the Telos API.

Routes ``Depends()`` on these to receive the current user (resolved from
the signed cookie OR a ``Bearer`` token) and the module-level service
singletons constructed in :mod:`telos.api.app`.
"""
from fastapi import Cookie, Depends, Header, HTTPException

from telos import Agent
from telos.services import (
    AgentRuntime,
    CostRollupService,
    EmailNotificationService,
    RoutineExecutor,
)

from telos.api import app as _app
from telos.api.auth import tokens
from telos.api.registry import AgentRegistry
from telos.housekeeper import HOUSEKEEPER_SLUG


def current_user(
    user_id: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> str:
    """Resolve the authenticated user from a signed cookie OR Bearer token.

    Bearer wins if both are supplied — that lets a CLI override a stale
    browser cookie deliberately. Same token format in either case (an
    opaque ``URLSafeSerializer`` string).

    Returns 401 on missing or invalid auth. (Pre-Phase-7K, this dep also
    issued a 303 → ``/login`` for non-``/api`` paths to support the SSR
    pages; that branch is gone with SSR.)
    """
    token = tokens.extract_token(user_id, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated (missing)")
    username = tokens.verify(_app._signer, token)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated (invalid)")
    return username


def agent_runtime() -> AgentRuntime:
    return _app._agent_runtime


def registry() -> AgentRegistry:
    return _app._registry


def cost_rollup_service() -> CostRollupService:
    """Return a fresh ``CostRollupService`` bound to the current registry/storage_root.

    Constructed per-call (not cached) so tests that monkeypatch
    ``settings.storage_root`` and ``_registry`` see the patched bindings.
    """
    return CostRollupService(_app._registry, _app.settings.storage_root)


def email_service() -> EmailNotificationService:
    return _app._email_service


def routine_executor() -> RoutineExecutor:
    return _app._routine_executor


async def get_agent(slug: str, uid: str = Depends(current_user)) -> Agent:
    """Resolve an agent for ``(uid, slug)`` with clean HTTP error mapping.

    The exception→HTTP mapping (LookupError → 404; seed failures → 503,
    transient ones with ``Retry-After``) lives in ``_app._resolve_agent``
    so route modules that call ``_app._get_agent`` / ``_get_housekeeper``
    directly get the exact same behaviour as ``Depends()`` users.
    """
    return await _app._resolve_agent(uid, slug)


async def get_housekeeper(uid: str = Depends(current_user)) -> Agent:
    """Resolve the housekeeper agent for the current user (Depends form)."""
    return await _app._resolve_agent(uid, HOUSEKEEPER_SLUG)


__all__ = [
    "current_user",
    "agent_runtime",
    "registry",
    "cost_rollup_service",
    "email_service",
    "routine_executor",
    "get_agent",
    "get_housekeeper",
]
