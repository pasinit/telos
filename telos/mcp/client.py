"""MCP tool integration for Telos agents via toolregistry."""
import asyncio
import logging

from pydantic import BaseModel, ConfigDict
from toolregistry import ToolRegistry

from telos.domain.tools.context import ToolContext, ToolResult
from telos.tools import tool_repository

log = logging.getLogger("telos.mcp")


def _sanitize_namespace(name: str) -> str:
    """Convert a server name like 'com.example/my-server' to a valid namespace."""
    return name.replace("/", "_").replace(".", "_").replace("-", "_")


class McpToolManager:
    """Manages MCP server connections and tool execution via toolregistry."""

    def __init__(self, workspace: str | None = None) -> None:
        self._registry = ToolRegistry()
        self._connected: set[str] = set()
        self._workspace = workspace
        # Map our tool names (mcp:server/name) -> toolregistry internal names
        self._tool_name_map: dict[str, str] = {}

    async def connect_tool(self, tool_name: str) -> None:
        """Connect an MCP tool by its Telos name (e.g. 'mcp:filesystem').

        Resolves the transport from KNOWN_SERVERS and registers the server.
        """
        if tool_name in self._connected:
            return

        if not tool_name.startswith("mcp:"):
            return

        server_name = tool_name[4:]  # strip "mcp:" prefix
        namespace = _sanitize_namespace(server_name)

        transport = tool_repository.get_transport(tool_name, workspace=self._workspace)
        if transport is None:
            log.warning("unknown MCP server %s — skipping", server_name)
            return

        log.info("connecting MCP server: %s (namespace=%s)", server_name, namespace)
        tools_before = set(self._registry.list_tools())

        async def _do_connect():
            await self._registry.register_from_mcp_async(
                transport=transport,
                namespace=namespace,
                persistent=True,
            )

        task = asyncio.create_task(_do_connect())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except (TimeoutError, BaseException) as e:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            log.warning("failed to connect MCP server %s: %s", server_name, type(e).__name__)
            return

        self._connected.add(tool_name)
        new_tools = set(self._registry.list_tools()) - tools_before
        for internal_name in new_tools:
            self._tool_name_map[internal_name] = tool_name
        log.info("registered %d tools from %s: %s", len(new_tools), server_name, list(new_tools))

    async def connect_all(self, tool_names: list[str]) -> list[str]:
        """Connect all MCP tools in the list. Returns names of tools that failed to connect."""
        failed = []
        for name in tool_names:
            if name.startswith("mcp:"):
                await self.connect_tool(name)
                if name not in self._connected:
                    failed.append(name)
        return failed

    def get_tools(self) -> list[dict]:
        """Return all registered MCP tool schemas in OpenAI function-calling format."""
        return self._registry.get_schemas(api_format="openai-chat")

    def get_all_tool_names(self) -> list[str]:
        """Return all registered MCP tool names."""
        return self._registry.list_tools()

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute a tool by its internal registry name and return the result."""
        callable_fn = self._registry.get_callable(tool_name)
        if callable_fn is None:
            return f"Unknown MCP tool: {tool_name}"
        try:
            if asyncio.iscoroutinefunction(callable_fn):
                result = await callable_fn(**arguments)
            else:
                result = callable_fn(**arguments)
            return str(result) if result is not None else ""
        except Exception as e:
            log.exception("MCP tool %s failed", tool_name)
            return f"Tool error: {e}"

    async def close(self) -> None:
        """Shut down all MCP server connections."""
        try:
            await self._registry.close_async()
        except Exception:
            log.exception("error closing MCP connections")
        self._connected.clear()
        self._tool_name_map.clear()


class MCPInput(BaseModel):
    """Pass-through Pydantic input for MCP tools.

    MCP server tools have schemas the server reports at discovery time;
    we don't try to model each one statically on our side. ``extra="allow"``
    lets any keyword args flow through, and ``model_dump()`` reconstructs
    a plain dict for :meth:`McpToolManager.call_tool`.
    """

    model_config = ConfigDict(extra="allow")


class MCPTool:
    """``Tool`` wrapper around one MCP-server-discovered tool.

    One instance per discovered tool. We can't hand-write a class per
    MCP tool (the toolset is discovered at runtime from whatever server
    connects), so this single parameterised class fills in for the
    whole MCP family.

    Schemas for MCP tools come from the MCP server directly (via
    :meth:`McpToolManager.get_tools`); we don't derive them from a
    Pydantic model on our side. ``McpToolManager.call_tool`` already
    catches exceptions and returns ``"Tool error: ..."`` strings, so
    ``is_error=False`` is preserved here — the model reads the error
    text and recovers.
    """

    description = "Tool exposed by an attached MCP server."

    def __init__(self, name: str, mcp: McpToolManager) -> None:
        self.name = name
        self._mcp = mcp

    def oai_schema(self) -> dict:
        # Returns a minimal schema — the real, server-derived schemas
        # are obtained through ``McpToolManager.get_tools()``.
        # This method exists only to satisfy the ``Tool`` Protocol.
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    async def __call__(self, inp: MCPInput, ctx: ToolContext) -> ToolResult:
        try:
            text = await self._mcp.call_tool(self.name, inp.model_dump())
            return ToolResult(text=text, is_error=False)
        except Exception as exc:
            err = str(exc) or f"{self.name} failed with an unknown error"
            log.warning("MCP tool %s failed: %s", self.name, err)
            return ToolResult(text=err, is_error=True)
