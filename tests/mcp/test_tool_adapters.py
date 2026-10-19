"""Unit tests for :mod:`telos.mcp.tool_adapters`.

The HTTP suite in :mod:`tests.mcp.test_host` covers the integrated
path. These tests pin a few invariants directly so a future FastMCP
upgrade that changes its internals fails fast and obviously:

- the JSON schema FastMCP publishes matches what the Pydantic input
  model would produce on its own (single source of truth);
- a tool that the adapter classifies as "downstream MCP" is *not*
  re-exported (we don't want to chain a client tool back out);
- the per-request ``ContextVar`` is read by the handler at call time
  (so a stale ``ToolContext`` from a previous request never leaks).
"""

from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp.types import CallToolResult

from telos.domain.tools import PythonExecTool, ShellExecTool
from telos.domain.tools.schema import input_class_of
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.mcp.tool_adapters import (
    McpRequestContext,
    current_mcp_request,
    register_tools,
)


def _server() -> FastMCP:
    return FastMCP(
        "test",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def test_register_tools_publishes_pydantic_schema():
    server = _server()
    sandbox = AsyncMock()
    tools = {"python_exec": PythonExecTool(sandbox)}
    names = register_tools(server, tools=tools)
    assert names == ["python_exec"]

    registered = server._tool_manager._tools["python_exec"]  # noqa: SLF001
    # The schema we publish should match the Pydantic input model's
    # JSON schema (minus the ``title``, like every other place in
    # telos that consumes Pydantic schemas).
    expected = input_class_of(PythonExecTool).model_json_schema()
    expected.pop("title", None)
    # Account for the ArgModelBase mix-in that the adapter applies —
    # it changes the model's title only, never its fields/required.
    assert registered.parameters["properties"] == expected["properties"]
    assert registered.parameters["required"] == expected["required"]


def test_register_tools_skips_downstream_mcp_tools():
    """A :class:`MCPTool` (proxy for a downstream MCP server's tool)
    must not be re-exported — that would chain telos's MCP host back
    into someone else's MCP server.
    """
    from telos.mcp.client import MCPTool, McpToolManager

    server = _server()
    sandbox = AsyncMock()
    mgr = McpToolManager()
    tools = {
        "python_exec": PythonExecTool(sandbox),
        "external_thing": MCPTool("external_thing", mgr),
    }
    names = register_tools(server, tools=tools)
    assert names == ["python_exec"]
    assert "external_thing" not in server._tool_manager._tools  # noqa: SLF001


@pytest.mark.asyncio
async def test_handler_reads_current_mcp_request(tmp_path):
    """The handler closure reads :data:`current_mcp_request` at call
    time, not at registration time — so a single FastMCP can serve
    many users by flipping the ContextVar before dispatch.
    """
    server = _server()
    sandbox = AsyncMock(return_value=("ok", "", 0))
    tools = {"shell_exec": ShellExecTool(sandbox)}
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["shell_exec"]  # noqa: SLF001

    workspace = tmp_path / "alice" / "agent-1" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    token = current_mcp_request.set(
        McpRequestContext(
            user_id="alice",
            agent_slug="agent-1",
            workspace=workspace,
            storage_root=tmp_path,
        ),
    )
    try:
        # Run the FastMCP tool through its public ``run`` method — same
        # codepath the streamable HTTP transport drives.
        await fmcp_tool.run({"command": "pwd"})
    finally:
        current_mcp_request.reset(token)

    assert sandbox.call_args.kwargs["cwd"] == workspace


@pytest.mark.asyncio
async def test_handler_raises_when_request_context_missing():
    """Defensive: invoking a registered tool outside any
    :data:`current_mcp_request` scope is a programming error — the
    handler raises rather than running with a stale/empty context.
    """
    server = _server()
    sandbox = AsyncMock()
    tools = {"python_exec": PythonExecTool(sandbox)}
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["python_exec"]  # noqa: SLF001

    # No current_mcp_request set.
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — message matters more than type
        await fmcp_tool.run({"code": "print(1)"})
    assert "per-request context" in str(excinfo.value)
    sandbox.assert_not_called()


def test_auth_token_defaults_to_generated_value(monkeypatch, caplog):
    """If no env var is set, ``resolve_auth_token`` returns a random
    URL-safe value and logs a warning so the operator can see it."""
    import logging

    from telos.mcp.server import resolve_auth_token

    monkeypatch.delenv("TELOS_MCP_AUTH_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING, logger="telos.mcp.server"):
        token1 = resolve_auth_token()
        token2 = resolve_auth_token()
    assert len(token1) >= 32
    assert token1 != token2  # genuinely random
    assert any("TELOS_MCP_AUTH_TOKEN unset" in rec.message for rec in caplog.records)


def test_auth_token_honors_env(monkeypatch):
    from telos.mcp.server import resolve_auth_token

    monkeypatch.setenv("TELOS_MCP_AUTH_TOKEN", "super-secret")
    assert resolve_auth_token() == "super-secret"


# ──────────────────────────────────────────────────────────────────────
# Housekeeper-only gating + wire-level error propagation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_housekeeper_only_tool_denied_for_specialist_caller(tmp_path):
    """A specialist agent invoking ``delegate_to`` over MCP must be
    refused with an ``isError=True`` result — only the housekeeper can
    delegate. Without this gate any caller could escalate by routing
    a delegation through the MCP host.
    """
    from telos.domain.tools.team import DelegateToTool

    server = _server()
    get_agent = AsyncMock()  # would be invoked if the gate failed
    tools = {"delegate_to": DelegateToTool(get_agent)}
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["delegate_to"]  # noqa: SLF001

    workspace = tmp_path / "alice" / "specialist" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    token = current_mcp_request.set(
        McpRequestContext(
            user_id="alice",
            agent_slug="weather",  # NOT the housekeeper
            workspace=workspace,
            storage_root=tmp_path,
        ),
    )
    try:
        result = await fmcp_tool.run(
            {"agent_slug": "other", "message": "do thing"},
        )
    finally:
        current_mcp_request.reset(token)

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    assert "restricted to the housekeeper" in text
    get_agent.assert_not_called()


@pytest.mark.asyncio
async def test_housekeeper_only_tool_allowed_for_housekeeper(tmp_path):
    """The housekeeper itself must still be able to invoke its own
    privileged tools over MCP — the gate is keyed on the caller slug.
    """
    from telos.domain.tools.team import DelegateToTool

    # Build a fake sibling agent whose astream yields one text chunk.
    async def _astream(_msg, _ctx):
        yield "text", "ok-from-sibling"

    sibling = type("Sib", (), {"astream": staticmethod(_astream)})()
    get_agent = AsyncMock(return_value=sibling)

    server = _server()
    tools = {"delegate_to": DelegateToTool(get_agent)}
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["delegate_to"]  # noqa: SLF001

    workspace = tmp_path / "alice" / HOUSEKEEPER_SLUG / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    token = current_mcp_request.set(
        McpRequestContext(
            user_id="alice",
            agent_slug=HOUSEKEEPER_SLUG,
            workspace=workspace,
            storage_root=tmp_path,
        ),
    )
    try:
        result = await fmcp_tool.run(
            {"agent_slug": "other", "message": "hello"},
        )
    finally:
        current_mcp_request.reset(token)

    # The handler returned a plain string (not a CallToolResult), which
    # FastMCP serialises as content with isError=False.
    if isinstance(result, CallToolResult):
        assert result.isError is not True
    get_agent.assert_called_once_with("alice", "other")


@pytest.mark.asyncio
async def test_dispatch_error_propagates_is_error_to_wire(tmp_path):
    """When ``_dispatch_tool_call`` reports ``is_error=True`` (e.g. the
    tool raised), the MCP handler must return a ``CallToolResult`` with
    ``isError=True`` — not a benign text payload that an MCP client
    will treat as a successful run.
    """
    server = _server()
    # Sandbox raises — _dispatch_tool_call catches and reports is_error=True.
    sandbox = AsyncMock(side_effect=RuntimeError("kaboom"))
    tools = {"python_exec": PythonExecTool(sandbox)}
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["python_exec"]  # noqa: SLF001

    workspace = tmp_path / "alice" / "weather" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    token = current_mcp_request.set(
        McpRequestContext(
            user_id="alice",
            agent_slug="weather",
            workspace=workspace,
            storage_root=tmp_path,
        ),
    )
    try:
        result = await fmcp_tool.run({"code": "print(1)"})
    finally:
        current_mcp_request.reset(token)

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    assert "kaboom" in text or "failed" in text.lower()


# ──────────────────────────────────────────────────────────────────────
# Routine tools over the MCP path — scheduler wiring
# ──────────────────────────────────────────────────────────────────────
#
# Regression tests for the post-Phase-6 prod bug: the MCP host's tool
# catalogue was built with ``routine_runner=None``, so routine writes
# over MCP mutated the store but never APScheduler — deleted routines
# kept firing, newly activated ones never started until a restart. The
# tools now take a late-binding runner *provider* resolved per call.


def _routine(**over):
    from types import SimpleNamespace

    defaults = {
        "id": "8f0820501efd",
        "user_id": "alice",
        "agent_slug": "weather",
        "description": "Daily news",
        "interval_seconds": 86_400,
        "time_of_day": None,
        "one_shot": False,
        "enabled": False,
        "last_run": None,
    }
    defaults.update(over)
    return SimpleNamespace(**defaults)


def _request_ctx(tmp_path, *, slug: str = "weather"):
    workspace = tmp_path / "alice" / slug / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return McpRequestContext(
        user_id="alice",
        agent_slug=slug,
        workspace=workspace,
        storage_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_create_routine_over_mcp_registers_enabled_routine_with_live_runner(tmp_path):
    """create_routine invoked through the MCP handler schedules an
    enabled routine on the runner the provider resolves AT CALL TIME —
    i.e. the one the FastAPI lifespan installed after the catalogue was
    built."""
    from unittest.mock import MagicMock

    from telos.domain.tools.routines import CreateRoutineTool

    enabled = _routine(enabled=True)
    store = MagicMock()
    store.create = MagicMock(return_value=enabled)
    llm = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})

    # Simulate the lifespan ordering: no runner exists when the tool is
    # constructed; the provider picks up the late-bound instance.
    runner_holder = {"runner": None}
    tools = {"create_routine": CreateRoutineTool(store, llm, lambda: runner_holder["runner"])}

    server = _server()
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["create_routine"]  # noqa: SLF001

    live_runner = MagicMock()
    runner_holder["runner"] = live_runner  # lifespan starts the runner

    token = current_mcp_request.set(_request_ctx(tmp_path))
    try:
        result = await fmcp_tool.run(
            {"description": "Daily news", "interval_seconds": 86_400},
        )
    finally:
        current_mcp_request.reset(token)

    if isinstance(result, CallToolResult):
        assert result.isError is not True
    live_runner.add.assert_called_once_with(enabled)


@pytest.mark.asyncio
async def test_delete_routine_over_mcp_unschedules_job(tmp_path):
    """delete_routine over MCP removes the APScheduler job AND the row."""
    from unittest.mock import MagicMock

    from telos.domain.tools.routines import DeleteRoutineTool

    store = MagicMock()
    store.get = MagicMock(return_value=_routine())
    live_runner = MagicMock()
    tools = {"delete_routine": DeleteRoutineTool(store, lambda: live_runner)}

    server = _server()
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["delete_routine"]  # noqa: SLF001

    token = current_mcp_request.set(_request_ctx(tmp_path))
    try:
        result = await fmcp_tool.run({"routine_id": "8f0820501efd"})
    finally:
        current_mcp_request.reset(token)

    if isinstance(result, CallToolResult):
        assert result.isError is not True
    live_runner.remove.assert_called_once_with("8f0820501efd")
    store.delete.assert_called_once_with("8f0820501efd")


@pytest.mark.asyncio
async def test_delete_routine_over_mcp_unknown_id_is_wire_level_error(tmp_path):
    """An id that doesn't exist (e.g. an invented numeric id) surfaces
    as ``isError=True`` on the wire and leaves the store untouched."""
    from unittest.mock import MagicMock

    from telos.domain.tools.routines import DeleteRoutineTool

    store = MagicMock()
    store.get = MagicMock(return_value=None)
    live_runner = MagicMock()
    tools = {"delete_routine": DeleteRoutineTool(store, lambda: live_runner)}

    server = _server()
    register_tools(server, tools=tools)
    fmcp_tool = server._tool_manager._tools["delete_routine"]  # noqa: SLF001

    token = current_mcp_request.set(_request_ctx(tmp_path))
    try:
        result = await fmcp_tool.run({"routine_id": "157396451090429"})
    finally:
        current_mcp_request.reset(token)

    assert isinstance(result, CallToolResult)
    assert result.isError is True
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    assert "157396451090429" in text
    store.delete.assert_not_called()
    live_runner.remove.assert_not_called()
