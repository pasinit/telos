"""Per-context delegation chain — typed wrapper around a ``ContextVar``.

When agent A delegates to agent B (via the ``delegate_to`` / ``coordinate``
tools), each agent's ``_stream_lock`` is held by its own ``astream`` call.
Re-entering an agent that is already on the active call chain would deadlock
on that lock, so ``delegate_to`` and ``coordinate`` refuse to dispatch to a
slug that is already active.

``ContextVar`` is the right primitive here because it propagates through
``await`` and into tasks created by ``asyncio.gather`` (each task copies the
parent context at creation), so a child sees the chain of its ancestors;
sibling tasks don't leak modifications back to each other.

Lives in ``core`` because ``ToolContext`` references it and ``core`` is
the only layer that can be imported from both ``domain`` and the
application code that builds the context.
"""

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


class DelegationGraph:
    """Tracks the set of agent slugs currently on the call stack of one turn."""

    def __init__(self, name: str = "telos_delegation_chain") -> None:
        self._chain: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
            name, default=frozenset(),
        )

    def active(self) -> frozenset[str]:
        return self._chain.get()

    def contains(self, slug: str) -> bool:
        return slug in self._chain.get()

    def cyclic_subset(self, slugs: list[str]) -> list[str]:
        active = self._chain.get()
        return [s for s in slugs if s in active]

    @contextmanager
    def enter(self, slug: str) -> Iterator[None]:
        token = self._chain.set(self._chain.get() | {slug})
        try:
            yield
        finally:
            self._chain.reset(token)
