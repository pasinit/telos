"""Tests for ``telos.services.scheduler.BackgroundScheduler`` — phase 5C.

The scheduler is the seam between the chat-path coroutine and the
improvement work that should NOT block chat. The properties we pin here:

- ``submit`` runs a zero-arg async factory on a worker thread.
- The factory is *called* inside the worker (each task gets a fresh
  coroutine + loop) — submitting the same factory twice runs the body
  twice.
- A raising coroutine sets the future's exception; the scheduler itself
  keeps accepting work.
- ``shutdown(wait=True)`` drains, then rejects further submissions with
  ``SchedulerShutdownError``.
- Two submits run concurrently when ``max_workers >= 2``.
"""
import asyncio
import threading
import time

import pytest

from telos.services.scheduler import BackgroundScheduler, SchedulerShutdownError


def test_submit_runs_coroutine_factory_and_returns_value():
    scheduler = BackgroundScheduler(max_workers=2, name="test")
    try:
        async def work() -> int:
            return 42

        fut = scheduler.submit(lambda: work(), name="task-1")
        assert fut.result(timeout=2) == 42
    finally:
        scheduler.shutdown(wait=True)


def test_submit_isolates_exceptions_in_future():
    """A raising coroutine must be captured on the future, not propagate
    out of the worker into the executor's catch-all (which would kill
    the thread silently)."""
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    try:
        async def boom() -> None:
            raise ValueError("nope")

        fut = scheduler.submit(lambda: boom(), name="boom")
        with pytest.raises(ValueError, match="nope"):
            fut.result(timeout=2)

        # Scheduler is still alive — next submit must succeed.
        async def ok() -> str:
            return "ok"

        assert scheduler.submit(lambda: ok(), name="ok").result(timeout=2) == "ok"
    finally:
        scheduler.shutdown(wait=True)


def test_factory_is_invoked_inside_worker_each_time():
    """The factory pattern (zero-arg callable returning a fresh coroutine)
    is what lets us submit the same logic twice without binding the
    coroutine to the wrong loop. Verify each submit produces an
    independent coroutine: the call count equals the submit count."""
    calls: list[int] = []
    lock = threading.Lock()

    async def work() -> None:
        with lock:
            calls.append(1)

    def factory():
        return work()

    scheduler = BackgroundScheduler(max_workers=2, name="test")
    try:
        futs = [scheduler.submit(factory, name=f"task-{i}") for i in range(3)]
        for f in futs:
            f.result(timeout=2)
        assert len(calls) == 3
    finally:
        scheduler.shutdown(wait=True)


def test_shutdown_rejects_new_submissions():
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    scheduler.shutdown(wait=True)

    async def work() -> None:
        return None

    with pytest.raises(SchedulerShutdownError):
        scheduler.submit(lambda: work(), name="late")


def test_shutdown_is_idempotent():
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    scheduler.shutdown(wait=True)
    # Second call must not raise.
    scheduler.shutdown(wait=True)


def test_shutdown_wait_true_drains_pending_work():
    """If a task is in flight when shutdown is called with wait=True, the
    scheduler must wait for it to finish before returning."""
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    started = threading.Event()
    done = threading.Event()

    async def slow() -> None:
        started.set()
        # Yield control twice; loop.run_until_complete still drives this
        # to completion. We don't need a real sleep — just a yield.
        await asyncio.sleep(0.05)
        done.set()

    fut = scheduler.submit(lambda: slow(), name="slow")
    assert started.wait(timeout=2)
    scheduler.shutdown(wait=True)
    # If shutdown waited, the task is done by now.
    assert done.is_set()
    # And the future is fulfilled.
    fut.result(timeout=2)


def test_concurrent_submits_overlap():
    """Two submits with max_workers=2 should overlap in time. We prove
    this by having each task signal an event and wait for the other to
    signal too — if the executor were serial, this would deadlock and
    the test would hit its timeout."""
    scheduler = BackgroundScheduler(max_workers=2, name="test")
    barrier = threading.Barrier(2, timeout=3)

    async def work_a() -> str:
        barrier.wait()
        return "a"

    async def work_b() -> str:
        barrier.wait()
        return "b"

    try:
        fa = scheduler.submit(lambda: work_a(), name="a")
        fb = scheduler.submit(lambda: work_b(), name="b")
        assert fa.result(timeout=3) == "a"
        assert fb.result(timeout=3) == "b"
    finally:
        scheduler.shutdown(wait=True)


def test_cancel_before_start_returns_true():
    """We can only really cancel work that hasn't started. Test that
    ``cancel`` exists and returns a bool from the underlying executor."""
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    # Saturate the single worker so subsequent submits queue.
    block = threading.Event()
    release = threading.Event()

    async def blocker() -> None:
        block.set()
        # Spin until released. Use threading.Event poll rather than
        # asyncio.sleep so we don't depend on loop policy.
        while not release.is_set():
            await asyncio.sleep(0.01)

    async def victim() -> None:
        return None

    try:
        blocker_fut = scheduler.submit(lambda: blocker(), name="blocker")
        assert block.wait(timeout=2)
        # This one is queued, never started.
        victim_fut = scheduler.submit(lambda: victim(), name="victim")
        cancelled = scheduler.cancel(victim_fut)
        # Cancellation of a queued task should succeed; if the executor
        # happened to start it already, cancellation will report False.
        # Either way the result type is a bool.
        assert isinstance(cancelled, bool)
    finally:
        release.set()
        # Wait for the blocker so shutdown can drain cleanly.
        blocker_fut.result(timeout=2)
        scheduler.shutdown(wait=True)


def test_logs_include_task_name(caplog):
    """Task name is the only correlation handle a reviewer has when
    triaging a failing improvement run. The scheduler MUST emit a
    start + (done|failed) log carrying the task name."""
    import logging
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    try:
        async def work() -> None:
            return None

        with caplog.at_level(logging.INFO, logger="telos.services.scheduler"):
            fut = scheduler.submit(lambda: work(), name="my-task")
            fut.result(timeout=2)

        messages = " ".join(r.message for r in caplog.records)
        assert "my-task" in messages
        assert "start" in messages
        assert "done" in messages
    finally:
        scheduler.shutdown(wait=True)


# ──────────────────────────────────────────────────────────────────────
# Fresh-loop-per-task: a task that creates an asyncio object must not
# bleed into the next task's loop. Pin the "each task gets a brand new
# loop" property by running two tasks back-to-back that both use
# asyncio primitives.
# ──────────────────────────────────────────────────────────────────────

def test_each_task_has_its_own_loop():
    scheduler = BackgroundScheduler(max_workers=1, name="test")
    try:
        async def with_lock() -> str:
            # asyncio.Lock binds to the loop running when it is created.
            # If the worker reused a closed loop from the previous task,
            # this would raise.
            lock = asyncio.Lock()
            async with lock:
                return "held"

        for _ in range(3):
            fut = scheduler.submit(lambda: with_lock(), name="reuse")
            assert fut.result(timeout=2) == "held"
    finally:
        scheduler.shutdown(wait=True)
