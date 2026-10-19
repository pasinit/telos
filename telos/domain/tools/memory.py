"""``memory_recall`` — targeted long-term-memory search over Recordatio.

Runtime agents receive memory only as a one-shot briefing injected by
Recordatio at the start of a turn. That briefing is a single ranked
snapshot: when a prompt commands "run an explicit, targeted memory
search for EACH tracked organisation" (jobber) or "run a dedicated
memory search on that lead's street AND portal listing ID" (homey),
there is no way to satisfy it — the model can only re-read whatever the
briefing already surfaced, and anchors on a degraded summary when the
detail sits in units the briefing didn't rank.

This tool closes that gap: it lets the agent issue an explicit,
targeted query against Recordatio's retrieval endpoint mid-turn. The
retrieval callable is constructor-injected (``recall``) exactly like
``WebSearchTool`` takes its ``search`` — the tool never reaches for
infrastructure through ``ctx``. ``None`` is accepted so deployments
without a Recordatio memory endpoint can still register the tool; it then
returns "not configured" at call time instead of vanishing from the
schema (mirrors :class:`SendEmailTool`).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for

log = logging.getLogger(__name__)


# The injected retrieval callable. Given the caller's ``query`` plus the
# per-call ``user_id``/``slug`` (read off ``ctx``), it returns Recordatio's
# raw memory-search JSON payload (the same shape the nightly analyst's
# ``search_memories`` consumes). Raising on transport/HTTP failure is
# fine — the tool converts it into a ``ToolResult`` error.
MemoryRecall = Callable[..., Awaitable[dict[str, Any]]]

# Keys Recordatio may nest the ranked hits under. Used only to detect the
# empty-result case for a friendlier message; the full payload is always
# serialised so no detail is silently dropped.
_RESULT_KEYS = ("results", "hits", "memories", "units", "matches")

_NOT_CONFIGURED = ToolResult(
    text="Long-term memory recall is not configured on this Telos instance.",
    is_error=True,
)


class MemoryRecallInput(BaseModel):
    query: str = Field(
        description=(
            "A targeted natural-language query to recall from long-term "
            "memory — e.g. a specific organisation name, a street, or a "
            "portal listing ID. Run one focused query per entity rather "
            "than a single broad query."
        ),
    )
    k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of memories to return (default 10).",
    )


def _shape_memory_result(data: dict[str, Any], *, query: str) -> ToolResult:
    """Render Recordatio's memory-search payload into a ``ToolResult``.

    The payload shape is owned by Recordatio; we pass it through as JSON
    (the same contract the nightly analyst consumes) so no recalled
    detail is lost, and only special-case the empty result for a clearer
    signal to the model that nothing is on record for the query.
    """
    if not data:
        return ToolResult(text=f'No memories found for "{query}".')
    for key in _RESULT_KEYS:
        val = data.get(key)
        if isinstance(val, list) and not val:
            return ToolResult(text=f'No memories found for "{query}".')
    return ToolResult(text=json.dumps(data, ensure_ascii=False, default=str))


class MemoryRecallTool:
    """``memory_recall`` — targeted search of the agent's Recordatio memory."""

    name = "memory_recall"
    description = (
        "Search this agent's long-term memory for facts recalled from past "
        "conversations. Use this for targeted recall beyond the one-shot "
        "briefing injected at the start of the turn — e.g. everything "
        "remembered about a specific organisation, street, or listing ID. "
        "Run one focused query per entity."
    )

    def __init__(self, recall: MemoryRecall | None) -> None:
        self._recall = recall

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: MemoryRecallInput, ctx: ToolContext) -> ToolResult:
        if self._recall is None:
            return _NOT_CONFIGURED
        try:
            data = await self._recall(
                inp.query,
                user_id=ctx.user_id,
                slug=ctx.agent_slug,
                k=inp.k,
            )
        except Exception as e:
            log.warning("memory_recall failed: %s", e)
            return ToolResult(text=f"memory_recall failed: {e}", is_error=True)
        return _shape_memory_result(data, query=inp.query)
