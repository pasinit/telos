"""Cardea's handler contract (ADR-023/026), imported from Cardea when installed.

Inside Cardea's environment the real classes are used, so ``OperationRejected``
raised here is the one Cardea's dispatcher catches. Without Cardea (tests, local
use) structurally identical stand-ins keep the handlers importable.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

try:
    from cardea.service.operations import OperationContext, OperationRejected, Reconciliation
except ImportError:  # pragma: no cover - exercised only without Cardea installed

    class OperationRejected(Exception):  # type: ignore[no-redef]
        """Raised by a handler that is certain no external effect happened."""

    @dataclass(frozen=True)
    class OperationContext:  # type: ignore[no-redef]
        task_id: str
        namespace: str
        receipt_id: str
        deadline: datetime
        effect_key: str
        lease_id: str = ""
        # Fields Cardea is adding to the context; read them with the helpers below.
        agent_id: str | None = None
        idempotency_context: str | None = None
        slot_time: str | None = None

    @dataclass(frozen=True)
    class Reconciliation:  # type: ignore[no-redef]
        applied: bool
        output: dict[str, Any] | None = None


Arguments = dict[str, Any]
Output = dict[str, Any]


def agent_id(context: OperationContext) -> str | None:
    """The calling agent run's principal; ``None`` outside agent runs or on a Cardea without the field."""
    return getattr(context, "agent_id", None) or None


def slot_time(context: OperationContext) -> str | None:
    """ISO-8601 scheduled slot time of a trigger-admitted run, when Cardea provides it."""
    return getattr(context, "slot_time", None) or None


__all__ = ["Arguments", "OperationContext", "OperationRejected", "Output", "Reconciliation", "agent_id", "slot_time"]
