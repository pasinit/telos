"""Unit tests for :class:`telos.routines.RoutineRunner` execution hardening.

The scheduler's job args carry a *snapshot* of the routine row taken at
registration time. Prod drifted: rows deleted while the MCP tool path
had no live runner left stale jobs behind, firing forever for routines
that no longer exist. ``_run_routine`` must therefore re-read the row
per tick — a missing row skips execution and unschedules the job; an
existing row executes with its FRESH state, not the snapshot.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.routines import RoutineRunner, RoutineStore


@pytest.fixture
def store(tmp_path) -> RoutineStore:
    return RoutineStore(tmp_path / "routines.db")


def _runner(store: RoutineStore, execute_fn: AsyncMock) -> RoutineRunner:
    # The scheduler is never started — these tests drive ``_run_routine``
    # directly, the same coroutine APScheduler invokes per tick.
    return RoutineRunner(store, execute_fn)


@pytest.mark.asyncio
async def test_run_routine_with_missing_row_skips_and_unschedules(store):
    """A job whose row was deleted behind the scheduler's back must not
    execute — and must remove itself so it never fires again."""
    routine = store.create("alice", "weather", "Daily news", interval_seconds=3600)
    store.delete(routine.id)  # row gone; the job (and its snapshot) remain

    execute_fn = AsyncMock(return_value="ok")
    runner = _runner(store, execute_fn)
    stale_job = MagicMock()
    runner._jobs[routine.id] = stale_job

    await runner._run_routine(routine)

    execute_fn.assert_not_awaited()
    stale_job.remove.assert_called_once()
    assert routine.id not in runner._jobs


@pytest.mark.asyncio
async def test_run_routine_executes_fresh_row_not_snapshot(store):
    """Edits made after registration (e.g. the notify-email flag flipped
    via the PATCH route) apply on the next tick — execution re-reads the
    row instead of trusting the snapshot captured in the job args."""
    routine = store.create("alice", "weather", "Daily news", interval_seconds=3600,
                           notify_email=False)
    store.update(routine.id, notify_email=True)

    execute_fn = AsyncMock(return_value="ok")
    runner = _runner(store, execute_fn)

    await runner._run_routine(routine)

    execute_fn.assert_awaited_once()
    args = execute_fn.await_args.args
    assert args[3].notify_email is True  # fresh row, not the stale snapshot
    # Bookkeeping ran against the existing row.
    assert store.get(routine.id).last_run is not None
    assert store.recent_results("alice", "weather")[0].response == "ok"


@pytest.mark.asyncio
async def test_run_routine_advances_last_run_on_failure(store):
    """A crashed run must still advance ``last_run``. Otherwise ``start()``
    reads a stale clock, treats the routine as perpetually missed, and
    re-fires it on every scheduler start — the same-day re-fire pathology."""
    routine = store.create("alice", "jobber", "Daily brief", interval_seconds=3600)

    execute_fn = AsyncMock(side_effect=RuntimeError("model timed out"))
    runner = _runner(store, execute_fn)

    await runner._run_routine(routine)

    execute_fn.assert_awaited_once()
    # Clock advanced despite the failure; no result persisted for a crash.
    assert store.get(routine.id).last_run is not None
    assert store.recent_results("alice", "jobber") == []


@pytest.mark.asyncio
async def test_run_routine_single_flight_skips_overlapping_launch(store):
    """A routine already executing is not launched again — a slow run
    overlapping its interval tick (or the catch-up path racing the regular
    job) must be de-duplicated, not fanned out into redundant runs."""
    routine = store.create("alice", "jobber", "Daily brief", interval_seconds=3600)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_execute(*_args):
        started.set()
        await release.wait()
        return "ok"

    execute_fn = AsyncMock(side_effect=slow_execute)
    runner = _runner(store, execute_fn)

    first = asyncio.ensure_future(runner._run_routine(routine))
    await started.wait()

    # A second launch while the first is in flight must be a no-op.
    await runner._run_routine(routine)
    assert execute_fn.await_count == 1

    release.set()
    await first
    assert execute_fn.await_count == 1
    assert store.recent_results("alice", "jobber")[0].response == "ok"

    # Once the first run finishes, the routine is launchable again.
    release.clear()
    started.clear()
    release.set()
    await runner._run_routine(routine)
    assert execute_fn.await_count == 2


@pytest.mark.asyncio
async def test_runner_shares_london_reporting_date_with_executor(store, monkeypatch):
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from telos import routines
    from telos.routine_invocation import current_invocation
    from telos.services import routine_execution
    from telos.services.routine_execution import RoutineExecutor

    utc_now = datetime(2026, 9, 11, 23, 30, tzinfo=UTC)
    london_now = utc_now.astimezone(ZoneInfo("Europe/London"))
    monkeypatch.setattr(routines, "local_now", lambda: london_now)
    # The executor must inherit the runner's clock, not sample another one.
    monkeypatch.setattr(routine_execution, "local_now", lambda: datetime(2026, 9, 13, tzinfo=UTC))
    messages = []

    class Agent:
        async def astream(self, message, ctx):
            messages.append(message)
            yield "text", "brief"

    runtime = MagicMock()
    runtime.get = AsyncMock(return_value=Agent())
    email = MagicMock()
    executor = RoutineExecutor(runtime, email, lambda _: "owner@example.com", lambda *_: "Jobber", "")
    routine = store.create("alice", "jobber", "Daily news", notify_email=True)
    await _runner(store, executor.execute_with_email)._run_routine(routine)

    assert utc_now.date().isoformat() == "2026-09-11"
    assert "Reporting date: 2026-09-12" in messages[0]
    assert "Timezone: Europe/London" in messages[0]
    assert "Invocation timestamp: 2026-09-12T00:30:00+01:00" in messages[0]
    assert email.send_routine_notification.call_args.kwargs["subject"].endswith("12 Sep 2026")
    assert current_invocation.get() is None


@pytest.mark.asyncio
async def test_run_routine_fences_off_sibling_routines(store):
    """A run names the agent's other enabled routines as out of scope, so
    the agent does not repeat their work from history (homey 2026-10-14:
    each of two daily searches ran both searches and sent both emails)."""
    routine = store.create("alice", "homey", "Flat search", name="Flat search",
                           interval_seconds=86400, enabled=True)
    store.create("alice", "homey", "Loft search", name="Loft search",
                 interval_seconds=86400, enabled=True)
    store.create("alice", "homey", "Staged idea", name="Staged idea",
                 interval_seconds=86400, enabled=False)
    store.create("alice", "other", "Other agent", name="Other agent",
                 interval_seconds=86400, enabled=True)

    execute_fn = AsyncMock(return_value="ok")
    await _runner(store, execute_fn)._run_routine(routine)

    message = execute_fn.await_args.args[2]
    assert message.startswith("Flat search")
    assert "perform ONLY the routine described above" in message
    assert "- Loft search" in message
    assert "Staged idea" not in message
    assert "Other agent" not in message


@pytest.mark.asyncio
async def test_run_routine_without_siblings_sends_bare_description(store):
    routine = store.create("alice", "homey", "Flat search", interval_seconds=86400, enabled=True)

    execute_fn = AsyncMock(return_value="ok")
    await _runner(store, execute_fn)._run_routine(routine)

    assert execute_fn.await_args.args[2] == "Flat search"
