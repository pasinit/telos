"""``BackgroundScheduler`` — fire-and-forget runner for async work.

This is the worker pool that the upcoming ``ImprovementService`` (phase 5D)
uses to run improver passes off the chat path. Today the improver runs on
``asyncio.create_task`` from inside ``Agent._astream_inner``, which couples
the chat coroutine to improvement scheduling and makes the "chat latency is
sacred" invariant fragile.

``BackgroundScheduler`` decouples that work: each ``submit(coro_factory)``
takes a *zero-arg* async callable, hands it to a ``ThreadPoolExecutor``
worker, and the worker opens a **fresh** event loop on its own thread to
drive the coroutine. The caller's loop (FastAPI's) is never touched.

Why a thread-per-task loop instead of pumping work into the main loop:
the improver run is CPU-and-network bound for tens of seconds; if it ran
on the request loop it would compete with chat traffic for scheduling
slack. A separate thread + its own loop keeps the request loop free.

The class also carries a small "accepting" flag so ``shutdown`` can stop
admitting new work while it drains pending futures.
"""
import asyncio
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

log = logging.getLogger(__name__)

# Re-exported for callers building submit factories. The actual coroutine
# type is parameterless and returns Any.
CoroFactory = Callable[[], Coroutine[Any, Any, Any]]


class SchedulerShutdownError(RuntimeError):
    """Raised by :meth:`BackgroundScheduler.submit` when the scheduler is
    no longer accepting new work (``shutdown`` has been called). Callers
    should treat this as a benign drop — the improvement loop, for
    example, will simply be re-triggered by its polling trigger after
    the next restart.
    """


class BackgroundScheduler:
    """Threaded fire-and-forget runner for zero-arg async callables.

    Parameters:
        max_workers: Size of the underlying ``ThreadPoolExecutor``.
                     Improver runs are sequential per agent today, so the
                     default of 4 leaves plenty of headroom for parallel
                     across-agent fan-out without saturating the CPU.
        name:        Prefix for the worker threads — shows up in stack
                     traces and ``threading.enumerate()`` output. Useful
                     when triaging hung workers.
    """

    def __init__(self, max_workers: int = 4, name: str = "background") -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=f"{name}-worker",
        )
        self._name = name
        self._accepting = True
        # Guards the ``_accepting`` flag against the submit/shutdown race.
        self._lock = threading.Lock()

    def submit(self, coro_factory: CoroFactory, name: str) -> Future:
        """Schedule ``coro_factory()`` on a worker thread.

        ``coro_factory`` MUST be a zero-arg async callable (a function or
        lambda that returns a fresh coroutine when invoked). We *call*
        the factory inside the worker, not at submit time, so the
        coroutine is created on the same thread / loop that will drive
        it — coroutines bound to one loop cannot be awaited on another.

        Returns a ``concurrent.futures.Future`` whose ``.result()``
        carries the coroutine's return value or its exception. The
        scheduler itself never blocks on the future.
        """
        with self._lock:
            if not self._accepting:
                raise SchedulerShutdownError(
                    f"scheduler {self._name!r} not accepting new work",
                )

        def _worker() -> Any:
            start = time.monotonic()
            log.info("scheduler:%s task %r start", self._name, name)
            # Each task gets a brand-new loop on the worker thread. We
            # close it on the way out so the thread is reusable.
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(coro_factory())
                log.info(
                    "scheduler:%s task %r done in %.2fs",
                    self._name, name, time.monotonic() - start,
                )
                return result
            except Exception:
                log.exception(
                    "scheduler:%s task %r failed after %.2fs",
                    self._name, name, time.monotonic() - start,
                )
                raise
            finally:
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)

        return self._executor.submit(_worker)

    def cancel(self, future: Future) -> bool:
        """Best-effort cancel.

        ``ThreadPoolExecutor`` only honours cancellation of futures that
        haven't started running yet. A coroutine already inside
        ``run_until_complete`` cannot be safely cancelled from outside
        — we don't try.
        """
        return future.cancel()

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting new work and (optionally) drain pending tasks.

        Idempotent: calling ``shutdown`` twice is a no-op on the second
        call. Set ``wait=False`` to return immediately while in-flight
        tasks keep running on their threads — useful during process
        teardown when the OS will reclaim the threads anyway.
        """
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        self._executor.shutdown(wait=wait)
