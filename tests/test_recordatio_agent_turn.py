"""Tests for ``Agent.astream`` driving a turn through Recordatio's agent API.

Recordatio mode is the only runtime (Phase 6 retired the legacy OpenAI-SSE
loop and its ``runtime_mode`` flag). Covers:

- The translated TaskEvents surface as ``(kind, data)`` tuples.
- The surrounding bookkeeping (``turn_id`` emit, history append, log
  persistence, usage emit) runs around the Recordatio turn.
- Briefing events persist their ``request_id`` for the recalled-memories
  panel.
- ``message_delta`` streaming reconciles with the authoritative
  ``message`` events so persisted history is delta-agnostic.
- ``AgentRuntime`` seeds Recordatio's AgentConfig unconditionally.
"""
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from telos import Agent, Goal
from telos.domain import DelegationGraph, RunContext
from telos.routines import Routine


def _ctx(user_id: str = "u") -> RunContext:
    return RunContext(user_id=user_id, delegation_graph=DelegationGraph())


def _make_agent(tmp_path) -> Agent:
    goal = Goal(name="t", description="d")
    return Agent(goal, user_id="u", storage_root=tmp_path)


# ──────────────────────────────────────────────────────────────────────
# Translation reaches the caller verbatim
# ──────────────────────────────────────────────────────────────────────


async def _fake_recordatio_stream(*_args, **_kwargs) -> AsyncIterator[tuple[str, Any]]:
    """Yield the same (kind, data) tuples the real translator produces."""
    yield "text", "hello "
    yield "text", "world"


@pytest.mark.asyncio
async def test_recordatio_turn_yields_text_and_persists(tmp_path):
    agent = _make_agent(tmp_path)

    with patch(
        "telos.services.agent_runtime_v2.stream_recordatio_agent_turn",
        new=_fake_recordatio_stream,
    ):
        events = []
        async for kind, data in agent.astream("hi there", _ctx()):
            events.append((kind, data))

    # turn_id must come first (bookkeeping wraps the Recordatio turn).
    assert events[0][0] == "turn_id"

    text_chunks = [d for k, d in events if k == "text"]
    assert text_chunks == ["hello ", "world"]

    # usage event still emitted at the end (post-loop bookkeeping ran).
    usage_events = [d for k, d in events if k == "usage"]
    assert len(usage_events) == 1
    assert "turn_cost" in usage_events[0]

    # And persistence ran.
    turns = agent._log.recent_turns()
    assert len(turns) == 1
    assert turns[0].user_message == "hi there"
    assert turns[0].assistant_response == "hello world"


@pytest.mark.asyncio
async def test_recordatio_turn_forwards_thinking_but_never_persists_it(tmp_path):
    """``("thinking", ...)`` tuples reach the caller (the chat route maps
    them to a transient SSE frame) but are excluded from the persisted
    turn text and the in-memory history — thinking is ephemeral UI."""
    agent = _make_agent(tmp_path)

    async def fake_stream(*_a, **_kw) -> AsyncIterator[tuple[str, Any]]:
        yield "thinking", "pondering the request"
        yield "thinking", " carefully"
        yield "text", "the answer"

    with patch(
        "telos.services.agent_runtime_v2.stream_recordatio_agent_turn",
        new=fake_stream,
    ):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    thinking_chunks = [d for k, d in events if k == "thinking"]
    assert thinking_chunks == ["pondering the request", " carefully"]

    # Persisted turn text is ONLY the text chunks.
    turns = agent._log.recent_turns()
    assert len(turns) == 1
    assert turns[0].assistant_response == "the answer"
    assert "pondering" not in turns[0].assistant_response

    # In-memory history (next turn's context) is equally clean.
    assistant_msgs = [m for m in agent._history if m["role"] == "assistant"]
    assert assistant_msgs == [{"role": "assistant", "content": "the answer"}]


@pytest.mark.asyncio
async def test_recordatio_turn_no_thinking_turn_unchanged(tmp_path):
    """A turn without thinking events persists and streams exactly as
    before — no spurious ``thinking`` frames."""
    agent = _make_agent(tmp_path)

    with patch(
        "telos.services.agent_runtime_v2.stream_recordatio_agent_turn",
        new=_fake_recordatio_stream,
    ):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert all(k != "thinking" for k, _ in events)
    turns = agent._log.recent_turns()
    assert turns[0].assistant_response == "hello world"


@pytest.mark.asyncio
async def test_recordatio_turn_surfaces_tool_call_and_result(tmp_path):
    agent = _make_agent(tmp_path)

    async def fake_stream(*_a, **_kw) -> AsyncIterator[tuple[str, Any]]:
        yield "tool_call", {"name": "web_search", "input": {"q": "x"}}
        yield "tool_result", {"name": "", "is_error": False}
        yield "text", "done"

    with patch(
        "telos.services.agent_runtime_v2.stream_recordatio_agent_turn",
        new=fake_stream,
    ):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    kinds = [k for k, _ in events]
    assert "tool_call" in kinds
    assert "tool_result" in kinds


# ──────────────────────────────────────────────────────────────────────
# Briefing event → recalled-memories panel (#43)
# ──────────────────────────────────────────────────────────────────────


def _briefing_payload(request_id: str | None = "recordatio-agent-deadbeef0001") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "units": [{"type": "semantic", "content": "user prefers tea"}],
        "entities": [],
        "relations": [],
        "topic_names": [],
    }
    if request_id is not None:
        payload["request_id"] = request_id
    return payload


def _fake_task_events(frames: list[tuple[str, dict]]):
    """Build a stand-in for ``stream_task_events`` (the wire layer) so the
    real ``_translate`` table and ``Agent._astream_body`` capture run."""
    async def _stream(*_a, **_kw) -> AsyncIterator[tuple[str, dict]]:
        for frame in frames:
            yield frame
    return _stream


@pytest.mark.asyncio
async def test_briefing_persists_request_id_and_surfaces_event(tmp_path):
    """A ``briefing`` TaskEvent must reach the SPA as the
    ``agentmem.briefing`` event (before the text — the button keys on it)
    AND persist its request_id on the turn row — the exact store/keying
    the recalled-memories endpoint reads."""
    agent = _make_agent(tmp_path)

    fake = _fake_task_events([
        ("briefing", _briefing_payload()),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    kinds = [k for k, _ in events]
    assert "agentmem.briefing" in kinds
    # Briefing arrives before the first text chunk.
    assert kinds.index("agentmem.briefing") < kinds.index("text")
    briefing = next(d for k, d in events if k == "agentmem.briefing")
    assert briefing["type"] == "agentmem.briefing"
    assert briefing["units"][0]["content"] == "user prefers tea"

    # request_id on the turn row, which
    # ``GET /api/agents/{slug}/turns/{turn_id}/memories`` resolves via
    # ``ConversationLog.get_request_id`` → Recordatio /v1/briefing-events/{id}.
    turns = agent._log.recent_turns()
    assert len(turns) == 1
    assert turns[0].request_id == "recordatio-agent-deadbeef0001"
    assert agent._log.get_request_id(turns[0].turn_id) == "recordatio-agent-deadbeef0001"


@pytest.mark.asyncio
async def test_no_briefing_leaves_request_id_null(tmp_path):
    agent = _make_agent(tmp_path)

    fake = _fake_task_events([
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert all(k != "agentmem.briefing" for k, _ in events)
    turns = agent._log.recent_turns()
    assert turns[0].request_id is None


@pytest.mark.asyncio
async def test_briefing_without_request_id_still_surfaces(tmp_path):
    """Forward-compat: a briefing payload missing ``request_id`` still
    drives the live button (event surfaces) but leaves the turn row null —
    the panel then renders its explicit ``no_request_id`` empty state."""
    agent = _make_agent(tmp_path)

    fake = _fake_task_events([
        ("briefing", _briefing_payload(request_id=None)),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert any(k == "agentmem.briefing" for k, _ in events)
    assert agent._log.recent_turns()[0].request_id is None


@pytest.mark.asyncio
async def test_malformed_briefing_dropped_turn_unaffected(tmp_path):
    """A malformed briefing payload is logged + dropped; the turn itself
    completes and persists normally with no request_id."""
    agent = _make_agent(tmp_path)

    fake = _fake_task_events([
        ("briefing", {"request_id": "x", "units": 42}),
        ("message", {"text": "still fine"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert all(k != "agentmem.briefing" for k, _ in events)
    turns = agent._log.recent_turns()
    assert turns[0].assistant_response == "still fine"
    assert turns[0].request_id is None


@pytest.mark.asyncio
async def test_first_briefing_request_id_wins(tmp_path):
    """The first briefing-bearing event is the one whose request_id gets
    persisted."""
    agent = _make_agent(tmp_path)

    fake = _fake_task_events([
        ("briefing", _briefing_payload("recordatio-agent-first")),
        ("briefing", _briefing_payload("recordatio-agent-second")),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        async for _ in agent.astream("hi", _ctx()):
            pass

    assert agent._log.recent_turns()[0].request_id == "recordatio-agent-first"


# ──────────────────────────────────────────────────────────────────────
# message_delta streaming → persisted history parity
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stored_history_identical_with_and_without_deltas(tmp_path):
    """The turn's persisted assistant text must come out identical whether
    Recordatio streamed token deltas (new producer) or only the block-level
    ``message`` events (older Recordatio). Runs the REAL ``_translate`` table
    plus ``Agent._astream_body``'s ``full_response`` accumulation and the
    shared post-loop persistence."""
    with_deltas = [
        ("message_delta", {"text": "hello "}),
        ("message_delta", {"text": "wor"}),
        ("message", {"text": "hello world"}),  # deltas missed the tail "ld"
        ("tool_call", {"call_id": "c1", "name": "web_search", "input": {}}),
        ("tool_result", {"call_id": "c1", "is_error": False}),
        ("message_delta", {"text": " — done"}),
        ("message", {"text": " — done"}),
        ("completed", {}),
    ]
    without_deltas = [
        ("message", {"text": "hello world"}),
        ("tool_call", {"call_id": "c1", "name": "web_search", "input": {}}),
        ("tool_result", {"call_id": "c1", "is_error": False}),
        ("message", {"text": " — done"}),
        ("completed", {}),
    ]

    responses: dict[str, str] = {}
    streamed_text: dict[str, list[str]] = {}
    for label, frames in (("deltas", with_deltas), ("plain", without_deltas)):
        agent = _make_agent(tmp_path / label)
        with patch(
            "telos.services.agent_runtime_v2.stream_task_events",
            new=_fake_task_events(frames),
        ):
            chunks = []
            async for kind, data in agent.astream("hi", _ctx()):
                if kind == "text":
                    chunks.append(data)
        streamed_text[label] = chunks
        turns = agent._log.recent_turns()
        assert len(turns) == 1
        responses[label] = turns[0].assistant_response

    assert responses["deltas"] == responses["plain"] == "hello world\n\n — done"
    # In-memory history (what the next turn's context is built from) agrees.
    # The delta path streamed MORE chunks (live tokens), but their
    # concatenation is the same authoritative text, emitted exactly once.
    assert len(streamed_text["deltas"]) > len(streamed_text["plain"])
    assert "".join(streamed_text["deltas"]) == "".join(streamed_text["plain"])


@pytest.mark.asyncio
async def test_delta_chunks_reach_caller_incrementally(tmp_path):
    """End-to-end through ``Agent.astream``: each ``message_delta`` becomes
    its own ("text", chunk) event — the shape the chat SSE route forwards
    frame-by-frame — and the authoritative ``message`` adds nothing when
    the deltas covered it."""
    agent = _make_agent(tmp_path)

    fake = _fake_task_events([
        ("message_delta", {"text": "a"}),
        ("message_delta", {"text": "b"}),
        ("message_delta", {"text": "c"}),
        ("message", {"text": "abc"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        chunks = []
        async for kind, data in agent.astream("hi", _ctx()):
            if kind == "text":
                chunks.append(data)

    assert chunks == ["a", "b", "c"]
    assert agent._log.recent_turns()[0].assistant_response == "abc"


# ──────────────────────────────────────────────────────────────────────
# create_routine tool_result → pending_routine frame (the chat pill)
# ──────────────────────────────────────────────────────────────────────
#
# On the Recordatio path ``create_routine`` runs inside the MCP host and the
# stream only carries a generic tool_result, so ``Agent._astream_body``
# recovers the staged enabled=False row from the RoutineStore and re-emits
# the ``pending_routine`` frame the SPA's confirmation pill renders.


def _stage_pending_routine(agent: Agent, **over: Any) -> Routine:
    """Stage an enabled=False row the way the MCP create_routine tool does."""
    kwargs: dict[str, Any] = dict(
        user_id=agent._user_id,
        agent_slug=agent._slug,
        description="daily news digest",
        interval_seconds=86400,
        time_of_day="09:00",
        one_shot=False,
        notify_email=True,
        enabled=False,
    )
    kwargs.update(over)
    return agent._routine_store.create(**kwargs)


def _create_routine_frames(
    *, is_error: bool = False, name: str = "mcp__telos__create_routine",
) -> list[tuple[str, dict]]:
    # Recordatio reports telos tools under their MCP address by default.
    return [
        ("tool_call", {"call_id": "c1", "name": name, "input": {}}),
        ("tool_result", {"call_id": "c1", "is_error": is_error}),
        ("message", {"text": "proposed!"}),
        ("completed", {}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["mcp__telos__create_routine", "create_routine"])
async def test_create_routine_result_emits_pending_routine_frame(tmp_path, tool_name):
    """A successful create_routine tool_result must surface the staged
    proposal as a ``pending_routine`` event with the exact payload shape
    the SPA's ``PendingRoutineEvent`` / RoutinePill expects (the legacy
    in-process loop's shape, field-for-field) — whether Recordatio reports
    the tool under its MCP address or its bare name."""
    agent = _make_agent(tmp_path)
    routine = _stage_pending_routine(agent)

    fake = _fake_task_events(_create_routine_frames(name=tool_name))
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("set up a digest", _ctx()):
            events.append((kind, data))

    pending = [d for k, d in events if k == "pending_routine"]
    assert pending == [{
        "id": routine.id,
        "description": "daily news digest",
        "interval_seconds": 86400,
        "time_of_day": "09:00",
        "one_shot": False,
        "notify_email": True,
    }]
    # The pill frame follows the tool_result it confirms (the SPA appends
    # it to the in-flight assistant bubble).
    kinds = [k for k, _ in events]
    assert kinds.index("pending_routine") > kinds.index("tool_result")
    # The SPA sees the bare telos tool name on both frames.
    assert [d["name"] for k, d in events if k in ("tool_call", "tool_result")] == ["create_routine", "create_routine"]


@pytest.mark.asyncio
async def test_failed_create_routine_emits_no_pending_frame(tmp_path):
    """``is_error: true`` on the create_routine result must not produce a
    pill — even if an unrelated pending row exists in the store."""
    agent = _make_agent(tmp_path)
    _stage_pending_routine(agent)

    fake = _fake_task_events(_create_routine_frames(is_error=True))
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert all(k != "pending_routine" for k, _ in events)


@pytest.mark.asyncio
async def test_other_tool_results_emit_no_pending_frame(tmp_path):
    """Only create_routine results trigger the store scan — a pending row
    must not piggyback on an unrelated tool's result."""
    agent = _make_agent(tmp_path)
    _stage_pending_routine(agent)

    fake = _fake_task_events([
        ("tool_call", {"call_id": "c1", "name": "web_search", "input": {"q": "x"}}),
        ("tool_result", {"call_id": "c1", "is_error": False}),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert all(k != "pending_routine" for k, _ in events)


@pytest.mark.asyncio
async def test_stale_pending_routine_not_resurrected_in_chat(tmp_path):
    """Pending rows older than the recency window (orphans from earlier
    sessions) stay out of chat — the settings-page routines panel is
    where they get resolved."""
    agent = _make_agent(tmp_path)
    routine = _stage_pending_routine(agent)
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with agent._routine_store._db.connect() as conn:
        conn.execute(
            "UPDATE routines SET created_at=? WHERE id=?", (stale, routine.id),
        )

    fake = _fake_task_events(_create_routine_frames())
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    assert all(k != "pending_routine" for k, _ in events)


@pytest.mark.asyncio
async def test_two_creates_in_one_turn_emit_each_proposal_once(tmp_path):
    """Each create_routine result re-scans the store; the per-turn
    emitted-id set must keep a proposal from rendering as two pills."""
    agent = _make_agent(tmp_path)
    first = _stage_pending_routine(agent, description="first")
    second = _stage_pending_routine(agent, description="second")

    fake = _fake_task_events([
        ("tool_call", {"call_id": "c1", "name": "create_routine", "input": {}}),
        ("tool_result", {"call_id": "c1", "is_error": False}),
        ("tool_call", {"call_id": "c2", "name": "create_routine", "input": {}}),
        ("tool_result", {"call_id": "c2", "is_error": False}),
        ("message", {"text": "both proposed"}),
        ("completed", {}),
    ])
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        events = []
        async for kind, data in agent.astream("hi", _ctx()):
            events.append((kind, data))

    pending_ids = [d["id"] for k, d in events if k == "pending_routine"]
    assert sorted(pending_ids) == sorted([first.id, second.id])


# ──────────────────────────────────────────────────────────────────────
# AgentRuntime: Recordatio config seed is unconditional
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runtime_seeds_recordatio_config_unconditionally(tmp_path):
    """Every agent build seeds Recordatio's AgentConfig — there is no flag
    or per-user allowlist gating the Recordatio path any more."""
    from telos.api.registry import AgentRegistry
    from telos.routines import RoutineStore
    from telos.services.agent_runtime import AgentRuntime

    registry = AgentRegistry(tmp_path / "reg.db")
    routine_store = RoutineStore(tmp_path / "routines.db")
    registry.create("alice", "Weather", "desc", "system", None)

    runtime = AgentRuntime(
        registry=registry,
        storage_root=tmp_path,
        recordatio_url="http://recordatio.test",
        model="haiku",
        chat_api_key=None,
        improver_api_key=None,
        utility_model="haiku",
        max_history_turns=10,
        max_context_tokens=1000,
        routine_store=routine_store,
        telos_mcp_url="http://telos.test",
    )

    seed_spy = AsyncMock(return_value=False)
    with patch(
        "telos.services.agent_runtime.seed_agent_config_if_missing",
        new=seed_spy,
    ):
        await runtime.get("alice", "weather")
    seed_spy.assert_called_once()
    assert seed_spy.call_args.kwargs["user_id"] == "alice"
    assert seed_spy.call_args.kwargs["slug"] == "weather"
