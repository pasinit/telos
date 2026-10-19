"""Seed Recordatio's AgentConfig from telos's local registry on first build.

When an Agent is built for the first time for a given ``(user_id,
slug)``, telos checks whether Recordatio already has an AgentConfig for
that pair and, if not, builds one from the registry row and PUTs it.

The mapping is intentionally minimal — Recordatio's AgentConfig is the
source of truth once seeded; the improver (Phase 5) is the writer for
subsequent updates. We never overwrite an existing config's *content*
from this path — with one deliberate exception: the telos-server
``Authorization`` header. That header is HMAC-derived from the MCP
master secret (see :func:`telos.mcp.server.derive_agent_token`), so a
config seeded before the per-(user, slug) token binding shipped — or
after a master-secret rotation — carries a stale token that 401s on
every MCP call forever. On every build we therefore compare the stored
header against the freshly derived value and PUT a header-only refresh
on mismatch, preserving every other field verbatim.

For the housekeeper specifically, the AgentConfig's ``subagents`` field
is populated from the user's other registry rows (i.e. ``list_specialists``).
Each becomes one SDK ``AgentDefinition`` — name, description, prompt,
tools — so the Recordatio-side SDK session can delegate to them via native
subagent semantics (replacing the legacy in-process ``DelegationGraph``
once Phase 6 retires it).
"""
import copy
import json
import logging
from typing import Any

import httpx

from telos.core.repos.registry import RegistryRepo
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.mcp.server import derive_agent_token
from telos.transport.llm.recordatio_agent import get_agent_config, put_agent_config

log = logging.getLogger("telos.services.recordatio_agent_seed")


def _registry_row_to_subagent(row: dict[str, Any]) -> dict[str, Any]:
    """Translate one specialist registry row → one subagent dict.

    The shape matches Recordatio's ``AgentConfig.subagents`` element schema:
    ``{name, description, prompt, tools}`` where ``tools`` is the JSON
    array stored in the registry (parsed) or ``None`` if absent.

    Tool names get the same ``mcp__telos__<name>`` rebadging as the
    top-level ``allowed_tools`` (see :func:`_rebadge_tools`) — Recordatio's
    ``AgentDefinition`` matches SDK-side names, so bare registry names
    would resolve to nothing and the subagent would have zero callable
    tools.
    """
    tools_raw = row.get("tools")
    tools: list[str] | None
    if tools_raw is None or tools_raw == "":
        tools = None
    else:
        try:
            parsed = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
        except json.JSONDecodeError:
            log.warning(
                "specialist %s/%s has unparseable tools=%r; sending None",
                row.get("user_id"), row.get("slug"), tools_raw,
            )
            parsed = None
        tools = _rebadge_tools(list(parsed)) if parsed is not None else None

    return {
        "name": row["slug"],
        "description": row.get("description") or "",
        "prompt": row.get("system_prompt") or "",
        "tools": tools,
    }


# Name of the telos MCP server as Recordatio / the Claude Agent SDK will see it.
# The SDK addresses MCP tools as ``mcp__<server>__<tool>`` (see Anthropic's
# Claude Code MCP convention), so this constant pins the prefix used in
# ``allowed_tools`` below.
TELOS_MCP_SERVER_NAME = "telos"


def _telos_mcp_tool_name(tool: str) -> str:
    """Translate a telos-side tool name into the MCP-prefixed name the SDK
    expects in ``allowed_tools``.

    Telos-side names ending up in registry rows (e.g. ``web_search``,
    ``create_routine``, ``delegate_to``) are exposed to Recordatio via the
    telos MCP host and surface as ``mcp__telos__<name>`` once the Claude
    Agent SDK resolves them. Bare names (no prefix) would never match and
    the housekeeper would silently end up with zero callable tools.
    """
    return f"mcp__{TELOS_MCP_SERVER_NAME}__{tool}"


# SDK-native tool names (Claude Agent SDK built-ins). When telos grants one
# of these to an agent it means the SDK's OWN tool, which does not route
# through telos's MCP host — so it must pass through verbatim rather than be
# prefixed to a ``mcp__telos__<Name>`` that no server can serve. telos's own
# tools are all snake_case (see ``tool_repository``), so there is no collision
# with these PascalCase names.
_SDK_NATIVE_TOOLS = frozenset({
    "Bash", "BashOutput", "KillShell",
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep",
    "WebFetch", "WebSearch",
    "TodoWrite", "Task", "ExitPlanMode",
})


def _rebadge_tools(tools: list[str]) -> list[str]:
    """Rebadge bare telos tool names into the ``mcp__telos__<name>`` form the
    SDK matches against. Two kinds of name pass through unchanged: names
    already MCP-prefixed, and SDK-native built-ins (``Bash``, ``Read``, …) —
    the latter are the SDK's own tools, not telos MCP tools, so prefixing them
    would point at a server that can't serve them.

    Shared by the top-level ``allowed_tools`` and each subagent's ``tools`` so
    both resolve against the same telos MCP server (and the same native set).
    """
    return [
        t if (t.startswith("mcp__") or t in _SDK_NATIVE_TOOLS) else _telos_mcp_tool_name(t)
        for t in tools
    ]


def _build_mcp_servers(
    *,
    user_id: str,
    slug: str,
    telos_mcp_url: str,
    telos_mcp_auth_token: str | None,
) -> list[dict[str, Any]]:
    """Build the ``mcp_servers`` list pointing Recordatio at telos's MCP host.

    The telos MCP host mounts under ``/mcp/{user_id}/{slug}`` (see
    :mod:`telos.mcp.server`); each agent gets its own subtree so per-call
    auth + workspace are bound by the URL path.
    """
    url = f"{telos_mcp_url.rstrip('/')}/mcp/{user_id}/{slug}"
    headers: dict[str, str] = {}
    if telos_mcp_auth_token:
        # ``telos_mcp_auth_token`` is the MASTER secret; the header gets
        # the per-(user, slug) HMAC-derived token so this config can only
        # reach its own ``/mcp/{user_id}/{slug}`` surface (matches
        # ``TelosMcpHost._auth_ok``).
        agent_token = derive_agent_token(telos_mcp_auth_token, user_id, slug)
        headers["Authorization"] = f"Bearer {agent_token}"
    server: dict[str, Any] = {
        "name": TELOS_MCP_SERVER_NAME,
        "type": "http",
        "url": url,
    }
    if headers:
        server["headers"] = headers
    return [server]


def build_agent_config_payload(
    *,
    registry: RegistryRepo,
    user_id: str,
    slug: str,
    model: str | None,
    telos_mcp_url: str,
    telos_mcp_auth_token: str | None,
) -> dict[str, Any] | None:
    """Construct the AgentConfig payload from telos's registry row.

    Returns ``None`` if the registry has no row for (user_id, slug)
    (nothing to seed). For the housekeeper, populates ``subagents`` from
    the user's specialist list; for specialists, leaves it empty.

    ``telos_mcp_url`` must be the externally-reachable base URL of the
    telos process (e.g. ``https://telos.example.com``); we append the
    per-agent ``/mcp/{user_id}/{slug}`` path. Raises ``RuntimeError`` if
    it's empty — silently seeding an empty ``mcp_servers`` would leave
    the agent with ``allowed_tools`` names that resolve to no server.
    """
    if not telos_mcp_url:
        raise RuntimeError(
            "cannot seed recordatio config: telos MCP URL unknown — set "
            "AgentRuntime(telos_mcp_url=...) or the TELOS_PUBLIC_URL env var",
        )
    row = registry.get(user_id, slug)
    if row is None:
        return None

    subagents: list[dict[str, Any]] = []
    if slug == HOUSEKEEPER_SLUG:
        for specialist in registry.list_specialists(user_id):
            subagents.append(_registry_row_to_subagent(specialist))

    tools_raw = row.get("tools")
    try:
        raw_tools = json.loads(tools_raw) if tools_raw else []
    except json.JSONDecodeError:
        raw_tools = []
    # Rebadge bare telos tool names into the ``mcp__telos__<name>`` form
    # the SDK matches against (see ``_rebadge_tools``).
    allowed_tools = _rebadge_tools(raw_tools)

    mcp_servers = _build_mcp_servers(
        user_id=user_id, slug=slug,
        telos_mcp_url=telos_mcp_url,
        telos_mcp_auth_token=telos_mcp_auth_token,
    )

    return {
        "system_prompt": row.get("system_prompt") or "",
        "mcp_servers": mcp_servers,
        "subagents": subagents,
        "model": model or "",
        "allowed_tools": allowed_tools,
        "version": 1,        # ignored on insert; meaningful only for subsequent PUTs
    }


def _with_refreshed_auth_header(
    config: dict[str, Any],
    *,
    expected_header: str,
) -> dict[str, Any] | None:
    """Return a deep copy of ``config`` with the telos-server
    ``Authorization`` header set to ``expected_header``, or ``None`` if
    the stored header already matches (nothing to refresh) or there is
    no telos server entry to fix.

    Every field other than that one header is preserved verbatim —
    including ``version``, which Recordatio's config store uses for its
    optimistic-concurrency check on PUT.
    """
    servers = config.get("mcp_servers") or []
    target = next(
        (s for s in servers if isinstance(s, dict) and s.get("name") == TELOS_MCP_SERVER_NAME),
        None,
    )
    if target is None:
        # No telos server entry → no stale telos token to fix. The
        # improver owns config shape; don't invent an entry here.
        return None
    stored = (target.get("headers") or {}).get("Authorization")
    if stored == expected_header:
        return None

    updated = copy.deepcopy(config)
    for server in updated["mcp_servers"]:
        if isinstance(server, dict) and server.get("name") == TELOS_MCP_SERVER_NAME:
            headers = dict(server.get("headers") or {})
            headers["Authorization"] = expected_header
            server["headers"] = headers
    return updated


async def _refresh_auth_header_if_stale(
    client: httpx.AsyncClient,
    *,
    existing: dict[str, Any],
    user_id: str,
    slug: str,
    telos_mcp_auth_token: str | None,
) -> bool:
    """Migration path for already-seeded configs (PR #162 review, P0).

    Configs seeded before per-(user, slug) token binding carry the OLD
    global token in their telos-server ``Authorization`` header; the MCP
    host now 401s those forever. Compare the stored header against the
    freshly derived token and PUT a header-only refresh on mismatch —
    this also re-heals configs after a master-secret rotation.

    Returns ``True`` if a refresh PUT was issued. On a 409 (someone —
    e.g. the improver — bumped the version between our GET and PUT),
    re-GETs the current config and retries exactly once.
    """
    if not telos_mcp_auth_token:
        # No master secret → nothing to derive or compare against.
        return False
    expected_header = (
        f"Bearer {derive_agent_token(telos_mcp_auth_token, user_id, slug)}"
    )

    config = existing
    for attempt in (1, 2):
        updated = _with_refreshed_auth_header(config, expected_header=expected_header)
        if updated is None:
            return False
        try:
            await put_agent_config(client, user_id=user_id, slug=slug, config=updated)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409 or attempt == 2:
                raise
            refetched = await get_agent_config(client, user_id=user_id, slug=slug)
            if refetched is None:
                # Config vanished between GET and PUT; the create path
                # will reseed it on the next build.
                return False
            config = refetched
            continue
        log.info(
            "refreshed stale telos MCP auth header in recordatio config for %s/%s",
            user_id, slug,
        )
        return True
    return False  # pragma: no cover — loop always returns/raises above


async def seed_agent_config_if_missing(
    client: httpx.AsyncClient,
    *,
    registry: RegistryRepo,
    user_id: str,
    slug: str,
    model: str | None,
    telos_mcp_url: str,
    telos_mcp_auth_token: str | None,
) -> bool:
    """Idempotently seed Recordatio's AgentConfig for (user_id, slug).

    Returns ``True`` if a new config was created, ``False`` if one
    already existed (even if its stale telos auth header got refreshed —
    see :func:`_refresh_auth_header_if_stale`) or there was nothing in
    the registry to seed from.

    Any HTTP error other than 404-on-GET is surfaced to the caller; the
    seed step shouldn't silently fail because subsequent chat turns
    would then run with whatever empty/stale config Recordatio happens to
    have. One exception: a 409 on the PUT after our GET saw a 404 means
    a concurrent worker won the first-seed race — the config exists now,
    which is exactly the state this function exists to reach, so we
    re-GET to confirm and return ``False`` (already existed) instead of
    failing closed.
    """
    existing = await get_agent_config(client, user_id=user_id, slug=slug)
    if existing is not None:
        refreshed = await _refresh_auth_header_if_stale(
            client, existing=existing, user_id=user_id, slug=slug,
            telos_mcp_auth_token=telos_mcp_auth_token,
        )
        if not refreshed:
            log.debug(
                "recordatio config already present for %s/%s; skip seed", user_id, slug,
            )
        return False

    payload = build_agent_config_payload(
        registry=registry, user_id=user_id, slug=slug, model=model,
        telos_mcp_url=telos_mcp_url,
        telos_mcp_auth_token=telos_mcp_auth_token,
    )
    if payload is None:
        log.warning(
            "registry has no row for %s/%s; cannot seed recordatio config",
            user_id, slug,
        )
        return False

    try:
        await put_agent_config(client, user_id=user_id, slug=slug, config=payload)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 409:
            raise
        # 409 on a create-if-missing PUT: someone else seeded between our
        # GET-404 and our PUT (two workers cold-starting the same agent).
        # That's success-after-race, not a config error. Re-GET to confirm
        # the config really is there; if it somehow isn't, this was a
        # genuine version conflict — surface it.
        existing = await get_agent_config(client, user_id=user_id, slug=slug)
        if existing is None:
            raise
        log.info(
            "recordatio config seed race for %s/%s: concurrent worker seeded "
            "first; using the existing config",
            user_id, slug,
        )
        return False
    log.info(
        "seeded recordatio agent config for %s/%s (subagents=%d)",
        user_id, slug, len(payload["subagents"]),
    )
    return True
