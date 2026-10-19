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

    @dataclass(frozen=True)
    class Reconciliation:  # type: ignore[no-redef]
        applied: bool
        output: dict[str, Any] | None = None


Arguments = dict[str, Any]
Output = dict[str, Any]

__all__ = ["Arguments", "OperationContext", "OperationRejected", "Output", "Reconciliation"]
