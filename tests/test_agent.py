import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from telos import Agent, Goal
from telos.agent import _execute_tool
from telos.domain import DelegationGraph, RunContext
from telos.log import ConversationLog
from telos.signals import detect_signals
from telos.tools import tool_repository


def _ctx(user_id: str = "test_user", **overrides) -> RunContext:
    """Build a fresh ``RunContext`` for tests that don't care about delegation chains."""
    return RunContext(user_id=user_id, delegation_graph=DelegationGraph(), **overrides)

# ──────────────────────────────────────────────────────────────────────
# Goal tests
# ──────────────────────────────────────────────────────────────────────

def test_goal_generates_system_prompt():
    goal = Goal(name="job hunt", description="Help me find a job.")
    assert "job hunt" in goal.system_prompt
    assert "Help me find a job." in goal.system_prompt


def test_goal_accepts_custom_system_prompt():
    goal = Goal(name="x", description="y", system_prompt="Custom prompt.")
    assert goal.system_prompt == "Custom prompt."


# ──────────────────────────────────────────────────────────────────────
# ConversationLog tests
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_log(tmp_path):
    return ConversationLog(tmp_path / "history.db")


def test_log_append_and_retrieve(tmp_log):
    tmp_log.append("hello", "hi there", "sha001")
    turns = tmp_log.recent_turns()
    assert len(turns) == 1
    assert turns[0].user_message == "hello"
    assert turns[0].assistant_response == "hi there"
    assert turns[0].prompt_sha == "sha001"
    assert turns[0].corrected is False


def test_log_append_persists_trace_id(tmp_log):
    tmp_log.append("hello", "hi there", "sha001", trace_id="trace-123")
    turns = tmp_log.recent_turns()
    assert turns[0].trace_id == "trace-123"


def test_log_signal_update(tmp_log):
    turn_id = tmp_log.append("hello", "hi", "sha001")
    tmp_log.update_signals(turn_id, corrected=True, followed_up=False, ignored=False)
    turns = tmp_log.recent_turns()
    assert turns[0].corrected is True


def test_log_count(tmp_log):
    assert tmp_log.count() == 0
    tmp_log.append("a", "b", "sha1")
    tmp_log.append("c", "d", "sha1")
    assert tmp_log.count() == 2


def test_log_signal_summary(tmp_log):
    t1 = tmp_log.append("q1", "a1", "s1")
    t2 = tmp_log.append("q2", "a2", "s1")
    tmp_log.update_signals(t1, corrected=True, followed_up=False, ignored=False)
    tmp_log.update_signals(t2, corrected=False, followed_up=True, ignored=False)
    summary = tmp_log.signal_summary()
    assert summary["total_turns"] == 2
    assert summary["correction_rate"] == 0.5
    assert summary["follow_up_rate"] == 0.5


def test_log_save_and_deploy_prompt(tmp_log):
    sha = tmp_log.save_prompt("my prompt")
    assert sha == hashlib.sha256(b"my prompt").hexdigest()[:12]
    history = tmp_log.prompt_history()
    assert any(p["sha"] == sha for p in history)


# ──────────────────────────────────────────────────────────────────────
# Signal detection tests
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg,expected_corrected", [
    ("actually that's wrong", True),
    ("no that's not right", True),
    ("what time is it?", False),
    ("tell me more", False),
])
def test_detect_correction(msg, expected_corrected):
    corrected, _, _ = detect_signals(msg)
    assert corrected == expected_corrected


@pytest.mark.parametrize("msg,expected_followed_up", [
    ("what else should I know?", True),
    ("can you also explain the second part?", True),
    ("ok", False),
])
def test_detect_follow_up(msg, expected_followed_up):
    _, followed_up, _ = detect_signals(msg)
    assert followed_up == expected_followed_up


@pytest.mark.parametrize("msg,expected_ignored", [
    ("ok", True),
    ("thanks", True),
    ("that's really interesting, tell me more!", False),
])
def test_detect_ignored(msg, expected_ignored):
    _, _, ignored = detect_signals(msg)
    assert ignored == expected_ignored


# ──────────────────────────────────────────────────────────────────────
# Agent tests — every chat turn is driven by Recordatio's agent API; the
# wire layer (``stream_task_events``) is faked with TaskEvent frames so
# the real translator + ``Agent._astream_body`` bookkeeping run.
# ──────────────────────────────────────────────────────────────────────

def _fake_task_events(frames: list[tuple[str, dict]]):
    """Stand-in for ``stream_task_events`` yielding the given frames."""
    async def _stream(*_a, **_kw):
        for frame in frames:
            yield frame
    return _stream


def _frames(chunks: list[str], usage: dict | None = None) -> list[tuple[str, dict]]:
    """TaskEvent frames for a simple text turn, optionally with usage on
    the terminal ``completed`` event (mirrors how Recordatio reports cost)."""
    frames: list[tuple[str, dict]] = [("message", {"text": t}) for t in chunks]
    frames.append(("completed", {"usage": usage} if usage is not None else {}))
    return frames


def make_agent(tmp_path) -> Agent:
    goal = Goal(name="test goal", description="A test goal.")
    return Agent(goal, user_id="test_user", storage_root=tmp_path)


async def _collect(agent: Agent, message: str, frames: list[tuple[str, dict]]) -> str:
    with patch(
        "telos.services.agent_runtime_v2.stream_task_events",
        new=_fake_task_events(frames),
    ):
        return "".join([data async for kind, data in agent.astream(message, _ctx()) if kind == "text"])


@pytest.mark.asyncio
async def test_chat_returns_response(tmp_path):
    agent = make_agent(tmp_path)
    response = await _collect(agent, "Hi", _frames(["Hello", " world"]))
    assert response == "Hello\n\n world"


def test_live_recordatio_config_none_without_base(tmp_path):
    # Direct construction (no base config) ⇒ nothing to send; Recordatio uses
    # its stored config.
    assert make_agent(tmp_path)._live_recordatio_config() is None


def test_live_recordatio_config_merges_live_prompt(tmp_path):
    # The base config's system_prompt is overridden by the agent's LIVE
    # prompt each turn, and the base dict is not mutated.
    base = {
        "model": "m",
        "allowed_tools": ["mcp__telos__web_search"],
        "system_prompt": "SEED prompt (stale)",
    }
    agent = Agent(
        Goal(name="g", description="d"),
        user_id="u", storage_root=tmp_path, recordatio_config=base,
    )
    cfg = agent._live_recordatio_config()
    assert cfg["allowed_tools"] == ["mcp__telos__web_search"]
    assert cfg["system_prompt"] == agent._system_prompt
    assert base["system_prompt"] == "SEED prompt (stale)"  # not mutated


def test_mcp_turn_headers_do_not_mutate_provider_configuration(tmp_path):
    base = {"mcp_servers": [{"name": "telos", "url": "http://host/mcp/u/a", "headers": {"Authorization": "Bearer scoped"}},
                            {"name": "other", "headers": None}], "allowed_tools": ["mcp__telos__browser_open"]}
    agent = Agent(Goal(name="g", description="d"), user_id="u", storage_root=tmp_path, recordatio_config=base)
    first = agent._live_recordatio_config(turn_id="first")
    second = agent._live_recordatio_config(turn_id="second")
    assert first["mcp_servers"][0]["headers"]["X-Telos-Turn-Id"] == "first"
    assert second["mcp_servers"][0]["headers"]["X-Telos-Turn-Id"] == "second"
    assert first["mcp_servers"][0]["headers"]["Authorization"] == "Bearer scoped"
    assert "X-Telos-Turn-Id" not in base["mcp_servers"][0]["headers"]
    assert first["mcp_servers"][1]["headers"] is None
    assert first["allowed_tools"] == base["allowed_tools"]


@pytest.mark.asyncio
async def test_chat_persists_to_log(tmp_path):
    agent = make_agent(tmp_path)
    await _collect(agent, "Hello", _frames(["Hi"]))
    assert agent._log.count() == 1


@pytest.mark.asyncio
async def test_chat_persists_context_trace_id(tmp_path):
    agent = make_agent(tmp_path)

    with patch(
        "telos.services.agent_runtime_v2.stream_task_events",
        new=_fake_task_events(_frames(["Hi"])),
    ):
        async for _kind, _data in agent.astream(
            "Hello",
            _ctx(correlation_id="trace-from-context"),
        ):
            pass

    turns = agent._log.recent_turns()
    assert turns[0].trace_id == "trace-from-context"


@pytest.mark.asyncio
async def test_signals_extracted_on_second_turn(tmp_path):
    agent = make_agent(tmp_path)
    await _collect(agent, "First message", _frames(["response1"]))
    await _collect(agent, "actually that was wrong", _frames(["response2"]))
    turns = agent._log.recent_turns(10)
    assert turns[0].corrected is True


def test_reset_clears_history(tmp_path):
    agent = make_agent(tmp_path)
    agent._history = [{"role": "user", "content": "Hi"}]
    agent.reset()
    assert agent.history == []


def test_workspace_is_per_agent(tmp_path):
    """Sibling agents owned by the same user must NOT share a workspace
    directory — otherwise scratch files written by one specialist would
    be readable by another (see #79)."""
    user = "alice"
    a = Agent(Goal(name="weather", description="x"), user_id=user, storage_root=tmp_path)
    b = Agent(Goal(name="finance", description="y"), user_id=user, storage_root=tmp_path)
    assert a.workspace != b.workspace
    assert a.workspace == tmp_path / user / "weather" / "workspace"
    assert b.workspace == tmp_path / user / "finance" / "workspace"


# Note: pre-Phase-5, ``Agent`` itself owned the improvement counter and
# fired ``asyncio.create_task(_run_improver())`` on every Nth turn. Phase
# 5D moved that logic into ``ImprovementService`` — see
# ``tests/test_improvement_service.py`` for the migrated coverage of
# trigger semantics. ``Agent`` no longer schedules anything; it only
# publishes a ``TurnComplete`` on the (optional) event bus.


def test_approve_updates_system_prompt(tmp_path):
    agent = make_agent(tmp_path)
    new_prompt = "An improved system prompt."
    from telos.improver import CandidatePrompt
    candidate = CandidatePrompt(sha="abc123", content=new_prompt, rationale="better", score_delta=0.3)
    agent._approval.stage(candidate)
    assert agent.has_pending_improvement

    agent.approve()
    assert agent._system_prompt == new_prompt
    assert not agent.has_pending_improvement


def test_rollback(tmp_path):
    agent = make_agent(tmp_path)
    old_prompt = agent._system_prompt
    old_sha = agent._log.save_prompt(old_prompt)
    agent._system_prompt = "New prompt"
    agent.rollback(old_sha)
    assert agent._system_prompt == old_prompt


def test_send_email_tool_schema_registered():
    schema = tool_repository.get_schemas(["send_email"])["send_email"]
    fn = schema["function"]
    assert fn["name"] == "send_email"
    assert fn["parameters"]["required"] == ["to", "subject", "body"]


@pytest.mark.asyncio
async def test_send_email_tool_uses_configured_sender(tmp_path):
    from telos.domain.tools import SendEmailTool

    calls = []

    def sender(to: str, subject: str, body: str) -> None:
        calls.append((to, subject, body))

    dispatcher: dict = {}
    _t = SendEmailTool(sender)
    dispatcher[_t.name] = _t

    result, is_error, images = await _execute_tool(
        "send_email",
        {"to": "a@example.com", "subject": "Hello", "body": "Body"},
        dispatcher,
        user_id="u",
        agent_slug="x",
        workspace=tmp_path,
        storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )

    assert is_error is False
    assert images is None
    assert result == "Email sent to a@example.com."
    assert calls == [("a@example.com", "Hello", "Body")]


@pytest.mark.asyncio
async def test_send_email_tool_errors_without_sender(tmp_path):
    from telos.domain.tools import SendEmailTool

    dispatcher: dict = {}
    _t = SendEmailTool(None)
    dispatcher[_t.name] = _t

    result, is_error, _ = await _execute_tool(
        "send_email",
        {"to": "a@example.com", "subject": "Hello", "body": "Body"},
        dispatcher,
        user_id="u",
        agent_slug="x",
        workspace=tmp_path,
        storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )

    assert is_error is True
    assert "not configured" in result


# ──────────────────────────────────────────────────────────────────────
# Peer coordination — specialists can use list_agents/agent_status and
# coordinate (consult peers), but cannot delegate full tasks or hire.
# Only the housekeeper has delegate_to + create_agent.
# ──────────────────────────────────────────────────────────────────────

def test_coordination_tools_available_to_specialists():
    names = set(tool_repository.get_schemas(None, include_housekeeper=False))
    assert "list_agents" in names
    assert "agent_status" in names
    assert "coordinate" in names


def test_delegate_to_is_housekeeper_only():
    spec_names = set(tool_repository.get_schemas(None, include_housekeeper=False))
    hk_names = set(tool_repository.get_schemas(None, include_housekeeper=True))
    assert "delegate_to" not in spec_names
    assert "delegate_to" in hk_names


def test_create_agent_is_housekeeper_only():
    spec_names = set(tool_repository.get_schemas(None, include_housekeeper=False))
    hk_names = set(tool_repository.get_schemas(None, include_housekeeper=True))
    assert "create_agent" not in spec_names
    assert "create_agent" in hk_names


@pytest.mark.asyncio
async def test_coordinate_refuses_cycle_without_invoking_get_agent(tmp_path):
    calls = []

    async def fake_get_agent(user_id, slug):
        calls.append(slug)
        raise AssertionError("get_agent_fn must not be called on a cycle")

    from telos.domain.tools import CoordinateTool

    dispatcher: dict = {}
    _t = CoordinateTool(fake_get_agent)
    dispatcher[_t.name] = _t

    graph = DelegationGraph()
    with graph.enter("alice"):
        result, is_error, _ = await _execute_tool(
            "coordinate",
            {"agent_slugs": ["bob", "alice"], "message": "hi"},
            dispatcher,
            user_id="u",
            agent_slug="x",
            workspace=tmp_path,
            storage_root=tmp_path,
            delegation_graph=graph,
        )

    assert is_error is True
    assert "alice" in result
    assert calls == []


# ──────────────────────────────────────────────────────────────────────
# recordatio_extraction cost recording (piggybacked on the ``completed``
# TaskEvent's usage block)
# ──────────────────────────────────────────────────────────────────────

def _ext_rows(agent: Agent) -> list[tuple]:
    """Return raw rows from the agent's cost_events table for recordatio_extraction."""
    with agent._costs._connect() as conn:
        return conn.execute(
            "SELECT source, model, prompt_tokens, completion_tokens, cost_usd, turn_id "
            "FROM cost_events WHERE source = 'recordatio_extraction'"
        ).fetchall()


@pytest.mark.asyncio
async def test_extraction_cost_recorded_when_present(tmp_path):
    agent = make_agent(tmp_path)
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": 0.0034,
        "cost_usd_extraction": 0.0007,
        "extraction_input_tokens": 800,
        "extraction_output_tokens": 200,
        "extraction_calls": 1,
        "extraction_model": "claude-haiku-4-5-20251001",
    }
    await _collect(agent, "Hi", _frames(["Hello"], usage))

    rows = _ext_rows(agent)
    assert len(rows) == 1
    source, model, in_tok, out_tok, cost, turn_id = rows[0]
    assert source == "recordatio_extraction"
    assert model == "claude-haiku-4-5-20251001"
    assert in_tok == 800
    assert out_tok == 200
    assert cost == pytest.approx(0.0007)
    # Extraction reports prior turns' work — not tied to this turn.
    assert turn_id is None


@pytest.mark.asyncio
async def test_extraction_cost_absent_when_fields_missing(tmp_path):
    agent = make_agent(tmp_path)
    # First-chat shape: usage block is present but has no extraction fields.
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": 0.0034,
    }
    await _collect(agent, "Hi", _frames(["Hello"], usage))

    assert _ext_rows(agent) == []
    # And chat is still recorded.
    s = agent._costs.cost_summary()
    assert s["by_source"]["chat"]["calls"] == 1


@pytest.mark.asyncio
async def test_extraction_cost_zero_does_not_record(tmp_path):
    agent = make_agent(tmp_path)
    # Field present but zero — must not produce a row.
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": 0.0034,
        "cost_usd_extraction": 0.0,
        "extraction_input_tokens": 0,
        "extraction_output_tokens": 0,
        "extraction_calls": 0,
        "extraction_model": "claude-haiku-4-5",
    }
    await _collect(agent, "Hi", _frames(["Hello"], usage))

    assert _ext_rows(agent) == []


@pytest.mark.asyncio
async def test_extraction_cost_multiple_calls_summed_into_single_row(tmp_path):
    agent = make_agent(tmp_path)
    # Recordatio sums multiple back-to-back extractions before reporting them
    # on the next chat response. We record this as a single row.
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": 0.0034,
        "cost_usd_extraction": 0.0021,
        "extraction_input_tokens": 2400,
        "extraction_output_tokens": 600,
        "extraction_calls": 3,
        "extraction_model": "claude-haiku-4-5",
    }
    await _collect(agent, "Hi", _frames(["Hello"], usage))

    rows = _ext_rows(agent)
    assert len(rows) == 1
    _, _, in_tok, out_tok, cost, _ = rows[0]
    assert in_tok == 2400
    assert out_tok == 600
    assert cost == pytest.approx(0.0021)


@pytest.mark.asyncio
async def test_chat_cost_uses_recordatio_cost_usd_when_present(tmp_path):
    """Recordatio's ``cost_usd`` (on the ``completed`` event's usage block)
    must win over the local price table — model-specific pricing and
    cache-hit discounts are reflected there, and an unknown model would
    otherwise fall back to a 0.0 lookup."""
    agent = make_agent(tmp_path)
    agent.model = "anthropic/claude-sonnet-4-6"
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "total_tokens": 160,
        "cost_usd": 0.0042,
    }
    await _collect(agent, "Hi", _frames(["Hello"], usage))

    s = agent._costs.cost_summary()
    chat = s["by_source"]["chat"]
    assert chat["calls"] == 1
    assert chat["total_usd"] == pytest.approx(0.0042)


@pytest.mark.asyncio
async def test_extraction_cost_missing_token_fields_defaults_to_zero(tmp_path):
    agent = make_agent(tmp_path)
    # Defensive: cost present but token fields missing — should not blow up,
    # should record the row with zero tokens but the real cost.
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": 0.0034,
        "cost_usd_extraction": 0.0007,
        # No extraction_input_tokens / extraction_output_tokens / model.
    }
    await _collect(agent, "Hi", _frames(["Hello"], usage))

    rows = _ext_rows(agent)
    assert len(rows) == 1
    _, model, in_tok, out_tok, cost, _ = rows[0]
    assert model == ""
    assert in_tok == 0
    assert out_tok == 0
    assert cost == pytest.approx(0.0007)


# ──────────────────────────────────────────────────────────────────────
# Routine flag wire-through. Routine-triggered turns must carry an
# ``x-recordatio-skip-input-extraction`` header so Recordatio skips the
# input-side extraction pass on scheduled prompts. The header name
# describes Recordatio's behavior (skip input pass), not Telos's trigger
# (routine turn) — keeps the wire contract decoupled from Telos
# internals. See Recordatio#49 / PR#50.
# ──────────────────────────────────────────────────────────────────────

def _capture_turn_kwargs(captured: dict):
    """Fake ``stream_recordatio_agent_turn`` capturing the kwargs it got."""
    async def _fake(client, **kwargs):
        captured.update(kwargs)
        yield "text", "ok"
    return _fake


@pytest.mark.asyncio
async def test_skip_input_extraction_header_set_when_is_routine_true(tmp_path):
    agent = make_agent(tmp_path)
    captured: dict = {}
    with patch(
        "telos.services.agent_runtime_v2.stream_recordatio_agent_turn",
        new=_capture_turn_kwargs(captured),
    ):
        async for _ in agent.astream("scheduled work", _ctx(is_routine=True)):
            pass

    assert captured["extra_headers"]["x-recordatio-skip-input-extraction"] == "1"


@pytest.mark.asyncio
async def test_skip_input_extraction_header_absent_when_is_routine_false(tmp_path):
    agent = make_agent(tmp_path)
    captured: dict = {}
    with patch(
        "telos.services.agent_runtime_v2.stream_recordatio_agent_turn",
        new=_capture_turn_kwargs(captured),
    ):
        async for _ in agent.astream("hi", _ctx(is_routine=False)):
            pass

    # No control headers at all on a plain user turn.
    assert captured["extra_headers"] is None


@pytest.mark.asyncio
async def test_cancelled_mcp_cleanup_releases_stream_lock(tmp_path):
    import asyncio
    agent = make_agent(tmp_path)
    async def inner(*args):
        yield "text", "answer"
    agent._astream_inner = inner
    agent._close_mcp_turn = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        async for _ in agent.astream("request", _ctx("u")):
            pass
    assert not agent._stream_lock.locked()
    assert agent._browser is None
