"""In-process SDK MCP toolset for the nightly analyst.

The analyst is a ``claude-agent-sdk`` session whose ONLY capabilities are
the tools built here — no web, no shell, no filesystem. Read-tools pull
exactly what the analyst asks for (turns, briefings, memory probes) rather
than stuffing a whole day's transcript into the prompt; write-tools are how
results leave the session, validated against :mod:`.schemas` so a malformed
payload is rejected at the boundary.

Two toolsets:

- ``build_agent_toolset`` — per-agent map phase: inspect that agent's turns,
  see what memory was recalled, probe what *should* have been recalled, then
  ``submit_agent_feedback``.
- ``build_memory_toolset`` — reduce phase: read the day's extracted units,
  probe recall across the user, then ``submit_memory_feedback``.

Each builder returns a :class:`Toolset` — the in-process MCP server(s) to
hand to ``ClaudeAgentOptions``, the fully-qualified ``allowed_tools`` names,
a mutable result holder the write-tool fills, and the raw handlers (so tests
can drive the tools without spawning the SDK CLI).

Every tool declares an explicit, ``Annotated`` input schema; because the SDK
hands a handler one dict, each handler unpacks that dict into named, typed
locals in a single place rather than reaching into an opaque bag mid-body.
"""
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from telos.core.repos.turns import Turn
from telos.services.nightly.recordatio_client import RecordatioFeedbackClient
from telos.services.nightly.schemas import AgentFeedback, MemoryFeedback
from telos.services.nightly.tool_definitions import create_sdk_mcp_server, tool

# Per-turn text is truncated in the paginated ``get_turns`` view so a chatty
# day can't blow the context; ``get_turn`` fetches one turn in full.
_HEAD = 600
_TAIL = 600
_PAGE = 8

# Explicit, self-documenting input schemas for the read tools. The SDK hands a
# tool handler a single dict (its contract), so each handler unpacks that dict
# into named, typed locals in one place rather than reaching into an opaque
# bag; ``Annotated[type, "…"]`` documents each expected argument to the model.
# The write tools' schemas ARE their pydantic models (fully explicit already).
_GET_TURNS_SCHEMA = {"page": Annotated[int, "zero-based page index into the day's turns"]}
_GET_TURN_SCHEMA = {"turn_id": Annotated[str, "id of the turn to fetch in full"]}
_GET_BRIEFING_SCHEMA = {"request_id": Annotated[str, "the turn's request_id (read it off the turn)"]}
_SEARCH_SCHEMA = {
    "query": Annotated[str, "the facts a good answer needed, phrased as a search query"],
    "mode": Annotated[str, "'production' replays real briefing ranking; 'raw' only checks existence"],
    "as_of": Annotated[str, "ISO-8601 turn timestamp; the probe ignores memories written after it"],
    "k": Annotated[int, "max results to return (default 10)"],
}
_LIST_UNITS_SCHEMA = {"limit": Annotated[int, "max units to list (default 200)"]}


def _ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _clip(text: str) -> str:
    if len(text) <= _HEAD + _TAIL:
        return text
    return f"{text[:_HEAD]}\n…[{len(text) - _HEAD - _TAIL} chars elided]…\n{text[-_TAIL:]}"


def _executed_by(t: Turn) -> str:
    """Who ran the turn: Cardea-ingested routine runs carry a ``cardea:<task id>`` trace id."""
    return "cardea" if (t.trace_id or "").startswith("cardea:") else "telos"


def _turn_summary(t: Turn) -> dict[str, Any]:
    return {
        "turn_id": t.turn_id,
        "timestamp": t.timestamp,
        "is_routine": t.is_routine,
        "executed_by": _executed_by(t),
        "request_id": t.request_id,
        "flags": {"corrected": t.corrected, "followed_up": t.followed_up, "ignored": t.ignored},
        "user_message": _clip(t.user_message),
        "assistant_response": _clip(t.assistant_response),
    }


@dataclass
class AgentToolResult:
    """Filled by ``submit_agent_feedback``; ``None`` until the analyst calls it."""

    feedback: AgentFeedback | None = None


@dataclass
class MemoryToolResult:
    feedback: MemoryFeedback | None = None


@dataclass
class Toolset:
    """What a builder hands the analyst — plus the raw handlers so tests can
    drive the tools without spawning the SDK CLI."""

    mcp_servers: dict[str, Any]
    allowed_tools: list[str]
    result: Any
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]


@dataclass
class ToolContext:
    """Everything the tools close over for one session."""

    user_id: str
    slug: str | None
    day: str
    client: RecordatioFeedbackClient
    turns: list[Turn] = field(default_factory=list)
    event_reader: Callable | None = None


async def _search(
    ctx: ToolContext,
    query: str,
    *,
    mode: str,
    as_of: str | None,
    k: int,
    slug: str | None,
) -> dict[str, Any]:
    """Run a memory-search probe with explicit, typed arguments (shared by
    both toolsets' ``search_memories``). Surfaces upstream errors to the
    analyst as a tool error instead of crashing the session."""
    try:
        return _ok(await ctx.client.memory_search(
            ctx.user_id, query, mode=mode, slug=slug, as_of=as_of, k=k))
    except Exception as exc:
        return _err(f"memory-search failed: {exc.__class__.__name__}: {exc}")


def build_agent_toolset(ctx: ToolContext) -> Toolset:
    """Toolset for a per-agent map session. ``ctx.turns`` is that agent's
    turns for the day; ``ctx.slug`` scopes the memory probes."""
    result = AgentToolResult()
    by_id = {t.turn_id: t for t in ctx.turns}

    @tool("get_turns", "List this agent's turns for the day (paginated, text clipped). Returns a page of turn summaries with quality flags.", _GET_TURNS_SCHEMA)
    async def get_turns(params: dict[str, Any]) -> dict[str, Any]:
        page: int = max(int(params.get("page", 0)), 0)
        start = page * _PAGE
        window = ctx.turns[start : start + _PAGE]
        return _ok({
            "total": len(ctx.turns),
            "page": page,
            "page_size": _PAGE,
            "turns": [_turn_summary(t) for t in window],
        })

    @tool("get_turn", "Fetch one turn in full (untruncated user+assistant text) by turn_id.", _GET_TURN_SCHEMA)
    async def get_turn(params: dict[str, Any]) -> dict[str, Any]:
        turn_id: str = str(params.get("turn_id", ""))
        t = by_id.get(turn_id)
        if t is None:
            return _err(f"no such turn_id in this day: {turn_id!r}")
        return _ok({
            "turn_id": t.turn_id,
            "timestamp": t.timestamp,
            "is_routine": t.is_routine,
            "executed_by": _executed_by(t),
            "request_id": t.request_id,
            "flags": {"corrected": t.corrected, "followed_up": t.followed_up, "ignored": t.ignored},
            "user_message": t.user_message,
            "assistant_response": t.assistant_response,
        })

    @tool("get_briefing", "What memory Recordatio recalled for a turn (units/entities), given the turn's request_id. Use before search_memories to see what was actually surfaced.", _GET_BRIEFING_SCHEMA)
    async def get_briefing(params: dict[str, Any]) -> dict[str, Any]:
        request_id: str = str(params.get("request_id") or "")
        if not request_id:
            return _err("request_id is required (read it off the turn)")
        return _ok(await ctx.client.get_briefing(request_id))

    @tool("search_memories", "Probe what SHOULD have been recalled. mode=production replays the real ranking; mode=raw checks existence. Pass as_of=<turn timestamp> so the probe can't see memories written after the turn.", _SEARCH_SCHEMA)
    async def search_memories(params: dict[str, Any]) -> dict[str, Any]:
        query: str = str(params.get("query") or "").strip()
        mode: str = str(params.get("mode") or "production")
        as_of: str | None = str(params.get("as_of") or "") or None
        k: int = int(params.get("k", 10))
        if not query:
            return _err("query is required")
        return await _search(ctx, query, mode=mode, as_of=as_of, k=k, slug=ctx.slug)

    @tool("get_execution_events", "Read actual tool arguments/results/errors and email delivery evidence for a turn. Page through all events. Empty evidence is a visibility gap, not success.", {
        "turn_id": Annotated[str, "the reviewed turn id"],
        "offset": Annotated[int, "zero-based event offset"],
        "limit": Annotated[int, "page size, at most 20"],
    })
    async def get_execution_events(params: dict[str, Any]) -> dict[str, Any]:
        turn_id = str(params.get("turn_id", ""))
        if turn_id not in by_id:
            return _err("turn is outside this evaluation")
        if ctx.event_reader is None:
            return _ok({"available": False, "events": [], "total": 0})
        return _ok(ctx.event_reader(turn_id, offset=int(params.get("offset", 0)), limit=int(params.get("limit", 5))))

    @tool("submit_agent_feedback", "Submit the final per-agent feedback. Call exactly once, last. score is 1-5; classify each recall_miss. Its arguments are the fields of the AgentFeedback schema.", AgentFeedback.model_json_schema())
    async def submit_agent_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
        try:
            result.feedback = AgentFeedback.model_validate(feedback)
        except Exception as exc:
            return _err(f"invalid agent feedback, fix and resubmit: {exc}")
        return _ok({"accepted": True})

    tools = [get_turns, get_turn, get_briefing, search_memories, get_execution_events, submit_agent_feedback]
    server = create_sdk_mcp_server("nightly", tools=tools)
    return Toolset(
        mcp_servers={"nightly": server},
        allowed_tools=[f"mcp__nightly__{t.name}" for t in tools],
        result=result,
        handlers={t.name: t.handler for t in tools},
    )


def build_memory_toolset(ctx: ToolContext, agent_observations: list[dict[str, Any]]) -> Toolset:
    """Toolset for the reduce session. ``agent_observations`` are the
    memory-related notes each per-agent session surfaced (recall misses +
    any memory remarks), passed in so the analyst reduces over them."""
    result = MemoryToolResult()

    @tool("list_units", "List the knowledge units extracted on this day across all of the user's agents.", _LIST_UNITS_SCHEMA)
    async def list_units(params: dict[str, Any]) -> dict[str, Any]:
        limit: int = int(params.get("limit", 200))
        return _ok(await ctx.client.list_units(ctx.user_id, day=ctx.day, limit=limit))

    @tool("get_agent_observations", "Read the memory-related observations (recall misses, remarks) the per-agent analyses surfaced today. Reduce over these.", {})
    async def get_agent_observations(_params: dict[str, Any]) -> dict[str, Any]:
        return _ok({"observations": agent_observations})

    @tool("search_memories", "Search the user's memory store. mode=raw checks existence; mode=production replays ranking. Use to confirm duplicates/staleness before proposing an op.", _SEARCH_SCHEMA)
    async def search_memories(params: dict[str, Any]) -> dict[str, Any]:
        query: str = str(params.get("query") or "").strip()
        mode: str = str(params.get("mode") or "raw")
        as_of: str | None = str(params.get("as_of") or "") or None
        k: int = int(params.get("k", 10))
        if not query:
            return _err("query is required")
        return await _search(ctx, query, mode=mode, as_of=as_of, k=k, slug=None)

    @tool("submit_memory_feedback", "Submit the final memory-module feedback. Call exactly once, last. Each op (tombstone/merge/correct) is a reversible proposal a human approves. Its arguments are the fields of the MemoryFeedback schema.", MemoryFeedback.model_json_schema())
    async def submit_memory_feedback(feedback: dict[str, Any]) -> dict[str, Any]:
        try:
            result.feedback = MemoryFeedback.model_validate(feedback)
        except Exception as exc:
            return _err(f"invalid memory feedback, fix and resubmit: {exc}")
        return _ok({"accepted": True})

    tools = [list_units, get_agent_observations, search_memories, submit_memory_feedback]
    server = create_sdk_mcp_server("nightly_memory", tools=tools)
    return Toolset(
        mcp_servers={"nightly_memory": server},
        allowed_tools=[f"mcp__nightly_memory__{t.name}" for t in tools],
        result=result,
        handlers={t.name: t.handler for t in tools},
    )


__all__ = [
    "ToolContext",
    "AgentToolResult",
    "MemoryToolResult",
    "Toolset",
    "build_agent_toolset",
    "build_memory_toolset",
]
