"""``ToolContext`` and ``ToolResult`` — the per-call envelope and product
of every :class:`telos.core.ports.Tool` invocation.

Lives in ``core`` so the :class:`Tool` Protocol can annotate
``ctx: ToolContext`` directly — keeps the type information at the
Protocol level without the ``Any`` escape hatch.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telos.core.run_context import RunContext


# Callback the executor uses to surface streaming delegation events
# (``delegate_to`` / ``coordinate``) back to the agent loop.
OnEvent = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ToolContext:
    """Per-call data every :class:`Tool` receives.

    Attributes:
        run_context:     Per-call ``RunContext`` (user_id,
                         delegation_graph, image_url, is_routine,
                         request_id, correlation_id).
        agent_slug:      Slug of the agent invoking the tool.
        workspace:       Per-user sandbox/MCP workspace path.
        storage_root:    Root for per-agent persistence.
        browser_session: Per-turn stateful browser session, created
                         lazily by ``Agent.astream``. None when no
                         browser tools are enabled.
        on_event:        Streaming event sink for delegation
                         (``None`` outside ``delegate_to`` /
                         ``coordinate``).
    """

    run_context: RunContext
    agent_slug: str
    workspace: Path
    storage_root: Path
    browser_session: Any = None
    on_event: OnEvent | None = None

    @property
    def user_id(self) -> str:
        return self.run_context.user_id


@dataclass(frozen=True)
class ToolResult:
    """The product of a single tool call.

    Attributes:
        text:     Human-readable / model-readable text the agent feeds
                  back into the model as the tool result.
        is_error: True when the tool ran but produced an error condition.
        images:   Optional inline images (base64 data URIs) the tool
                  surfaced (e.g. ``browser_open`` extracts a few).
    """

    text: str
    is_error: bool = False
    images: list[str] | None = None
