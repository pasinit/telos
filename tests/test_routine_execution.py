"""Tests for ``RoutineExecutor`` — the routine-fire callbacks.

Covers the failure contract added for truncated agent streams: a
``stream_error`` event from ``Agent.astream`` must turn the run into a
``RoutineStreamError`` (never a normal result), and the opt-in email must
become an explicit FAILURE notice carrying the partial output — a partial
brief emailed as if it were the result is how the bug this guards against
shipped silently for days.
"""
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from telos.services import routine_execution
from telos.services.routine_execution import RoutineExecutor, RoutineStreamError


class _FakeAgent:
    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self._events = events

    async def astream(self, message: str, ctx: Any) -> AsyncIterator[tuple[str, Any]]:
        for kind, data in self._events:
            yield kind, data


class _SequencedAgent:
    """Yields a different event list on each successive ``astream`` call.

    Models a routine turn that fails transiently and then succeeds on retry:
    call N replays ``runs[N]`` (the last entry is reused once exhausted).
    ``calls`` records how many times ``astream`` was invoked so a test can
    assert that a *non*-retryable failure is NOT re-attempted.
    """

    def __init__(self, runs: list[list[tuple[str, Any]]]) -> None:
        self._runs = runs
        self.calls = 0

    async def astream(self, message: str, ctx: Any) -> AsyncIterator[tuple[str, Any]]:
        events = self._runs[min(self.calls, len(self._runs) - 1)]
        self.calls += 1
        for kind, data in events:
            yield kind, data


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Keep the bounded-retry tests fast by zeroing the backoff sleep."""
    monkeypatch.setattr(routine_execution, "ROUTINE_STREAM_RETRY_BACKOFF_S", 0.0)


class _FakeRuntime:
    def __init__(self, agent: _FakeAgent) -> None:
        self._agent = agent

    async def get(self, user_id: str, slug: str) -> _FakeAgent:
        return self._agent

    def make_context(self, user_id: str, is_routine: bool = False) -> Any:
        return SimpleNamespace(user_id=user_id, is_routine=is_routine)


def _executor(agent: _FakeAgent, email_service: MagicMock | None = None) -> RoutineExecutor:
    return RoutineExecutor(
        agent_runtime=_FakeRuntime(agent),  # type: ignore[arg-type]
        email_service=email_service or MagicMock(),
        get_user_email_fn=lambda user_id: "owner@example.com",
        get_agent_name_fn=lambda user_id, slug: "Jobber",
        base_url="https://telos.example",
    )


def _routine(notify_email: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id="r1", name="Daily brief", notify_email=notify_email)


@pytest.mark.asyncio
async def test_execute_joins_text_events():
    agent = _FakeAgent([
        ("text", "hello "),
        ("thinking", "never surfaced"),
        ("text", "world"),
    ])
    assert await _executor(agent).execute("u", "jobber", "go") == "hello world"


@pytest.mark.asyncio
async def test_execute_raises_on_stream_error_with_partial_text():
    agent = _FakeAgent([
        ("text", "intro only"),
        ("text", "[error: recordatio transport failure: ReadTimeout]"),
        ("stream_error", {"reason": "transport:ReadTimeout"}),
    ])
    with pytest.raises(RoutineStreamError) as exc_info:
        await _executor(agent).execute("u", "jobber", "go")
    assert exc_info.value.reason == "transport:ReadTimeout"
    assert "intro only" in exc_info.value.partial


@pytest.mark.asyncio
async def test_execute_with_email_success_sends_result_email():
    agent = _FakeAgent([("text", "the full brief"), ])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    response = await executor.execute_with_email("u", "jobber", "go", _routine())

    assert response == "the full brief"
    email_service.send_routine_notification.assert_called_once()
    kwargs = email_service.send_routine_notification.call_args.kwargs
    assert "the full brief" in kwargs["body"]
    assert "FAILED" not in kwargs["subject"]


@pytest.mark.asyncio
async def test_execute_with_email_failure_sends_failure_notice_and_reraises():
    agent = _FakeAgent([
        ("text", "partial intro"),
        ("stream_error", {"reason": "truncated"}),
    ])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    with pytest.raises(RoutineStreamError):
        await executor.execute_with_email("u", "jobber", "go", _routine())

    email_service.send_routine_notification.assert_called_once()
    kwargs = email_service.send_routine_notification.call_args.kwargs
    assert "FAILED" in kwargs["subject"]
    assert "failed before completing" in kwargs["body"]
    assert "truncated" in kwargs["body"]
    # The partial output is included, but marked as partial.
    assert "partial intro" in kwargs["body"]
    assert "Partial output" in kwargs["body"]


@pytest.mark.asyncio
async def test_transient_stream_error_is_retried_then_succeeds():
    # First run dies mid-SSE on a transient transport disconnect; the retry
    # succeeds. The user must get the real brief, not a FAILED notice.
    agent = _SequencedAgent([
        [
            ("text", "partial intro"),
            ("text", "[error: recordatio transport failure: RemoteProtocolError]"),
            ("stream_error", {"reason": "transport:RemoteProtocolError"}),
        ],
        [("text", "the full brief")],
    ])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    response = await executor.execute_with_email("u", "jobber", "go", _routine())

    assert response == "the full brief"
    assert agent.calls == 2  # retried exactly once
    email_service.send_routine_notification.assert_called_once()
    kwargs = email_service.send_routine_notification.call_args.kwargs
    assert "the full brief" in kwargs["body"]
    assert "FAILED" not in kwargs["subject"]


@pytest.mark.asyncio
async def test_truncated_stream_is_retried():
    agent = _SequencedAgent([
        [("stream_error", {"reason": "truncated"})],
        [("text", "recovered brief")],
    ])
    executor = _executor(agent)

    assert await executor.execute_with_email("u", "jobber", "go", _routine()) == "recovered brief"
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_persistent_transient_failure_exhausts_retries_and_fails():
    # Every attempt fails transiently: bounded retry gives up and emails FAILURE.
    agent = _SequencedAgent([
        [("text", "partial"), ("stream_error", {"reason": "transport:ReadTimeout"})],
    ])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    with pytest.raises(RoutineStreamError):
        await executor.execute_with_email("u", "jobber", "go", _routine())

    # Initial attempt + ROUTINE_STREAM_MAX_RETRIES retries.
    assert agent.calls == routine_execution.ROUTINE_STREAM_MAX_RETRIES + 1
    kwargs = email_service.send_routine_notification.call_args.kwargs
    assert "FAILED" in kwargs["subject"]


@pytest.mark.asyncio
async def test_upstream_error_event_is_not_retried():
    # A genuine upstream model error is terminal — must fail fast, no retry.
    agent = _SequencedAgent([
        [
            ("text", "[error: overloaded_error: upstream is busy]"),
            ("stream_error", {"reason": "overloaded_error", "message": "upstream is busy"}),
        ],
    ])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    with pytest.raises(RoutineStreamError) as exc_info:
        await executor.execute_with_email("u", "jobber", "go", _routine())

    assert exc_info.value.reason == "overloaded_error"
    assert agent.calls == 1  # not retried


@pytest.mark.asyncio
async def test_http_status_error_is_not_retried():
    # HTTP status errors (e.g. 404 missing-config) are terminal — no retry.
    agent = _SequencedAgent([
        [
            ("text", "[error: recordatio returned HTTP 404]"),
            ("stream_error", {"reason": "http_404"}),
        ],
    ])
    executor = _executor(agent)

    with pytest.raises(RoutineStreamError) as exc_info:
        await executor.execute_with_email("u", "jobber", "go", _routine())

    assert exc_info.value.reason == "http_404"
    assert agent.calls == 1  # not retried


@pytest.mark.asyncio
async def test_execute_raises_empty_output_on_whitespace_only_completion():
    # Stream completes cleanly (no stream_error) but yields only whitespace.
    # This must NOT be treated as a successful run — it is a silent no-op.
    agent = _FakeAgent([("text", "  "), ("text", "\n")])
    with pytest.raises(RoutineStreamError) as exc_info:
        await _executor(agent).execute("u", "jobber", "go")
    assert exc_info.value.reason == "empty_output"


@pytest.mark.asyncio
async def test_empty_output_is_retried_then_succeeds():
    # A clean-but-empty completion is retried; the retry produces the real brief.
    # This is exactly homey 2026-08-16: an empty assistant response that
    # succeeded on a re-run. The user must get the brief, not a FAILED notice.
    agent = _SequencedAgent([
        [("text", "   ")],
        [("text", "the full brief")],
    ])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    response = await executor.execute_with_email("u", "jobber", "go", _routine())

    assert response == "the full brief"
    assert agent.calls == 2  # retried exactly once
    kwargs = email_service.send_routine_notification.call_args.kwargs
    assert "the full brief" in kwargs["body"]
    assert "FAILED" not in kwargs["subject"]


@pytest.mark.asyncio
async def test_persistent_empty_output_exhausts_retries_and_fails():
    # Every attempt is empty: bounded retry gives up and emails an explicit
    # FAILURE notice rather than persisting/emailing a zero-output "success".
    agent = _SequencedAgent([[("text", "")]])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    with pytest.raises(RoutineStreamError) as exc_info:
        await executor.execute_with_email("u", "jobber", "go", _routine())

    assert exc_info.value.reason == "empty_output"
    assert agent.calls == routine_execution.ROUTINE_STREAM_MAX_RETRIES + 1
    kwargs = email_service.send_routine_notification.call_args.kwargs
    assert "FAILED" in kwargs["subject"]
    assert "No result was produced" in kwargs["body"]


@pytest.mark.asyncio
async def test_execute_with_email_failure_without_optin_sends_nothing():
    agent = _FakeAgent([("stream_error", {"reason": "truncated"})])
    email_service = MagicMock()
    executor = _executor(agent, email_service)

    with pytest.raises(RoutineStreamError):
        await executor.execute_with_email("u", "jobber", "go", _routine(notify_email=False))

    email_service.send_routine_notification.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_reporting_date_survives_midnight_and_retries(monkeypatch, failure):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from telos.routine_invocation import current_invocation

    before = datetime(2026, 9, 11, 23, 59, tzinfo=ZoneInfo("Europe/London"))
    after = datetime(2026, 9, 12, 0, 1, tzinfo=ZoneInfo("Europe/London"))
    clock = [before]
    messages = []

    class MidnightAgent:
        async def astream(self, message, ctx):
            messages.append(message)
            clock[0] = after
            yield "turn_id", {"turn_id": str(len(messages))}
            if failure or len(messages) == 1:
                yield "stream_error", {"reason": "truncated"}
            else:
                yield "text", "brief"

    monkeypatch.setattr(routine_execution, "local_now", lambda: clock[0])
    email = MagicMock()
    executor = _executor(MidnightAgent(), email)
    executor._record = MagicMock()
    if failure:
        with pytest.raises(RoutineStreamError):
            await executor.execute_with_email("u", "jobber", "Daily brief", _routine())
    else:
        await executor.execute_with_email("u", "jobber", "Daily brief", _routine())

    assert len(messages) >= 2
    assert len(set(messages)) == 1
    assert "Reporting date: 2026-09-11" in messages[0]
    assert "Timezone: Europe/London" in messages[0]
    assert "Invocation timestamp: 2026-09-11T23:59:00+01:00" in messages[0]
    assert email.send_routine_notification.call_args.kwargs["subject"].endswith("11 Sep 2026")
    delivery = [call.args[3] for call in executor._record.call_args_list if call.args[2] == "email_delivery"]
    assert delivery[0]["notification_completed_at"] == after.isoformat()
    assert current_invocation.get() is None
