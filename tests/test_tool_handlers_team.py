"""Unit tests for the team-family tools.

Each tool now takes its deps via ``__init__``:
- ``DelegateToTool(get_agent_fn | None)``
- ``CoordinateTool(get_agent_fn | None)``
- ``ListAgentsTool(registry | None)``
- ``AgentStatusTool(registry | None)``
- ``CreateAgentTool()`` (no deps — staging only)
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.tools import ToolContext
from telos.domain.tools import team as team_tools
from telos.domain.tools.team import (
    AgentStatusTool,
    CoordinateTool,
    CreateAgentTool,
    DelegateToTool,
    ListAgentsTool,
)

from tests._helpers import dispatch


def _ctx(
    tmp_path: Path,
    *,
    graph: DelegationGraph | None = None,
    on_event=None,
    user_id: str = "alice",
) -> ToolContext:
    return ToolContext(
        run_context=RunContext(
            user_id=user_id,
            delegation_graph=graph or DelegationGraph(),
        ),
        agent_slug="parent",
        workspace=tmp_path,
        storage_root=tmp_path,
        on_event=on_event,
    )


def _streaming_agent(text: str):
    """Build a fake agent whose ``astream`` yields a single text chunk."""
    agent = MagicMock()

    async def _astream(msg, ctx):
        yield "text", text

    agent.astream = _astream
    return agent


# ──────────────────────────────────────────────────────────────────────
# delegate_to
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delegate_to_returns_not_available_without_get_agent_fn(tmp_path):
    result = await dispatch(DelegateToTool(None), {"agent_slug": "bob", "message": "hi"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "not available" in result.text


@pytest.mark.asyncio
async def test_delegate_to_refuses_cycles(tmp_path):
    """If ``slug`` is already on the active chain, the tool must NOT
    call ``get_agent_fn`` (would deadlock on the sibling's stream lock)."""
    graph = DelegationGraph()
    get_agent_fn = AsyncMock(side_effect=AssertionError("get_agent_fn must not be called"))
    with graph.enter("bob"):
        result = await dispatch(DelegateToTool(get_agent_fn), {"agent_slug": "bob", "message": "hi"}, _ctx(tmp_path, graph=graph),
        )
    assert result.is_error is True
    assert "Refusing to delegate to 'bob'" in result.text
    assert "Cycles are not allowed" in result.text
    get_agent_fn.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_to_streams_sibling_response(tmp_path):
    sibling = _streaming_agent("hello back")
    get_agent_fn = AsyncMock(return_value=sibling)
    result = await dispatch(DelegateToTool(get_agent_fn), {"agent_slug": "bob", "message": "ping"}, _ctx(tmp_path),
    )
    assert result.is_error is False
    assert "[Response from bob]" in result.text
    assert "hello back" in result.text


@pytest.mark.asyncio
async def test_delegate_to_fires_on_event_for_text(tmp_path):
    """``on_event`` exists so the parent UI can render the delegation as it streams."""
    sibling = _streaming_agent("chunk")
    events: list[tuple[str, dict]] = []

    async def _record(kind, data):
        events.append((kind, data))

    await dispatch(DelegateToTool(AsyncMock(return_value=sibling)), {"agent_slug": "bob", "message": "x"}, _ctx(tmp_path, on_event=_record),
    )
    assert events == [("delegation_text", {"agent": "bob", "text": "chunk"})]


# ──────────────────────────────────────────────────────────────────────
# coordinate
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coordinate_refuses_when_any_slug_in_cycle(tmp_path):
    graph = DelegationGraph()
    get_agent_fn = AsyncMock(side_effect=AssertionError("must not be called"))
    with graph.enter("alice"):
        result = await dispatch(CoordinateTool(get_agent_fn), {"agent_slugs": ["bob", "alice"], "message": "hi"}, _ctx(tmp_path, graph=graph),
        )
    assert result.is_error is True
    assert "alice" in result.text


@pytest.mark.asyncio
async def test_coordinate_aggregates_responses(tmp_path):
    siblings = {
        "bob": _streaming_agent("bob says A"),
        "carol": _streaming_agent("carol says B"),
    }

    async def _get(uid, slug):
        return siblings[slug]

    result = await dispatch(CoordinateTool(_get), {"agent_slugs": ["bob", "carol"], "message": "x"}, _ctx(tmp_path),
    )
    assert result.is_error is False
    assert "## bob" in result.text
    assert "bob says A" in result.text
    assert "## carol" in result.text
    assert "carol says B" in result.text


@pytest.mark.asyncio
async def test_coordinate_returns_not_available_without_get_agent_fn(tmp_path):
    result = await dispatch(CoordinateTool(None), {"agent_slugs": ["bob"], "message": "x"}, _ctx(tmp_path),
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_coordinate_times_out_a_hung_sibling_without_stalling_others(tmp_path, monkeypatch):
    """One sibling that never finishes must fail alone — its slot is marked
    as timed out while the healthy siblings still return their responses,
    instead of the whole gather hanging on the coordinating turn."""
    monkeypatch.setattr(team_tools, "_COORDINATE_SIBLING_TIMEOUT_S", 0.05)

    def _hung_agent():
        agent = MagicMock()

        async def _astream(msg, ctx):
            await asyncio.sleep(3600)  # never resolves within the timeout
            yield "text", "unreachable"

        agent.astream = _astream
        return agent

    siblings = {"bob": _streaming_agent("bob ok"), "slow": _hung_agent()}

    async def _get(uid, slug):
        return siblings[slug]

    result = await asyncio.wait_for(
        dispatch(CoordinateTool(_get), {"agent_slugs": ["bob", "slow"], "message": "x"}, _ctx(tmp_path)),
        timeout=2.0,
    )
    assert result.is_error is False
    assert "## bob" in result.text
    assert "bob ok" in result.text
    assert "## slow (timed out)" in result.text


@pytest.mark.asyncio
async def test_coordinate_bounds_concurrency(tmp_path, monkeypatch):
    """Fan-out never runs more than ``_COORDINATE_MAX_CONCURRENCY`` sibling
    turns at once — a wide coordinate call must not open one turn per slug."""
    monkeypatch.setattr(team_tools, "_COORDINATE_MAX_CONCURRENCY", 2)

    live = 0
    peak = 0

    def _tracking_agent(name: str):
        agent = MagicMock()

        async def _astream(msg, ctx):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                await asyncio.sleep(0.02)
                yield "text", name
            finally:
                live -= 1

        agent.astream = _astream
        return agent

    slugs = [f"s{i}" for i in range(6)]
    siblings = {s: _tracking_agent(s) for s in slugs}

    async def _get(uid, slug):
        return siblings[slug]

    result = await dispatch(CoordinateTool(_get), {"agent_slugs": slugs, "message": "x"}, _ctx(tmp_path))
    assert result.is_error is False
    assert peak <= 2
    for s in slugs:
        assert f"## {s}" in result.text


@pytest.mark.asyncio
async def test_coordinate_dedupes_duplicate_slugs(tmp_path):
    """Duplicate slugs must collapse to one sibling turn — a second copy would
    serialize on the same agent's stream lock and burn its timeout waiting."""
    calls = 0

    def _counting_agent():
        agent = MagicMock()

        async def _astream(msg, ctx):
            nonlocal calls
            calls += 1
            yield "text", "bob ok"

        agent.astream = _astream
        return agent

    siblings = {"bob": _counting_agent(), "carol": _streaming_agent("carol ok")}

    async def _get(uid, slug):
        return siblings[slug]

    result = await dispatch(
        CoordinateTool(_get), {"agent_slugs": ["bob", "bob", "carol"], "message": "x"}, _ctx(tmp_path),
    )
    assert result.is_error is False
    assert calls == 1
    assert result.text.count("## bob") == 1
    assert "## carol" in result.text


@pytest.mark.asyncio
async def test_coordinate_timeout_closes_sibling_stream_promptly(tmp_path, monkeypatch):
    """When the timeout fires while the drain is parked between generator
    steps (here: awaiting ``on_event``), the sibling's stream must be closed
    deterministically — its teardown (``finally`` → browser close, stream-lock
    release) must have run by the time coordinate returns, not whenever GC
    finalizes the suspended generator."""
    monkeypatch.setattr(team_tools, "_COORDINATE_SIBLING_TIMEOUT_S", 0.05)
    closed = False

    def _agent_with_teardown():
        agent = MagicMock()

        async def _astream(msg, ctx):
            nonlocal closed
            try:
                yield "text", "first chunk"
                yield "text", "unreachable"
            finally:
                closed = True

        agent.astream = _astream
        return agent

    async def _get(uid, slug):
        return _agent_with_teardown()

    async def _hung_on_event(kind, data):
        await asyncio.sleep(3600)  # parks the drain between generator steps

    result = await asyncio.wait_for(
        dispatch(
            CoordinateTool(_get), {"agent_slugs": ["slow"], "message": "x"}, _ctx(tmp_path, on_event=_hung_on_event),
        ),
        timeout=2.0,
    )
    assert "## slow (timed out)" in result.text
    assert closed is True


# ──────────────────────────────────────────────────────────────────────
# list_agents / agent_status
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_agents_renders_user_roster(tmp_path):
    """``list_agents`` surfaces specialists only — backed by
    ``registry.list_specialists`` (NOT ``list_for_user``)."""
    registry = MagicMock()
    registry.list_specialists = MagicMock(return_value=[
        {"slug": "weather", "name": "Weather", "description": "describes weather"},
        {"slug": "finance", "name": "Finance", "description": "tracks spend"},
    ])
    result = await dispatch(ListAgentsTool(registry), {}, _ctx(tmp_path))
    assert result.is_error is False
    assert "**Weather** (weather)" in result.text
    assert "**Finance** (finance)" in result.text
    registry.list_specialists.assert_called_once_with("alice")


@pytest.mark.asyncio
async def test_list_agents_handles_empty_roster(tmp_path):
    registry = MagicMock()
    registry.list_specialists = MagicMock(return_value=[])
    result = await dispatch(ListAgentsTool(registry), {}, _ctx(tmp_path))
    assert "No specialist agents yet." in result.text


@pytest.mark.asyncio
async def test_list_agents_returns_not_available_without_registry(tmp_path):
    result = await dispatch(ListAgentsTool(None), {}, _ctx(tmp_path))
    assert result.is_error is True


@pytest.mark.asyncio
async def test_agent_status_unknown_slug_is_error(tmp_path):
    registry = MagicMock()
    registry.get = MagicMock(return_value=None)
    result = await dispatch(AgentStatusTool(registry), {"agent_slug": "missing"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "missing" in result.text


@pytest.mark.asyncio
async def test_agent_status_reads_history_db(tmp_path):
    from telos.log import ConversationLog

    log = ConversationLog(tmp_path / "alice" / "weather" / "history.db")
    log.append("hello", "hi", "sha1")
    registry = MagicMock()
    registry.get = MagicMock(return_value={
        "name": "Weather",
        "description": "x",
        "last_active": "2026-05-15",
    })

    result = await dispatch(AgentStatusTool(registry), {"agent_slug": "weather"}, _ctx(tmp_path))
    assert result.is_error is False
    assert "Agent: Weather" in result.text
    assert "Total turns: 1" in result.text
    assert "Last active: 2026-05-15" in result.text


# ──────────────────────────────────────────────────────────────────────
# create_agent — stages a pending request, no LLM inline
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_agent_stages_pending_payload(tmp_path):
    result = await dispatch(CreateAgentTool(), {"name": "Trip Planner", "goal": "plan trips"}, _ctx(tmp_path),
    )
    assert result.is_error is False
    payload = json.loads(result.text)
    assert payload == {"pending_agent": True, "name": "Trip Planner", "goal": "plan trips"}
