"""Provider-neutral nightly tools with lazy Claude SDK adaptation."""
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

from pydantic import TypeAdapter


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    handler: Any


def tool(name: str, description: str, input_schema: dict):
    def decorate(handler):
        schema = input_schema
        if not isinstance(schema.get("type"), str):
            properties = {}
            for key, annotation in schema.items():
                param_description = None
                if get_origin(annotation) is Annotated:
                    annotation, *metadata = get_args(annotation)
                    param_description = next((m for m in metadata if isinstance(m, str)), None)
                properties[key] = TypeAdapter(annotation).json_schema()
                if param_description:
                    properties[key]["description"] = param_description
            schema = {"type": "object", "properties": properties, "required": list(properties)}
        return ToolDefinition(name, description, schema, handler)
    return decorate


@dataclass
class ToolServer:
    name: str
    tools: list[ToolDefinition]


def create_sdk_mcp_server(name: str, *, tools: list[ToolDefinition]) -> ToolServer:
    # Kept as the builder seam; no SDK import or server construction until
    # a Claude session actually needs one.
    return ToolServer(name, tools)


def claude_servers(servers: dict) -> dict:
    from claude_agent_sdk import create_sdk_mcp_server as create
    from claude_agent_sdk import tool as sdk_tool

    return {
        name: create(name, tools=[sdk_tool(t.name, t.description, t.input_schema)(t.handler) for t in server.tools])
        if isinstance(server, ToolServer) else server
        for name, server in servers.items()
    }


def codex_tools(servers: dict, allowed: list[str]) -> tuple[list[dict], dict]:
    specs, handlers = [], {}
    matched = set()
    for server_name, server in servers.items():
        if not isinstance(server, ToolServer):
            raise ValueError("Codex requires provider-neutral tool definitions")
        for definition in server.tools:
            allowed_name = f"mcp__{server_name}__{definition.name}"
            if allowed_name in allowed:
                # Codex reserves mcp__ for its own MCP bridge. These are
                # host-dispatched dynamic tools; keep the Claude allowlist
                # separate from their Codex wire names.
                name = f"host__{server_name}__{definition.name}"
                matched.add(allowed_name)
                specs.append({"type": "function", "name": name, "description": definition.description,
                              "inputSchema": definition.input_schema})
                handlers[name] = definition.handler
    if set(allowed) != matched:
        raise ValueError("Nightly tool allowlist contains unregistered tools")
    return specs, handlers
