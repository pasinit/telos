"""Team-family tools — multi-agent coordination.

Five tools — the only ones in the dispatcher that touch the per-call
:class:`~telos.domain.DelegationGraph` and the constructor-injected
``(user_id, slug) -> Agent`` lookup the runtime hands out.

- ``delegate_to`` — one-shot dispatch to a sibling specialist; refuses
  cycles via the ``DelegationGraph``.
- ``coordinate`` — parallel fan-out via :func:`asyncio.gather` to several
  sibling specialists at once.
- ``list_agents`` / ``agent_status`` — read-only registry views the
  housekeeper uses to introspect its team.
- ``create_agent`` — stages a pending agent request the housekeeper UI
  surfaces for user confirmation. No LLM call here; goal generation
  runs in the chat-route confirmation flow.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from telos.core.ports import AgentRegistry

from telos.domain.run_context import RunContext
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for
from telos.log import ConversationLog

log = logging.getLogger(__name__)

# ``coordinate`` fan-out safety rails. Without them one hung sibling would
# stall the whole ``asyncio.gather`` (no per-sibling timeout) and a wide
# fan-out could open an unbounded number of concurrent sibling turns.
_COORDINATE_MAX_CONCURRENCY = 4
_COORDINATE_SIBLING_TIMEOUT_S = 120.0


_TEAM_NOT_AVAILABLE = ToolResult(
    text="Team tools are not available — agent was not constructed with a get_agent_fn.",
    is_error=True,
)
_REGISTRY_NOT_AVAILABLE = ToolResult(
    text="Agent registry not available — this Telos process can't enumerate sibling agents.",
    is_error=True,
)


async def _drain_sibling_stream(
    agent,
    msg: str,
    child_ctx: RunContext,
    slug: str,
    on_event,
) -> str:
    """Run the sibling agent's stream, accumulate its text, fan tool events out."""
    chunks: list[str] = []
    stream = agent.astream(msg, child_ctx)
    try:
        async for kind, data in stream:
            if kind == "text":
                chunks.append(data)
                if on_event:
                    await on_event("delegation_text", {"agent": slug, "text": data})
            elif kind == "tool_call" and on_event:
                await on_event("delegation_tool", {"agent": slug, "tool_call": data})
            elif kind == "tool_result" and on_event:
                await on_event("delegation_tool_result", {"agent": slug, "tool_result": data})
    finally:
        # Close the generator even when we are cancelled between steps (e.g.
        # while awaiting ``on_event``) — otherwise it stays suspended holding
        # the sibling's ``_stream_lock`` until GC finalization gets around to
        # it, locking the sibling out for an unbounded window.
        await stream.aclose()
    return "".join(chunks)


# ──────────────────────────────────────────────────────────────────────
# delegate_to / coordinate
# ──────────────────────────────────────────────────────────────────────


class DelegateToInput(BaseModel):
    agent_slug: str = Field(description="Slug of the specialist to delegate to.")
    message: str = Field(description="Message / task to forward.")


class DelegateToTool:
    """``delegate_to`` — hand a single sub-task off to a named specialist."""

    name = "delegate_to"
    description = "Delegate a sub-task to a named specialist agent."

    def __init__(self, get_agent_fn: Callable[[str, str], Awaitable[Any]] | None) -> None:
        self._get_agent_fn = get_agent_fn

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: DelegateToInput, ctx: ToolContext) -> ToolResult:
        if self._get_agent_fn is None:
            return _TEAM_NOT_AVAILABLE
        slug = inp.agent_slug
        graph = ctx.run_context.delegation_graph
        if graph.contains(slug):
            active = graph.active()
            return ToolResult(
                text=(
                    f"Refusing to delegate to '{slug}': it is already in the "
                    f"active delegation chain ({sorted(active)}). "
                    f"Cycles are not allowed."
                ),
                is_error=True,
            )
        agent = await self._get_agent_fn(ctx.user_id, slug)
        child_ctx = RunContext(user_id=ctx.user_id, delegation_graph=graph)
        response = await _drain_sibling_stream(
            agent, inp.message, child_ctx, slug, ctx.on_event,
        )
        return ToolResult(text=f"[Response from {slug}]\n\n{response}")


class CoordinateInput(BaseModel):
    agent_slugs: list[str] = Field(description="Specialists to fan out to in parallel.")
    message: str = Field(description="Message broadcast to all targeted specialists.")


class CoordinateTool:
    """``coordinate`` — fan one prompt out to several specialists in parallel."""

    name = "coordinate"
    description = "Coordinate with multiple specialist agents in parallel."

    def __init__(self, get_agent_fn: Callable[[str, str], Awaitable[Any]] | None) -> None:
        self._get_agent_fn = get_agent_fn

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: CoordinateInput, ctx: ToolContext) -> ToolResult:
        if self._get_agent_fn is None:
            return _TEAM_NOT_AVAILABLE
        # Dedupe while preserving order: duplicate slugs would serialize on
        # the same sibling's ``_stream_lock`` and the second copy would burn
        # its whole timeout waiting for the first.
        slugs = list(dict.fromkeys(inp.agent_slugs))
        graph = ctx.run_context.delegation_graph
        cyclic = graph.cyclic_subset(slugs)
        if cyclic:
            active = graph.active()
            return ToolResult(
                text=(
                    f"Refusing to coordinate with {cyclic}: already in the "
                    f"active delegation chain ({sorted(active)}). "
                    f"Cycles are not allowed."
                ),
                is_error=True,
            )
        msg = inp.message
        child_ctx = RunContext(user_id=ctx.user_id, delegation_graph=graph)

        # Bound concurrency so a wide fan-out doesn't open one sibling turn
        # per slug at once, and give each sibling its own wall-clock timeout
        # so a single hung specialist fails alone instead of stalling the
        # whole gather (which would hang the coordinating turn).
        sem = asyncio.Semaphore(_COORDINATE_MAX_CONCURRENCY)

        async def _one(slug: str) -> str:
            assert self._get_agent_fn is not None  # narrowed above
            async with sem:
                # The timeout covers agent lookup too — if that path ever
                # grows a blocking await, it must not reopen the unbounded
                # hang this timeout exists to prevent.
                async with asyncio.timeout(_COORDINATE_SIBLING_TIMEOUT_S):
                    sibling = await self._get_agent_fn(ctx.user_id, slug)
                    return await _drain_sibling_stream(
                        sibling, msg, child_ctx, slug, ctx.on_event,
                    )

        results = await asyncio.gather(*(_one(s) for s in slugs), return_exceptions=True)
        parts: list[str] = []
        for slug, r in zip(slugs, results, strict=True):
            if isinstance(r, TimeoutError):
                log.warning("coordinate: sibling %s timed out after %.0fs", slug, _COORDINATE_SIBLING_TIMEOUT_S)
                parts.append(f"## {slug} (timed out)\nNo response within {_COORDINATE_SIBLING_TIMEOUT_S:.0f}s (sibling busy or hung).")
            elif isinstance(r, Exception):
                parts.append(f"## {slug} (error)\n{r}")
            else:
                parts.append(f"## {slug}\n{r}")
        return ToolResult(text="\n\n".join(parts))


# ──────────────────────────────────────────────────────────────────────
# list_agents / agent_status / create_agent
# ──────────────────────────────────────────────────────────────────────


class ListAgentsInput(BaseModel):
    """No arguments — lists every specialist agent for this user."""


class ListAgentsTool:
    """``list_agents`` — surface the user's specialist roster to the housekeeper."""

    name = "list_agents"
    description = "List the user's specialist agents."

    def __init__(self, registry: AgentRegistry | None) -> None:
        self._registry = registry

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: ListAgentsInput, ctx: ToolContext) -> ToolResult:
        if self._registry is None:
            return _REGISTRY_NOT_AVAILABLE
        agents = self._registry.list_specialists(ctx.user_id)
        lines = [
            f"- **{a['name']}** ({a['slug']}): {a['description']}"
            for a in agents
        ]
        return ToolResult(text="\n".join(lines) or "No specialist agents yet.")


class AgentStatusInput(BaseModel):
    agent_slug: str = Field(description="Slug of the specialist to inspect.")


class AgentStatusTool:
    """``agent_status`` — quick health snapshot of one specialist."""

    name = "agent_status"
    description = "Read a specialist agent's recent activity + signal rates."

    def __init__(self, registry: AgentRegistry | None) -> None:
        self._registry = registry

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: AgentStatusInput, ctx: ToolContext) -> ToolResult:
        if self._registry is None:
            return _REGISTRY_NOT_AVAILABLE
        meta = self._registry.get(ctx.user_id, inp.agent_slug)
        if not meta:
            return ToolResult(text=f"Agent '{inp.agent_slug}' not found.", is_error=True)
        history_path = Path(ctx.storage_root) / ctx.user_id / inp.agent_slug / "history.db"
        signals = ConversationLog(history_path).signal_summary()
        return ToolResult(text=(
            f"Agent: {meta['name']}\n"
            f"Description: {meta['description']}\n"
            f"Total turns: {signals['total_turns']}\n"
            f"Correction rate: {signals['correction_rate']}\n"
            f"Follow-up rate: {signals['follow_up_rate']}\n"
            f"Ignored rate: {signals['ignored_rate']}\n"
            f"Last active: {meta.get('last_active', 'never')}"
        ))


class CreateAgentInput(BaseModel):
    name: str = Field(description="Human-readable name for the new specialist.")
    goal: str = Field(description="Goal / responsibilities of the new specialist.")


class CreateAgentTool:
    """``create_agent`` — stage a pending specialist request (housekeeper-only)."""

    name = "create_agent"
    description = (
        "Propose creating a new specialist agent. The user confirms before "
        "creation; this tool only stages the request."
    )

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: CreateAgentInput, ctx: ToolContext) -> ToolResult:
        payload = {
            "pending_agent": True,
            "name": inp.name,
            "goal": inp.goal,
        }
        return ToolResult(text=json.dumps(payload))
