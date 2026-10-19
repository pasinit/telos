"""Per-tool typed inputs + concrete tools + the per-call envelope.

Every tool implements :class:`telos.core.ports.Tool` — typed input
class, typed output class. Tool dependencies are constructor-injected
by ``telos.tool_wiring.build_tools``; ``ToolContext`` only carries
per-call data.

See `#135 <https://github.com/pasinit/telos/issues/135>`_ for the
unification refactor.
"""

from telos.domain.tools.code import PythonExecInput, PythonExecTool, ShellExecInput, ShellExecTool
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.email import SendEmailInput, SendEmailTool
from telos.domain.tools.google import (
    DriveSearchInput,
    DriveSearchTool,
    GDocsAppendInput,
    GDocsAppendTool,
    GDocsReadInput,
    GDocsReadTool,
    GoogleCredsResolver,
    GSheetsAppendInput,
    GSheetsAppendTool,
    GSheetsListTabsInput,
    GSheetsListTabsTool,
    GSheetsReadInput,
    GSheetsReadTool,
    GSheetsUpdateInput,
    GSheetsUpdateTool,
)
from telos.domain.tools.memory import MemoryRecallInput, MemoryRecallTool
from telos.domain.tools.rightmove import RightmoveSearchInput, RightmoveSearchTool
from telos.domain.tools.routines import (
    CreateRoutineInput,
    CreateRoutineTool,
    DeleteRoutineInput,
    DeleteRoutineTool,
    ListRoutinesInput,
    ListRoutinesTool,
)
from telos.domain.tools.schema import coerce_args, oai_schema_for, schema_from_pydantic
from telos.domain.tools.team import (
    AgentStatusInput,
    AgentStatusTool,
    CoordinateInput,
    CoordinateTool,
    CreateAgentInput,
    CreateAgentTool,
    DelegateToInput,
    DelegateToTool,
    ListAgentsInput,
    ListAgentsTool,
)
from telos.domain.tools.web import (
    BrowserBackInput,
    BrowserBackTool,
    BrowserClickInput,
    BrowserClickTool,
    BrowserFillInput,
    BrowserFillTool,
    BrowserOpenInput,
    BrowserOpenTool,
    BrowserReadInput,
    BrowserReadTool,
    WebSearchInput,
    WebSearchTool,
)
from telos.domain.tools.zoopla import ZooplaSearchInput, ZooplaSearchTool


__all__ = [
    # Per-call envelope + product
    "ToolContext", "ToolResult",
    # Schema utilities
    "schema_from_pydantic", "coerce_args",
    # Concrete tool classes + their Pydantic input models
    "PythonExecTool", "PythonExecInput",
    "ShellExecTool", "ShellExecInput",
    "WebSearchTool", "WebSearchInput",
    "BrowserOpenTool", "BrowserOpenInput",
    "BrowserClickTool", "BrowserClickInput",
    "BrowserFillTool", "BrowserFillInput",
    "BrowserReadTool", "BrowserReadInput",
    "BrowserBackTool", "BrowserBackInput",
    "DriveSearchTool", "DriveSearchInput",
    "GDocsReadTool", "GDocsReadInput",
    "GDocsAppendTool", "GDocsAppendInput",
    "GSheetsListTabsTool", "GSheetsListTabsInput",
    "GSheetsReadTool", "GSheetsReadInput",
    "GSheetsAppendTool", "GSheetsAppendInput",
    "GSheetsUpdateTool", "GSheetsUpdateInput",
    "GoogleCredsResolver",
    "DelegateToTool", "DelegateToInput",
    "CoordinateTool", "CoordinateInput",
    "ListAgentsTool", "ListAgentsInput",
    "AgentStatusTool", "AgentStatusInput",
    "CreateAgentTool", "CreateAgentInput",
    "CreateRoutineTool", "CreateRoutineInput",
    "ListRoutinesTool", "ListRoutinesInput",
    "DeleteRoutineTool", "DeleteRoutineInput",
    "SendEmailTool", "SendEmailInput",
    "MemoryRecallTool", "MemoryRecallInput",
    "RightmoveSearchTool", "RightmoveSearchInput",
    "ZooplaSearchTool", "ZooplaSearchInput",
]
