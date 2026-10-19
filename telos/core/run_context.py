"""Per-call context for an agent turn.

Lives in ``core`` so :class:`telos.domain.tools.context.ToolContext`
(also in ``core``) can reference it without crossing the
``core → domain`` layering forbidden direction.
"""

from dataclasses import dataclass

from telos.core.delegation import DelegationGraph


@dataclass(frozen=True)
class RunContext:
    """Per-call state for one ``agent.turn(...)`` invocation."""

    user_id: str
    delegation_graph: DelegationGraph
    image_url: str | None = None
    is_routine: bool = False
    request_id: str | None = None
    correlation_id: str | None = None
