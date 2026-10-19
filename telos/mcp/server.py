"""FastMCP host that exposes Telos tools to Recordatio over HTTP.

Mounted at ``/mcp/{user_id}/{slug}`` on the Telos FastAPI app.

Wire-up overview
================

- A single :class:`mcp.server.fastmcp.FastMCP` instance holds every
  Telos tool, registered through :mod:`telos.mcp.tool_adapters`. Tools
  are stateless; per-call context (user id, slug, workspace) is
  carried on a :class:`ContextVar` that the ASGI dispatcher sets
  before forwarding the request.
- The FastMCP ``streamable_http_app`` listens at ``/mcp``. We wrap it
  in an ASGI router that:
  1. Extracts ``user_id`` and ``slug`` from the request path
     (``/mcp/{user_id}/{slug}``).
  2. Validates a per-(user_id, slug) bearer token derived from the
     master secret — recomputed from the path the request targets, so
     one agent's token cannot reach another agent's surface.
  3. Sets :data:`telos.mcp.tool_adapters.current_mcp_request` so the
     per-tool handler knows who is calling.
  4. Rewrites ``scope["path"]`` to ``/mcp`` and forwards to the
     FastMCP session manager.

Auth scheme
===========

Per-(user_id, slug) bearer tokens derived from one master secret:

- The master secret comes from the ``TELOS_MCP_AUTH_TOKEN`` env var, or
  — when unset — from a generated secret persisted under the telos data
  dir (``<storage_root>/mcp_master_secret``) so it survives restarts.
- The bearer token a caller must present on ``/mcp/{user_id}/{slug}``
  is ``HMAC-SHA256(master_secret, f"{user_id}:{slug}")`` (hex digest);
  see :func:`derive_agent_token`. ``_auth_ok`` recomputes the expected
  token from the path the request actually targets and constant-time
  compares, so a token minted for one agent cannot reach another
  agent's MCP surface (e.g. ``/mcp/{anyuser}/_housekeeper``).
- The seed path (:func:`telos.services.recordatio_agent_seed
  .build_agent_config_payload`) derives the same per-(user, slug) token
  when baking the ``Authorization`` header into each agent's Recordatio
  config.

The token may be supplied either as the ``Authorization: Bearer
<TOKEN>`` header (preferred) or as the ``X-Telos-Mcp-Token`` header
(simpler for tests / curl).

Lifespan
========

The FastMCP session manager requires its async ``run()`` context to be
active before handling requests. :meth:`TelosMcpHost.lifespan` enters
that context and yields, so the host should be wired into the parent
app's lifespan via :class:`contextlib.AsyncExitStack` (see
:func:`mount_mcp_host`).
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette._utils import get_route_path
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from telos.core.ports import AgentRegistry, Tool
from telos.mcp.browser_sessions import BrowserSessions
from telos.mcp.tool_adapters import (
    McpRequestContext,
    current_mcp_request,
    install_scoped_tool_listing,
    register_tools,
)
from telos.utils import SAFE_ID_DESCRIPTION, is_safe_id

log = logging.getLogger("telos.mcp.server")

# Path prefix the host is mounted at. Everything under this path is
# considered MCP traffic and must carry an auth token.
MCP_MOUNT_PATH = "/mcp"

# Header names the host accepts auth tokens on. We accept either to
# keep curl / test ergonomics simple while staying compatible with the
# standard ``Authorization: Bearer`` form Recordatio uses in production.
_AUTH_HEADER_BEARER = b"authorization"
_AUTH_HEADER_CUSTOM = b"x-telos-mcp-token"

# Allowlist for the path components we splice into the workspace
# filesystem path — shared with the login layer via
# :data:`telos.utils.SAFE_ID_RE` so signups can't mint ids the MCP
# layer rejects. Validation here stays STRICT on every request (these
# ids hit the filesystem), while the login route enforces the charset
# only when minting a NEW id and grandfathers pre-rule users — see the
# commentary on ``SAFE_ID_RE`` in :mod:`telos.utils`.
# ``.``/``@``/``+`` are admitted (email-style usernames, incl.
# plus-addressing); ``/``, ``:``, NUL, whitespace stay banned, and
# :func:`telos.utils.is_safe_id` explicitly rejects ``..`` and any
# leading-dot segment — guards against ``user_id=../../etc`` style
# traversal escaping ``storage_root``.

# Name of the file under ``storage_root`` that holds the generated MCP
# master secret when ``TELOS_MCP_AUTH_TOKEN`` is unset.
MASTER_SECRET_FILENAME = "mcp_master_secret"


def _validate_id(value: str, *, field: str) -> str:
    """Reject path components that aren't a safe telos id.

    Used on every ``user_id``/``slug`` extracted from an MCP request
    path before they flow into :func:`_default_workspace_for` (which
    calls ``mkdir(parents=True)`` and would otherwise create
    attacker-controlled directories outside ``storage_root``).
    """
    if not is_safe_id(value):
        raise ValueError(f"invalid {field}: must be {SAFE_ID_DESCRIPTION}")
    return value


def derive_agent_token(master_secret: str, user_id: str, slug: str) -> str:
    """Derive the bearer token for one ``(user_id, slug)`` MCP surface.

    ``HMAC-SHA256(master_secret, f"{user_id}:{slug}")`` as a hex digest.
    Both ends use this: :meth:`TelosMcpHost._auth_ok` recomputes it from
    the request path, and the Recordatio seed bakes it into each agent's
    ``Authorization`` header — so a leaked token grants access to
    exactly one agent's tool surface, not every user's housekeeper.

    ``user_id`` may legally contain ``.``/``@`` but never ``:`` or
    ``/`` (see :data:`telos.utils.SAFE_ID_RE`), so the ``:`` separator
    is unambiguous.
    """
    return hmac.new(
        master_secret.encode("utf-8"),
        f"{user_id}:{slug}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _redact_token(token: str | None) -> str:
    """Return a stable short fingerprint of ``token`` safe to log.

    Logs hit a 30-day retention bucket, so the raw secret must never
    appear. The fingerprint lets operators correlate a logged token
    with the one they configured without leaking it.
    """
    if not token:
        return "none"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]

# Default workspace layout: ``<storage_root>/<user_id>/<slug>/workspace``
# — same as :class:`telos.Agent` (see ``telos/agent.py`` around line
# 351). Tools that read ``ctx.workspace`` will see the same directory
# tree they see when called in-process by the agent.
def _default_workspace_for(storage_root: Path, user_id: str, slug: str) -> Path:
    # Defence-in-depth: the ASGI dispatcher validates already, but the
    # workspace_for hook is part of the public ctor API so anyone
    # calling it directly still gets a safe path.
    _validate_id(user_id, field="user_id")
    _validate_id(slug, field="slug")
    ws = storage_root / user_id / slug / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


class TelosMcpHost:
    """Owns the per-process FastMCP instance and the ASGI dispatcher
    that routes ``/mcp/{user_id}/{slug}`` requests into it.

    One :class:`TelosMcpHost` per Telos process. The dispatcher is
    mounted on the FastAPI app via :meth:`mount`. Lifecycle management
    is handled by :meth:`lifespan` — an async context manager that
    enters the FastMCP session manager's ``run()`` and yields.
    """

    def __init__(
        self,
        *,
        tools: dict[str, Tool],
        storage_root: Path,
        auth_token: str,
        workspace_for: Callable[[Path, str, str], Path] = _default_workspace_for,
        registry: AgentRegistry | None = None,
    ) -> None:
        """
        Args:
            tools:         The same ``dict[str, Tool]`` an :class:`Agent`
                           uses. Wired once by the app factory; the host
                           re-exports every entry that isn't a downstream
                           MCP-discovered tool (see
                           :func:`telos.mcp.tool_adapters.register_tools`).
            storage_root:  Telos's per-user storage root — used to derive
                           a per-(user, slug) workspace path.
            auth_token:    MASTER secret. The bearer token expected on a
                           ``/mcp/{user_id}/{slug}`` request is derived
                           from it via :func:`derive_agent_token` and
                           constant-time-compared — never the master
                           value itself.
            workspace_for: Override hook for tests. Default builds
                           ``storage_root/<user_id>/<slug>/workspace``.
            registry:      Agent registry used to scope what each
                           ``/mcp/{user_id}/{slug}`` surface ADVERTISES
                           on ``tools/list`` — each agent sees only its
                           own configured tool subset (see
                           :func:`telos.mcp.tool_adapters
                           .install_scoped_tool_listing`). ``None``
                           keeps the unscoped listing (minus
                           housekeeper-only tools for non-housekeeper
                           slugs). Invocation gating is unaffected.
        """
        self._storage_root = Path(storage_root)
        self._master_secret = auth_token
        self._workspace_for = workspace_for
        self._browser_sessions = BrowserSessions()
        # DNS-rebinding protection is off here because the host runs
        # behind Telos's own FastAPI app, which enforces its own
        # ``Host`` checks via the deployment's reverse proxy. Leaving
        # FastMCP's DNS-rebinding middleware on would refuse traffic
        # from any hostname not in its hard-coded localhost list — and
        # Recordatio connects on the deployment's real public host.
        self._fastmcp = FastMCP(
            name="telos",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            ),
        )
        # The FastMCP transport listens at this exact path inside its
        # own Starlette app. We mount that app under ``/mcp`` ourselves,
        # so the inner path stays ``/``.
        self._fastmcp.settings.streamable_http_path = "/"
        self._registered = register_tools(self._fastmcp, tools=tools, registry=registry)
        # Per-request listing scope: each agent's surface advertises only
        # its own configured tools (the housekeeper keeps the full set).
        install_scoped_tool_listing(
            self._fastmcp, registered=self._registered, registry=registry,
        )
        log.info(
            "TelosMcpHost initialised with %d tools: %s",
            len(self._registered),
            ", ".join(self._registered),
        )
        # Built lazily on first lifespan entry.
        self._inner_app: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Enter the FastMCP session manager's ``run()`` context.

        Must be entered before the dispatcher receives any traffic; the
        session manager spawns the per-request tasks inside the task
        group it owns. Idiomatically wired in :func:`mount_mcp_host` via
        :class:`contextlib.AsyncExitStack`.
        """
        # ``streamable_http_app()`` initialises the session manager
        # lazily, so we have to call it before reading ``.session_manager``.
        self._inner_app = self._fastmcp.streamable_http_app()
        async with self._fastmcp.session_manager.run():
            reaper = asyncio.create_task(self._browser_sessions.reap_forever())
            try:
                yield
            finally:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
                await self._browser_sessions.close_all()

    # ------------------------------------------------------------------
    # ASGI dispatcher
    # ------------------------------------------------------------------

    async def asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entrypoint for the mounted ``/mcp`` route tree.

        The Starlette ``Mount`` upstream has already stripped the
        ``/mcp`` prefix off ``scope["path"]``; what we see here looks
        like ``/{user_id}/{slug}`` (optionally followed by extra
        segments — none today, room for tomorrow).
        """
        if scope["type"] != "http":
            await _send_404(send)
            return

        # ``Mount("/mcp", app=...)`` adjusts ``scope["root_path"]`` but
        # leaves ``scope["path"]`` as the full URL path; the helper
        # below returns just the suffix the mount delegated.
        route_path = get_route_path(scope)
        parts = [p for p in route_path.split("/") if p]
        if len(parts) < 2:
            await _send_text(send, 404, "MCP path must be /mcp/<user_id>/<slug>")
            return
        user_id, slug = parts[0], parts[1]

        # Reject path-traversal-shaped inputs (``../``, ``foo/bar``,
        # NUL bytes, etc.) BEFORE any auth comparison or filesystem
        # call. ``_default_workspace_for`` calls ``mkdir(parents=True)``
        # so an unvalidated ``user_id`` of ``../../etc`` would escape
        # ``storage_root``.
        try:
            _validate_id(user_id, field="user_id")
            _validate_id(slug, field="slug")
        except ValueError as exc:
            await _send_text(send, 400, str(exc))
            return

        if not self._auth_ok(scope, user_id=user_id, slug=slug):
            await _send_text(send, 401, "missing or invalid MCP auth token")
            return

        if self._inner_app is None:
            # Lifespan didn't run — should be impossible if mounted via
            # :func:`mount_mcp_host`, but fail loudly rather than hang.
            await _send_text(send, 503, "MCP host not started — lifespan not entered")
            return

        turn_id = dict(scope.get("headers", [])).get(b"x-telos-turn-id", b"").decode("ascii", errors="ignore")
        if turn_id:
            try:
                _validate_id(turn_id, field="turn_id")
            except ValueError:
                await _send_text(send, 400, "invalid turn id")
                return
        if scope.get("method") == "DELETE" and turn_id:
            await self._browser_sessions.close((user_id, slug, turn_id))
            await _send_text(send, 200, "Turn browser closed")
            return

        workspace = self._workspace_for(self._storage_root, user_id, slug)
        req = McpRequestContext(
            user_id=user_id,
            agent_slug=slug,
            workspace=workspace,
            storage_root=self._storage_root,
            turn_id=turn_id or None,
            browser_sessions=self._browser_sessions,
        )
        token = current_mcp_request.set(req)

        # Rewrite the scope so the FastMCP route at ``/`` matches.
        # The inner Starlette computes ``route_path = path - root_path``.
        # We want the inner route_path to be exactly ``/``. Extend
        # ``root_path`` to consume the user/slug prefix and pad the
        # path with a trailing ``/`` if the client didn't send one.
        new_scope = dict(scope)
        consumed = scope.get("root_path", "") + f"/{user_id}/{slug}"
        new_scope["root_path"] = consumed
        # Ensure path ends with ``/`` so route_path resolves to ``/``.
        original_path = scope.get("path", "")
        inner_path = original_path if original_path.endswith("/") else original_path + "/"
        new_scope["path"] = inner_path
        if scope.get("raw_path") is not None:
            new_scope["raw_path"] = inner_path.encode()

        try:
            await self._inner_app(new_scope, receive, send)
        finally:
            current_mcp_request.reset(token)

    # ------------------------------------------------------------------
    # Mounting
    # ------------------------------------------------------------------

    def mount(self, app: FastAPI) -> None:
        """Attach the ASGI dispatcher to ``app`` under ``/mcp``."""
        app.router.routes.append(Mount(MCP_MOUNT_PATH, app=self.asgi))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auth_ok(self, scope: Scope, *, user_id: str, slug: str) -> bool:
        """Constant-time check of the per-(user_id, slug) derived token
        against either the ``Authorization: Bearer …`` header or the
        ``X-Telos-Mcp-Token`` header.

        The expected token is recomputed from the path the request
        actually targets (``user_id``/``slug`` arguments — already
        validated by the caller), so a token derived for one agent can
        never authorise a request against another agent's path.
        """
        if not self._master_secret:
            return False
        expected = derive_agent_token(self._master_secret, user_id, slug)
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for hk, hv in headers:
            if hk == _AUTH_HEADER_BEARER:
                # ``Bearer <token>`` — lowercase prefix tolerated for
                # the curl / httpx case where clients send ``bearer``.
                lowered = hv.strip()
                if lowered[:7].lower() == b"bearer ":
                    candidate = lowered[7:].decode("ascii", errors="ignore").strip()
                    if hmac.compare_digest(candidate, expected):
                        return True
            elif hk == _AUTH_HEADER_CUSTOM:
                candidate = hv.decode("ascii", errors="ignore").strip()
                if hmac.compare_digest(candidate, expected):
                    return True
        return False


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _read_secret_file(secret_path: Path) -> str | None:
    """Read a persisted master secret, enforcing owner-only permissions.

    Returns the stripped secret, or ``None`` if the file is missing or
    empty. A pre-existing file with group/other bits set (e.g. restored
    from a backup with default umask) is chmod'd back to 0600 — the
    secret signs every per-agent bearer token, so any wider read access
    hands out the whole MCP surface.
    """
    if not secret_path.is_file():
        return None
    mode = secret_path.stat().st_mode & 0o777
    if mode & 0o077:
        secret_path.chmod(0o600)
        log.warning(
            "tightened permissions on MCP master secret file %s from %03o to 600",
            secret_path, mode,
        )
    stored = secret_path.read_text(encoding="utf-8").strip()
    return stored or None


def resolve_auth_token(storage_root: Path | None = None) -> str:
    """Return the MCP MASTER secret.

    Resolution order:

    1. ``TELOS_MCP_AUTH_TOKEN`` env var, if set.
    2. ``<storage_root>/mcp_master_secret`` — read if present, else a
       fresh secret is generated, written there (0600), and returned.
       Persisting matters: the per-(user, slug) bearer tokens baked
       into Recordatio agent configs are HMAC-derived from this value
       (see :func:`derive_agent_token`), and the seed is
       create-if-missing — a master secret regenerated on every
       restart would strand every already-seeded config with stale
       tokens that 401 forever.
    3. No ``storage_root`` (tests / ad-hoc embedding): an ephemeral
       per-process secret, with a WARNING so the operator knows tokens
       won't survive a restart.

    We log only an 8-char SHA-256 fingerprint — never the raw value —
    because WARNING-level logs are kept for 30 days.
    """
    env = os.environ.get("TELOS_MCP_AUTH_TOKEN")
    if env:
        return env

    if storage_root is not None:
        secret_path = Path(storage_root) / MASTER_SECRET_FILENAME
        stored = _read_secret_file(secret_path)
        if stored:
            return stored
        if secret_path.is_file():
            # Exists but empty — a corrupt artifact (e.g. a crashed
            # write). Remove it so the O_EXCL create below can win.
            log.warning("removing empty MCP master secret file at %s", secret_path)
            secret_path.unlink(missing_ok=True)

        secret = secrets.token_urlsafe(32)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_CREAT|O_EXCL: exactly ONE process can create the file.
            # Without it, N workers racing on first boot would each keep
            # their own in-process secret (last write wins on disk) and
            # derived agent tokens would 401 on (N-1)/N of requests
            # until a restart. Mode 0600 is applied atomically at create.
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another worker won the race — adopt ITS secret. It may
            # still be mid-write (open succeeded, content not flushed),
            # so poll briefly rather than diverge.
            for _ in range(20):
                stored = _read_secret_file(secret_path)
                if stored:
                    return stored
                time.sleep(0.05)
            raise RuntimeError(
                f"MCP master secret file {secret_path} exists but stayed "
                "empty — cannot adopt the winning worker's secret",
            ) from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(secret)
        log.warning(
            "TELOS_MCP_AUTH_TOKEN unset — generated MCP master secret and "
            "persisted it to %s (secret_fp=%s) so derived agent tokens stay "
            "valid across restarts. Set TELOS_MCP_AUTH_TOKEN to manage the "
            "secret out-of-band.",
            secret_path,
            _redact_token(secret),
        )
        return secret

    token = secrets.token_urlsafe(32)
    log.warning(
        "TELOS_MCP_AUTH_TOKEN unset and no storage_root given — generated "
        "ephemeral MCP master secret for this process (secret_fp=%s). Derived "
        "agent tokens will NOT survive a restart.",
        _redact_token(token),
    )
    return token


def mount_mcp_host(
    app: FastAPI,
    *,
    tools: dict[str, Tool],
    storage_root: Path,
    auth_token: str | None = None,
    registry: AgentRegistry | None = None,
) -> TelosMcpHost:
    """Convenience wiring: build a :class:`TelosMcpHost`, mount it on
    ``app`` under ``/mcp``, and graft its lifespan onto the app's own.

    The host's ``run()`` context must be entered before the app accepts
    traffic; we layer it on top of any existing ``lifespan`` by
    wrapping the original. Returns the host so the caller can inspect
    or hold a reference.
    """
    host = TelosMcpHost(
        tools=tools,
        storage_root=Path(storage_root),
        auth_token=auth_token or resolve_auth_token(Path(storage_root)),
        registry=registry,
    )
    host.mount(app)

    # Compose lifespan: enter the host's lifespan during app startup,
    # exit on shutdown. We chain into whatever lifespan the app already
    # had (FastAPI keeps it in ``router.lifespan_context``).
    original_lifespan: Callable[[FastAPI], Any] | None = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _combined_lifespan(_app: FastAPI):
        async with contextlib.AsyncExitStack() as stack:
            if original_lifespan is not None:
                # Some FastAPI versions hand the app to the lifespan
                # callable directly; others expect a context manager.
                ctx = original_lifespan(_app)
                # FastAPI's router always wraps user lifespans in an
                # @asynccontextmanager so calling it returns a context
                # manager directly.
                await stack.enter_async_context(ctx)
            await stack.enter_async_context(host.lifespan())
            yield

    app.router.lifespan_context = _combined_lifespan  # type: ignore[assignment]
    return host


# ---------------------------------------------------------------------------
# Tiny ASGI helpers (kept inline so server.py is the single file you
# read to understand the dispatcher)
# ---------------------------------------------------------------------------


async def _send_text(send: Send, status: int, body: str) -> None:
    payload = body.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def _send_404(send: Send) -> None:
    await _send_text(send, 404, "not found")
