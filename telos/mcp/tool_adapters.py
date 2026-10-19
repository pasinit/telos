"""Adapt Telos's existing tools into MCP tool registrations.

For each :class:`telos.core.ports.Tool` in the wired ``dict[str, Tool]``
the agent uses, we register one FastMCP tool with:

- the same name,
- the same description,
- an input schema derived from the same Pydantic input model the OpenAI
  function-call schema is derived from (single source of truth),
- a handler that calls :func:`telos.agent._dispatch_tool_call` so the
  underlying tool implementation runs through *exactly* the same
  dispatch path as in-process tool calls — only the entry point
  changes.

Per-(user_id, slug) request context is read off a :class:`ContextVar`
that the ASGI dispatcher in :mod:`telos.mcp.server` sets per request.
This lets one FastMCP instance serve any user — there is no per-user
FastMCP cache, and tool wiring is built once.
"""

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool as FastMcpTool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpWireTool
from pydantic import create_model

from telos.agent import _dispatch_tool_call
from telos.core.delegation import DelegationGraph
from telos.core.ports import AgentRegistry, Tool
from telos.domain.tools.schema import input_class_of
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.tools import tool_repository

log = logging.getLogger("telos.mcp.adapters")


def _housekeeper_only_names() -> set[str]:
    """Names of tools the catalogue marks ``housekeeper_only=True``.

    Sourced from :data:`telos.tools.tool_repository` so the MCP gate
    shares the same single source of truth used by
    :meth:`ToolRepository.get_schemas` and the in-process agent path.
    """
    return {
        entry.name
        for entry in tool_repository.list_all()
        if entry.housekeeper_only
    }


def _error_result(text: str) -> CallToolResult:
    """Build a wire-level error result.

    Returning a :class:`CallToolResult` with ``isError=True`` is the
    idiomatic FastMCP error shape — the lowlevel server passes it
    through verbatim (see ``mcp.server.lowlevel.server`` ``call_tool``
    handler), so MCP clients see ``isError: true`` rather than a
    benign-looking text result.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=True,
    )


@dataclass(frozen=True)
class McpRequestContext:
    """Per-request context the ASGI dispatcher attaches to every tool
    call. The MCP tool handler reads this to build a fresh
    :class:`telos.domain.tools.ToolContext` bound to the right user and
    storage path.
    """

    user_id: str
    agent_slug: str
    workspace: Path
    storage_root: Path
    turn_id: str | None = None
    browser_sessions: Any = None


# ContextVar set by ``telos.mcp.server`` *before* dispatching a request
# into the FastMCP handler. Each MCP tool handler reads it to know which
# user/slug it is running for. Per-request and async-safe.
current_mcp_request: ContextVar[McpRequestContext | None] = ContextVar(
    "current_mcp_request", default=None,
)


def _make_handler(
    name: str,
    tools: dict[str, Tool],
    *,
    housekeeper_only: bool,
    registry: "AgentRegistry | None" = None,
):
    """Build the async handler FastMCP calls when this tool is invoked.

    The closure captures ``name`` and ``tools``; per-request state
    (user_id / slug / workspace) is read off
    :data:`current_mcp_request`. The handler routes through
    :func:`telos.agent._dispatch_tool_call` so the same validation,
    error handling, and observability spans fire on the MCP path as in
    the in-process path.

    When ``housekeeper_only`` is True and the caller's ``agent_slug``
    isn't the housekeeper's, the handler short-circuits with a
    permission-denied :class:`CallToolResult` (``isError=True``) so
    specialist agents can't escalate by invoking ``delegate_to`` or
    ``create_agent`` over MCP.
    """

    async def _handle(**kwargs: Any) -> Any:
        req = current_mcp_request.get()
        if req is None:
            # Defensive — the ASGI dispatcher must always set this.
            raise RuntimeError(
                "MCP tool invoked outside of a per-request context — "
                "telos.mcp.server failed to set current_mcp_request.",
            )

        call_id = uuid4().hex

        def record(kind, payload):
            if req.turn_id:
                from telos.log import ConversationLog
                try:
                    history = req.storage_root / req.user_id / req.agent_slug / "history.db"
                    ConversationLog(history).execution_events.append(req.turn_id, kind, {"call_id": call_id, "name": name, **payload})
                except Exception:
                    log.exception("could not persist MCP execution evidence")

        record("tool_call", {"input": kwargs})

        def denied(message):
            record("tool_result", {"result": message, "is_error": True, "image_count": 0})
            return _error_result(message)

        # Permission gate: housekeeper-only tools (delegate_to,
        # create_agent, …) refuse calls from any caller whose slug
        # isn't the reserved housekeeper slug. The in-process path
        # filters these out at schema-build time via
        # ``ToolRepository.get_schemas(include_housekeeper=...)``;
        # without this gate the MCP surface would let any caller
        # invoke them.
        if housekeeper_only and req.agent_slug != HOUSEKEEPER_SLUG:
            log.warning(
                "denied housekeeper-only tool %s for caller slug=%s user=%s",
                name, req.agent_slug, req.user_id,
            )
            return denied(
                f"Tool '{name}' is restricted to the housekeeper agent; "
                f"caller slug '{req.agent_slug}' is not authorised.",
            )

        # Per-agent authorization: a specialist may only call tools in its
        # own registry allowlist. telos exposes the tool, so telos enforces
        # ownership at its own boundary here — the SDK-side ``allowed_tools``
        # is defence-in-depth, not the authority. The housekeeper keeps the
        # full set; no registry (embedders/tests) ⇒ skip and rely on the
        # housekeeper-only gate above (matches the pre-enforcement behaviour).
        if registry is not None and req.agent_slug != HOUSEKEEPER_SLUG:
            allowed = scoped_tool_names(
                user_id=req.user_id,
                slug=req.agent_slug,
                registered=set(tools),
                registry=registry,
            )
            if name not in allowed:
                log.warning(
                    "denied tool %s for %s/%s — not in the agent's allowlist",
                    name, req.agent_slug, req.user_id,
                )
                return denied(
                    f"Tool '{name}' is not in agent '{req.agent_slug}'s "
                    f"allowlist; it is not authorised to call it.",
                )

        async def dispatch(browser=None):
            return await _dispatch_tool_call(
                name, kwargs, tools, user_id=req.user_id, agent_slug=req.agent_slug,
                workspace=req.workspace, storage_root=req.storage_root,
                delegation_graph=DelegationGraph(), browser_session=browser,
            )

        try:
            if name.startswith("browser_"):
                if not req.turn_id or req.browser_sessions is None:
                    text, is_error, images = "Browser tool requires a scoped turn header; restart the agent turn", True, None
                else:
                    async with req.browser_sessions.session((req.user_id, req.agent_slug, req.turn_id)) as browser:
                        text, is_error, images = await dispatch(browser)
            else:
                text, is_error, images = await dispatch()
        except Exception as exc:
            text, is_error, images = f"{type(exc).__name__}: {exc}", True, None
        record("tool_result", {"result": text, "is_error": is_error, "image_count": len(images or [])})
        if is_error:
            return _error_result(text)
        if images:
            content = [TextContent(type="text", text=text)]
            for uri in images:
                header, sep, data = uri.partition(",")
                if sep and header.startswith("data:image/") and header.endswith(";base64"):
                    content.append(ImageContent(type="image", data=data, mimeType=header[5:-7]))
            return CallToolResult(content=content, isError=False)
        return text

    return _handle


def _arg_model_for(tool: Tool) -> type[ArgModelBase]:
    """Wrap the tool's Pydantic input model into FastMCP's ArgModelBase.

    FastMCP's :class:`FuncMetadata` expects an :class:`ArgModelBase`
    subclass (it relies on ``model_dump_one_level()``). Telos's tools
    declare plain :class:`pydantic.BaseModel` subclasses. We bridge with
    :func:`pydantic.create_model`, inheriting both — the new model
    keeps every field declaration, validation rule, and JSON schema
    output of the original.
    """
    InputModel = input_class_of(type(tool))
    return create_model(
        f"{InputModel.__name__}_McpArgs",
        __base__=(InputModel, ArgModelBase),
    )


def register_tools(
    server: FastMCP,
    *,
    tools: dict[str, Tool],
    registry: "AgentRegistry | None" = None,
) -> list[str]:
    """Register every wired tool with the FastMCP server.

    Returns the list of registered tool names in iteration order.

    Tools that came from a downstream MCP server (:class:`MCPTool`) are
    skipped: the host's purpose is to surface *Telos's own* tools, not
    to re-export a chain of third-party MCP servers.

    When ``registry`` is provided, each tool's invocation handler enforces
    the calling agent's per-agent allowlist (see :func:`_make_handler`) — so
    a specialist can only call the tools its registry row grants, enforced at
    telos's own MCP boundary rather than delegated to Recordatio's SDK config.
    """
    # Import here to avoid a top-of-module cycle (client imports
    # tool_repository which imports the rest of telos).
    from telos.mcp.client import MCPTool as _MCPTool

    hk_only = _housekeeper_only_names()

    registered: list[str] = []
    for name, tool in tools.items():
        if isinstance(tool, _MCPTool):
            continue

        try:
            ArgModel = _arg_model_for(tool)
        except TypeError:
            log.warning(
                "skipping tool %s — input model is not a Pydantic BaseModel", name,
            )
            continue

        description = getattr(tool, "description", "") or name
        handler = _make_handler(
            name, tools, housekeeper_only=name in hk_only, registry=registry,
        )
        handler.__name__ = name
        handler.__doc__ = description

        params_schema = ArgModel.model_json_schema()
        params_schema.pop("title", None)

        fmcp_tool = FastMcpTool(
            fn=handler,
            name=name,
            title=None,
            description=description,
            parameters=params_schema,
            fn_metadata=FuncMetadata(arg_model=ArgModel),
            is_async=True,
            context_kwarg=None,
            annotations=None,
        )
        # FastMCP's public ``add_tool`` rebuilds metadata from the
        # callable's type hints; that would lose our Pydantic schema.
        # We pre-built a fully-formed FastMcpTool, so we register it
        # directly on the manager.
        server._tool_manager._tools[name] = fmcp_tool  # noqa: SLF001
        registered.append(name)

    log.info("registered %d telos tools with MCP host", len(registered))
    return registered


def scoped_tool_names(
    *,
    user_id: str,
    slug: str,
    registered: set[str],
    registry: AgentRegistry | None,
) -> set[str]:
    """Tool names the ``(user_id, slug)`` surface should ADVERTISE.

    Listing-side counterpart of the invocation gates: the SDK-side
    ``allowed_tools`` whitelist (plus the housekeeper-only handler gate
    above) already controls what can be CALLED; this narrows what is
    *seen* on ``tools/list`` so an agent's model only discovers the
    tools it owns — defence-in-depth plus noise reduction.

    Rules:

    - The housekeeper keeps the full registered set (including the
      housekeeper-only delegation tools) — its surface is unchanged.
    - Any other slug starts from ``registered`` minus the
      housekeeper-only names (those would be denied at invocation
      anyway), then — when a ``registry`` is wired — intersects with
      the agent's own ``tools`` whitelist from its registry row
      (``registry.db`` ``agents.tools`` JSON, the same column the
      Recordatio seed reads in
      :func:`telos.services.recordatio_agent_seed.build_agent_config_payload`).
    - Unknown agent (no registry row) or an unparseable ``tools``
      column fails CLOSED to the empty set, matching the host's
      fail-closed philosophy (cf. ``_auth_ok`` without a master
      secret, the housekeeper-only handler gate). No new HTTP
      semantics — the surface simply advertises nothing.
    - ``registry is None`` (embedders/tests that mount the host
      without one) keeps the pre-scoping behaviour, minus the
      housekeeper-only names.
    """
    if slug == HOUSEKEEPER_SLUG:
        return set(registered)

    allowed = set(registered) - _housekeeper_only_names()
    if registry is None:
        return allowed

    row = registry.get(user_id, slug)
    if row is None:
        log.warning(
            "tools/list for unknown agent %s/%s — advertising no tools",
            user_id, slug,
        )
        return set()

    tools_raw = row.get("tools")
    try:
        own = json.loads(tools_raw) if isinstance(tools_raw, str) else (tools_raw or [])
    except json.JSONDecodeError:
        log.warning(
            "agent %s/%s has unparseable tools=%r — advertising no tools",
            user_id, slug, tools_raw,
        )
        return set()
    return allowed & set(own)


def install_scoped_tool_listing(
    server: FastMCP,
    *,
    registered: list[str],
    registry: AgentRegistry | None,
) -> None:
    """Replace FastMCP's ``tools/list`` handler with a per-request one.

    FastMCP registers its own ``list_tools`` (the full tool-manager
    contents) at construction; re-registering on the lowlevel server
    swaps the ``ListToolsRequest`` handler for ours, which filters the
    full set down to :func:`scoped_tool_names` for the requesting
    ``(user_id, slug)`` read off :data:`current_mcp_request`.

    Invocation is untouched: ``tools/call`` still resolves against the
    full tool manager, gated by the existing housekeeper-only check in
    :func:`_make_handler` and the SDK-side ``allowed_tools`` whitelist.
    (The lowlevel server's listing-derived ``_tool_cache`` only feeds
    optional input validation, which FastMCP disables — see
    ``FastMCP._setup_handlers`` — so a scoped listing cannot block or
    weaken calls.)
    """
    registered_set = set(registered)

    async def _scoped_list_tools() -> list[McpWireTool]:
        all_tools = await FastMCP.list_tools(server)
        req = current_mcp_request.get()
        if req is None:
            # Outside a per-request context (e.g. the lowlevel server's
            # internal cache refresh at startup) there is no caller to
            # scope to — return the full set, as before.
            return all_tools
        allowed = scoped_tool_names(
            user_id=req.user_id,
            slug=req.agent_slug,
            registered=registered_set,
            registry=registry,
        )
        return [t for t in all_tools if t.name in allowed]

    server._mcp_server.list_tools()(_scoped_list_tools)  # noqa: SLF001


__all__ = [
    "McpRequestContext",
    "current_mcp_request",
    "install_scoped_tool_listing",
    "register_tools",
    "scoped_tool_names",
]
