"""Message-count trigger for the improvement loop.

Pre-Phase-5 the improvement loop fired on ``TurnComplete`` events
published by ``Agent._astream_inner``. That worked, but coupled the
improvement scheduler to the in-process chat path. Once Recordatio becomes
the agent runner (Phase 3) the chat turn no longer fires through the
local ``Agent`` at all, so the in-process event would stop arriving and
the improver would never run.

This module replaces the event subscription with a poll. The trigger:

- Wakes every ``poll_interval`` seconds on a daemon thread.
- For every ``(user_id, slug)`` pair returned by ``list_pairs_fn`` it
  reads the current chat-message count from the conversation log via
  the same ``AgentImprovementState`` contract that the legacy event
  subscriber consumed.
- When ``current_count - last_seen_count >= threshold`` it invokes
  ``on_threshold_fn(user_id, slug, state)`` exactly once and advances
  ``last_seen_count`` to ``current_count`` so the next fire requires a
  fresh ``threshold`` worth of messages.

Seeding (cross-restart behavior): the very first observation of a
``(user_id, slug)`` pair seeds ``last_seen_count`` to ``current_count``
*without* firing — the trigger does NOT replay history. That matches
the legacy "seed-at-Agent-init" semantics (the pre-Phase-5 counter
started at 0 on every process restart and never tried to make up for
turns it missed while down). If you want to override this — e.g. force a
run on first poll after restart — pass ``fire_on_first=True``.

The trigger never blocks the caller's thread. ``start`` returns
immediately; ``stop`` joins the worker thread (default 5s timeout).
``on_threshold_fn`` is expected to be cheap or itself dispatch the heavy
work to ``BackgroundScheduler`` — see :class:`ImprovementService` for the
production wiring.
"""

import logging
import threading
import time
from collections.abc import Callable

from telos.domain import AgentImprovementState

log = logging.getLogger(__name__)


# Per-MIGRATION_PLAN.md §"Phase 5 — Improver migration":
#   "threshold (default 50, configurable)"
# Pre-Phase-5 the in-Agent counter defaulted to 20; the new value is
# tuned for a Recordatio-fronted chat path where the improver also pays
# for the GET+PUT round-trip to Recordatio on every fire.
DEFAULT_THRESHOLD: int = 50

# Polling cadence. 60s is the conservative choice: a 50-message threshold
# at chat-typical throughput will be hit roughly every few hours, so the
# poll cycle's resolution is irrelevant to the user-visible cadence and
# the overhead of a single SQLite ``COUNT(*)`` per cached agent every
# minute is trivial.
DEFAULT_POLL_INTERVAL_SECONDS: float = 60.0


ListPairsFn = Callable[[], list[tuple[str, str]]]
"""Returns the ``(user_id, slug)`` pairs to poll.

Production passes a callback over ``AgentRuntime.all_cached()`` so the
trigger only polls agents that are currently in memory. Agents that
haven't been touched since startup don't get polled — they also can't
accumulate new messages without being loaded, so this is correct.
"""

GetStateFn = Callable[[str, str], AgentImprovementState | None]
"""Resolves a ``(user_id, slug)`` to its ``AgentImprovementState``.

Returns ``None`` if the agent has been evicted between
``list_pairs_fn()`` and the resolution call — the trigger silently skips
that pair this cycle.
"""

OnThresholdFn = Callable[[str, str, AgentImprovementState], None]
"""Called when ``(uid, slug)`` crosses the threshold.

Receives the resolved state so the consumer doesn't have to re-resolve.
Exceptions raised here are logged and swallowed — one buggy callback must
not break the polling loop for other agents.
"""


class MessageCountTrigger:
    """Polls chat persistence and fires when message count crosses a threshold.

    Args:
        list_pairs_fn:      Returns ``[(user_id, slug), ...]`` to poll each cycle.
        get_state_fn:       Resolves a pair to its
                            :class:`AgentImprovementState` (or ``None`` if
                            the agent has been evicted).
        on_threshold_fn:    Called once per threshold crossing per pair.
        threshold:          Messages-per-fire (default
                            :data:`DEFAULT_THRESHOLD` = 50).
        poll_interval:      Seconds between poll cycles (default
                            :data:`DEFAULT_POLL_INTERVAL_SECONDS` = 60).
        fire_on_first:      If True, fire on the first poll if the
                            already-stored count is at least ``threshold``.
                            Default False — see module docstring.
        name:               Thread name (shows up in stack traces).
        clock:              Test seam — defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        *,
        list_pairs_fn: ListPairsFn,
        get_state_fn: GetStateFn,
        on_threshold_fn: OnThresholdFn,
        threshold: int = DEFAULT_THRESHOLD,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        fire_on_first: bool = False,
        name: str = "improver-trigger",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be > 0, got {poll_interval}")

        self._list_pairs = list_pairs_fn
        self._get_state = get_state_fn
        self._on_threshold = on_threshold_fn
        self._threshold = threshold
        self._poll_interval = poll_interval
        self._fire_on_first = fire_on_first
        self._name = name
        self._clock = clock

        # Per-pair "what count did we last see / fire at?"
        self._last_seen: dict[tuple[str, str], int] = {}
        self._seeded: set[tuple[str, str]] = set()
        # Guards the two maps above against the (test-only) case where
        # ``poll_once`` is called from the test thread while the worker
        # is also active. Production has a single worker thread.
        self._lock = threading.Lock()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the daemon worker thread. Idempotent.

        Returns immediately; the worker calls ``poll_once`` every
        ``poll_interval`` seconds until ``stop`` is called.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True,
        )
        self._thread.start()
        log.info(
            "MessageCountTrigger started (threshold=%d, poll_interval=%.1fs)",
            self._threshold, self._poll_interval,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to stop and join it.

        ``timeout`` is the join timeout per call. The worker checks the
        stop event every ``poll_interval / 10`` seconds (capped at 1s)
        so a stop signal during a long sleep is honoured promptly.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ── public introspection (tests / debugging) ────────────────────

    def last_seen(self, user_id: str, slug: str) -> int:
        """Return the per-pair ``last_seen`` count, ``0`` if not yet seeded."""
        with self._lock:
            return self._last_seen.get((user_id, slug), 0)

    def is_seeded(self, user_id: str, slug: str) -> bool:
        """Return whether the trigger has observed the pair at least once."""
        with self._lock:
            return (user_id, slug) in self._seeded

    # ── poll loop ───────────────────────────────────────────────────

    def _run(self) -> None:
        # Cap the sleep granularity so ``stop`` is responsive even when
        # ``poll_interval`` is long. 1 second is a fine balance between
        # CPU idleness and shutdown latency.
        sleep_granularity = min(1.0, max(self._poll_interval / 10.0, 0.05))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                # Any unexpected error in poll_once should not kill the
                # daemon thread. Per-pair errors are already swallowed
                # inside poll_once; this is a defense-in-depth wrapper.
                log.exception("MessageCountTrigger poll cycle failed")

            # Sleep in small slices so ``stop`` reacts within
            # ``sleep_granularity`` seconds.
            slept = 0.0
            while slept < self._poll_interval and not self._stop.is_set():
                self._stop.wait(timeout=sleep_granularity)
                slept += sleep_granularity

    def poll_once(self) -> None:
        """One poll cycle: enumerate pairs, advance counters, fire callbacks.

        Public for tests — production calls it from the worker thread.
        """
        try:
            pairs = self._list_pairs()
        except Exception:
            log.exception("MessageCountTrigger: list_pairs_fn raised")
            return

        for user_id, slug in pairs:
            try:
                state = self._get_state(user_id, slug)
            except Exception:
                log.exception(
                    "MessageCountTrigger: get_state_fn raised for %s/%s",
                    user_id, slug,
                )
                continue
            if state is None:
                # Agent was evicted between list and resolve — skip,
                # without seeding. Next cycle will retry if it's back.
                continue

            try:
                current = int(state.conversation_log.count())
            except Exception:
                log.exception(
                    "MessageCountTrigger: count() raised for %s/%s",
                    user_id, slug,
                )
                continue

            self._tick(user_id, slug, current, state)

    def _tick(
        self,
        user_id: str,
        slug: str,
        current: int,
        state: AgentImprovementState,
    ) -> None:
        """Advance the per-pair counter and fire if the threshold was crossed.

        Splitting this out keeps the firing decision testable without
        forging a full ``poll_once`` invocation.
        """
        key = (user_id, slug)
        should_fire = False
        with self._lock:
            if key not in self._seeded:
                # First observation. Seed without firing unless the
                # caller explicitly opted into ``fire_on_first`` AND
                # there are at least ``threshold`` accumulated messages.
                self._seeded.add(key)
                if self._fire_on_first and current >= self._threshold:
                    self._last_seen[key] = current
                    should_fire = True
                else:
                    self._last_seen[key] = current
                    return
            else:
                last = self._last_seen.get(key, 0)
                if current < last:
                    # The conversation log shrank — most likely the user
                    # invoked "clear chat history" which drops the
                    # turns DB. Re-seed to the new count without firing.
                    log.info(
                        "MessageCountTrigger: count regressed for %s/%s "
                        "(%d → %d); re-seeding",
                        user_id, slug, last, current,
                    )
                    self._last_seen[key] = current
                    return
                if current - last >= self._threshold:
                    self._last_seen[key] = current
                    should_fire = True

        if should_fire:
            try:
                self._on_threshold(user_id, slug, state)
            except Exception:
                # Per-pair callback failures must not break the loop or
                # affect other pairs. The legacy event-driven path made
                # the same guarantee inside the ``EventBus``.
                log.exception(
                    "MessageCountTrigger: on_threshold_fn raised for %s/%s",
                    user_id, slug,
                )
