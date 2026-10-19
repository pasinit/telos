"""Telos MCP module — both client and host.

Two responsibilities live here:

- **Client** (existing) — :class:`McpToolManager` and :class:`MCPTool`
  let a Telos agent connect *out* to third-party MCP servers and use
  their tools. Implementation: :mod:`telos.mcp.client`.
- **Host** (Phase 2 migration) — :class:`TelosMcpHost` exposes Telos's
  *own* tools over HTTP so Recordatio can call them remotely. Mounted
  under ``/mcp/{user_id}/{slug}`` on the FastAPI app. Implementation:
  :mod:`telos.mcp.server` and :mod:`telos.mcp.tool_adapters`.

Only the client surface is re-exported here so importing
:mod:`telos.mcp` from low layers (e.g. :mod:`telos.agent`) doesn't pull
in the host module — the host imports ``telos.agent._dispatch_tool_call``
and the resulting cycle would break startup. The host is reached via
its fully-qualified module path:

    from telos.mcp.server import TelosMcpHost, mount_mcp_host
"""

from telos.mcp.client import MCPInput, MCPTool, McpToolManager

__all__ = ["MCPInput", "MCPTool", "McpToolManager"]
