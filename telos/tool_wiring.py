"""Tool wiring — one place that knows how every tool is constructed.

Every tool the agent can call is built here with its dependencies
injected via ``__init__``. The result is a plain ``dict[str, Tool]``
keyed by tool name (no ``Dispatcher`` class — a dict + a small dispatch
function in ``agent.py`` is all we need).

Two registration funnels:

- :func:`build_tools` — called once per agent at construction time.
- :func:`register_mcp_tools` — called from ``Agent.set_mcp`` after the
  MCP manager has connected its servers, adding one ``MCPTool`` per
  discovered tool to the agent's existing tools dict.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telos.core.ports import (
    AgentRegistry,
    LLMComplete,
    RoutineRunnerProvider,
    RoutineStore,
    SandboxRun,
    Tool,
)
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
    GoogleCredsResolver,
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
from telos.domain.tools.agency_search import AgencySearchTool, SearchDispatch
from telos.domain.tools.property_candidates import ListPropertyCandidatesTool, RecordPropertyAssessmentTool
from telos.domain.tools.onthemarket import OnTheMarketSearchTool
from telos.domain.tools.rightmove import RightmoveSearchTool
from telos.mcp import MCPTool, McpToolManager
from telos.transport.browser import agencies as _agencies
from telos.transport.browser.fetcher import fetch_json as _fetch_json
from telos.transport.browser.fetcher import fetch_raw as _fetch_raw
from telos.transport.browser.fetcher import fetch_rendered as _fetch_rendered

log = logging.getLogger(__name__)


def build_tools(
    *,
    sandbox_run: SandboxRun,
    web_search: Callable[[str], Awaitable[tuple[str, bool]]],
    web_fetch: Callable[[str], Awaitable[dict[str, Any]]],
    agency_search: SearchDispatch | None = None,
    is_private_url: Callable[[str], bool],
    llm_complete: LLMComplete,
    routine_store: RoutineStore,
    # Provider, not a runner: the live ``RoutineRunner`` is created by the
    # FastAPI lifespan AFTER tool catalogues are built (notably the MCP
    # host's, at ``telos.api.app`` module load), so the routine tools must
    # resolve it at call time. ``None`` → store-only (tests, one-shot CLI).
    routine_runner_provider: RoutineRunnerProvider | None = None,
    send_email_fn: Callable[[str, str, str], None] | None = None,
    # Retrieval callable for the ``memory_recall`` tool — issues a
    # targeted query against Recordatio's memory-search endpoint. ``None``
    # (tests / one-shot CLI / no Recordatio memory endpoint) keeps the tool
    # registered but returns "not configured" at call time, matching the
    # ``send_email_fn`` pattern.
    memory_recall: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    registry: AgentRegistry | None = None,
    google_oauth_client_id: str = "",
    google_oauth_client_secret: str = "",
    get_agent_fn: Callable[[str, str], Awaitable[Any]] | None = None,
) -> dict[str, Tool]:
    """Construct every tool with its dependencies; return a name→Tool dict.

    The caller (``Agent.__init__``) passes the same real implementations
    every tool will use for its lifetime. Tools never call back into
    ``ctx`` for these.
    """
    google_creds = GoogleCredsResolver(
        google_oauth_client_id, google_oauth_client_secret, registry,
    )
    if agency_search is None:
        # Default dispatch for the in-process tool set (Agent._tool_set, tests):
        # each agency impl fetches its own way via the fetcher's raw/json helpers.
        agency_search = _agencies.make_search_dispatch(
            fetch_html=_fetch_raw, fetch_json=_fetch_json, fetch_html_js=_fetch_rendered)
    tools: list[Tool] = [
        # Code execution
        PythonExecTool(sandbox_run),
        ShellExecTool(sandbox_run),
        # Web + browser
        WebSearchTool(web_search),
        BrowserOpenTool(is_private_url),
        BrowserClickTool(),
        BrowserFillTool(),
        BrowserReadTool(),
        BrowserBackTool(),
        # Google
        DriveSearchTool(google_creds),
        GDocsReadTool(google_creds),
        GDocsAppendTool(google_creds),
        GSheetsListTabsTool(google_creds),
        GSheetsReadTool(google_creds),
        GSheetsAppendTool(google_creds),
        GSheetsUpdateTool(google_creds),
        # Team coordination
        DelegateToTool(get_agent_fn),
        CoordinateTool(get_agent_fn),
        ListAgentsTool(registry),
        AgentStatusTool(registry),
        CreateAgentTool(),
        # Routines + email
        CreateRoutineTool(routine_store, llm_complete, routine_runner_provider),
        ListRoutinesTool(routine_store),
        DeleteRoutineTool(routine_store, routine_runner_provider),
        SendEmailTool(send_email_fn),
        # Long-term memory
        MemoryRecallTool(memory_recall),
        # Site-specific search
        RightmoveSearchTool(web_fetch),
        ZooplaSearchTool(web_fetch),
        OnTheMarketSearchTool(web_fetch),
        AgencySearchTool(agency_search),
        RecordPropertyAssessmentTool(),
        ListPropertyCandidatesTool(),
    ]
    return {t.name: t for t in tools}


def register_mcp_tools(tools: dict[str, Tool], mcp: McpToolManager) -> int:
    """Register every connected MCP tool with the agent's ``tools`` dict.

    Called from ``Agent.set_mcp`` *after* ``mcp.connect_all(...)`` has
    populated the manager's tool registry. Returns the number of tools
    registered.
    """
    n = 0
    for name in mcp.get_all_tool_names():
        tools[name] = MCPTool(name, mcp)
        n += 1
    if n:
        log.info("registered %d MCP tools", n)
    return n
