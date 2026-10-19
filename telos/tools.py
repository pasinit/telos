"""Tool catalogue — derives OAI schemas from :class:`Tool` classes.

Each builtin tool is represented by the same class that implements it
(``PythonExecTool``, ``WebSearchTool``, …). Its OpenAI function-call
schema is derived from the Pydantic ``Input`` model on the class via
:func:`telos.domain.tools.schema.schema_from_pydantic` — same source of
truth for runtime validation and for what the LLM sees.

Curated MCP servers are listed here too (name + description + transport
config). They don't have Tool *classes* on our side — the toolset is
discovered at runtime from whichever MCP server we connect to — so MCP
entries carry their transport directly.
"""

from dataclasses import dataclass
from typing import Any

from telos.domain.tools import (
    AgentStatusTool,
    BrowserBackTool,
    BrowserClickTool,
    BrowserFillTool,
    BrowserOpenTool,
    BrowserReadTool,
    CoordinateTool,
    CreateAgentTool,
    CreateRoutineTool,
    DelegateToTool,
    DeleteRoutineTool,
    DriveSearchTool,
    GDocsAppendTool,
    GDocsReadTool,
    GSheetsAppendTool,
    GSheetsListTabsTool,
    GSheetsReadTool,
    GSheetsUpdateTool,
    ListAgentsTool,
    ListRoutinesTool,
    MemoryRecallTool,
    PythonExecTool,
    SendEmailTool,
    ShellExecTool,
    WebSearchTool,
    ZooplaSearchTool,
)
from telos.domain.tools.agency_search import AgencySearchTool
from telos.domain.tools.property_candidates import ListPropertyCandidatesTool, RecordPropertyAssessmentTool
from telos.domain.tools.rightmove import RightmoveSearchTool
from telos.domain.tools.schema import oai_schema_for


@dataclass(frozen=True)
class ToolEntry:
    """One row in the tool catalogue.

    For built-in tools, ``cls`` holds the ``Tool`` class — the schema
    derives from its ``Input`` Pydantic model. For MCP servers, ``cls``
    is ``None`` and ``transport`` carries the launch config.
    """

    name: str
    description: str
    type: str  # "builtin" | "mcp"
    transport: str | dict[str, Any] | None = None
    cls: type | None = None
    housekeeper_only: bool = False

    @property
    def schema(self) -> dict[str, Any] | None:
        """Derived OAI schema for builtin tools; None for MCP entries."""
        if self.cls is None:
            return None
        return oai_schema_for(self.cls, name=self.name, description=self.description)


def _builtin(cls: type, *, housekeeper_only: bool = False) -> ToolEntry:
    """Build a ``ToolEntry`` from a Tool class — single source of truth."""
    return ToolEntry(
        name=cls.name,
        description=cls.description,
        type="builtin",
        cls=cls,
        housekeeper_only=housekeeper_only,
    )


class ToolRepository:
    """Registry of all tools available to Telos agents."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._load_defaults()

    def _load_defaults(self) -> None:
        # Code execution
        self._register(_builtin(PythonExecTool))
        self._register(_builtin(ShellExecTool))
        # Web + browser
        self._register(_builtin(WebSearchTool))
        self._register(_builtin(BrowserOpenTool))
        self._register(_builtin(BrowserClickTool))
        self._register(_builtin(BrowserFillTool))
        self._register(_builtin(BrowserReadTool))
        self._register(_builtin(BrowserBackTool))
        # Google
        self._register(_builtin(DriveSearchTool))
        self._register(_builtin(GDocsReadTool))
        self._register(_builtin(GDocsAppendTool))
        self._register(_builtin(GSheetsListTabsTool))
        self._register(_builtin(GSheetsReadTool))
        self._register(_builtin(GSheetsAppendTool))
        self._register(_builtin(GSheetsUpdateTool))
        # Team
        self._register(_builtin(DelegateToTool, housekeeper_only=True))
        self._register(_builtin(CoordinateTool))
        self._register(_builtin(ListAgentsTool))
        self._register(_builtin(AgentStatusTool))
        self._register(_builtin(CreateAgentTool, housekeeper_only=True))
        # Routines + email
        self._register(_builtin(CreateRoutineTool))
        self._register(_builtin(ListRoutinesTool))
        self._register(_builtin(DeleteRoutineTool))
        self._register(_builtin(SendEmailTool))
        # Long-term memory
        self._register(_builtin(MemoryRecallTool))
        # Site-specific search
        self._register(_builtin(RightmoveSearchTool))
        self._register(_builtin(ZooplaSearchTool))
        self._register(_builtin(AgencySearchTool))
        self._register(_builtin(RecordPropertyAssessmentTool))
        self._register(_builtin(ListPropertyCandidatesTool))

        # Curated MCP servers (no Tool class on our side — transport
        # describes how to launch the server; tool list is discovered
        # at connection time).
        self._register(ToolEntry(
            name="mcp:filesystem",
            description="Read, write, and manage files in the user's workspace directory.",
            type="mcp",
            transport={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "{workspace}"]},
        ))
        self._register(ToolEntry(
            name="mcp:github",
            description="Interact with GitHub: repos, issues, PRs, and code search.",
            type="mcp",
            transport={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        ))

    def _register(self, entry: ToolEntry) -> None:
        self._tools[entry.name] = entry

    def list_all(self) -> list[ToolEntry]:
        return list(self._tools.values())

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def get_transport(self, name: str, workspace: str | None = None) -> str | dict[str, Any] | None:
        entry = self._tools.get(name)
        if not entry or not entry.transport:
            return None
        transport = entry.transport
        if isinstance(transport, dict) and workspace:
            transport = dict(transport)
            if "args" in transport:
                transport["args"] = [a.replace("{workspace}", workspace) for a in transport["args"]]
        return transport

    def get_schemas(
        self,
        names: list[str] | None = None,
        *,
        include_housekeeper: bool = False,
    ) -> dict[str, dict]:
        """Return OpenAI function-call schemas keyed by tool name.

        Schemas are derived from each Tool class's Pydantic ``Input``
        model — never hand-rolled.
        """
        result: dict[str, dict] = {}
        for entry in self._tools.values():
            schema = entry.schema
            if schema is None:
                continue
            if entry.housekeeper_only and not include_housekeeper:
                continue
            if names is None or entry.name in names:
                result[entry.name] = schema
        return result

    def list_for_selection(self) -> list[dict[str, str]]:
        return [{"name": t.name, "description": t.description} for t in self._tools.values()]


# Singleton instance
tool_repository = ToolRepository()
