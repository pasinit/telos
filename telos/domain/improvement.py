"""Domain-layer types for the improvement subsystem.

``AgentImprovementState`` is the contract object that flows from
``Agent.improvement_state()`` (core) to ``ImprovementService`` (services).
Lives in ``domain/`` because both producer (``Agent`` in core) and
consumer (``ImprovementService`` in services) need to name the type, and
``core → services`` is the forbidden layering direction.
"""

from collections.abc import Callable
from dataclasses import dataclass

from telos.core.ports import ApprovalPort, ConversationLogPort, ImproverPort


@dataclass(frozen=True)
class AgentImprovementState:
    """The slice of an ``Agent``'s state that ``ImprovementService`` needs.

    Built by ``AgentRuntime.get_improvement_state(uid, slug)`` from a
    cached agent via ``Agent.improvement_state()``. Consumed by
    :class:`telos.services.improvement.ImprovementService`. The service
    never imports ``Agent`` — this contract is the only thing that
    crosses the boundary.

    Attributes:
        improver:            For ``await improver.run(current_prompt)``.
        approval:            For ``approval.stage(candidate)``.
        conversation_log:    For ``count()`` — used to seed the
                             per-``(uid, slug)`` counter on first event.
        get_current_prompt:  Reads the agent's current system prompt
                             under the agent's prompt lock. Callable so
                             the lock stays owned by the agent.
        approve_candidate:   Runs the agent's full approve sequence
                             (stage → swap current → write to disk →
                             clear pending). Used when ``score_delta``
                             exceeds the auto-deploy threshold.
    """

    improver: ImproverPort
    approval: ApprovalPort
    conversation_log: ConversationLogPort
    get_current_prompt: Callable[[], str]
    approve_candidate: Callable[[], None]
