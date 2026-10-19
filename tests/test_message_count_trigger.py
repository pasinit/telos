"""Tests for ``telos.improvement.triggers.MessageCountTrigger`` — Phase 5.

The trigger replaces the in-process ``TurnComplete`` subscription that
the legacy ``ImprovementService`` used (see PHASE 5D notes in
``telos.services.improvement``). These tests pin:

- Seeds without firing on first observation (cross-restart safety).
- Fires once per ``threshold`` messages of forward delta.
- Per-pair counters independent.
- Cache eviction (``get_state_fn`` returns ``None``) silently skips.
- Count regression (history cleared) re-seeds without firing.
- Callback exceptions don't break the polling loop.
- Worker thread starts and stops cleanly.

The tests drive ``poll_once`` directly rather than spinning the thread,
except for the lifecycle test which exercises ``start``/``stop``.
"""
import threading
import time

import pytest

from telos.improvement.triggers import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD,
    MessageCountTrigger,
)

# ──────────────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────────────

class _FakeLog:
    """Stand-in for ``ConversationLog`` exposing only ``count()``."""

    def __init__(self, count: int = 0) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def set_count(self, n: int) -> None:
        self._count = n


class _FakeState:
    """Stand-in for ``AgentImprovementState``: only ``conversation_log`` is read."""

    def __init__(self, log: _FakeLog) -> None:
        self.conversation_log = log


def _make_pair_resolver(states: dict[tuple[str, str], _FakeState]):
    """Return ``(list_pairs_fn, get_state_fn)`` over a mutable mapping.

    Tests can drop keys from ``states`` to simulate cache eviction
    between the list call and the resolve call.
    """
    def list_pairs() -> list[tuple[str, str]]:
        return list(states.keys())

    def get_state(uid: str, slug: str):
        return states.get((uid, slug))

    return list_pairs, get_state


# ──────────────────────────────────────────────────────────────────────
# Construction
# ──────────────────────────────────────────────────────────────────────

def test_defaults_match_plan():
    """The plan pins the defaults — 50 messages per fire, poll cycle 60s."""
    assert DEFAULT_THRESHOLD == 50
    assert DEFAULT_POLL_INTERVAL_SECONDS == 60.0


def test_invalid_threshold_raises():
    with pytest.raises(ValueError):
        MessageCountTrigger(
            list_pairs_fn=lambda: [],
            get_state_fn=lambda u, s: None,
            on_threshold_fn=lambda *a: None,
            threshold=0,
        )


def test_invalid_poll_interval_raises():
    with pytest.raises(ValueError):
        MessageCountTrigger(
            list_pairs_fn=lambda: [],
            get_state_fn=lambda u, s: None,
            on_threshold_fn=lambda *a: None,
            poll_interval=0.0,
        )


# ──────────────────────────────────────────────────────────────────────
# Seeding (cross-restart semantics)
# ──────────────────────────────────────────────────────────────────────

def test_first_observation_seeds_without_firing():
    """A fresh trigger seeing a pair for the first time records the
    count but does NOT fire — even if the count is already above the
    threshold. Matches the legacy "seed-at-Agent-init" behaviour: the
    pre-Phase-5 counter started at 0 on every process restart and
    never replayed history.
    """
    log = _FakeLog(count=100)  # well above threshold
    states = {("alice", "weather"): _FakeState(log)}
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=10,
    )
    trigger.poll_once()
    assert fires == []
    assert trigger.last_seen("alice", "weather") == 100
    assert trigger.is_seeded("alice", "weather")


def test_fire_on_first_when_opt_in():
    """``fire_on_first=True`` overrides the cautious seed-only path."""
    log = _FakeLog(count=10)
    states = {("alice", "weather"): _FakeState(log)}
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=10,
        fire_on_first=True,
    )
    trigger.poll_once()
    assert fires == [("alice", "weather")]


# ──────────────────────────────────────────────────────────────────────
# Threshold mechanics
# ──────────────────────────────────────────────────────────────────────

def test_fires_when_delta_reaches_threshold():
    log = _FakeLog(count=0)
    states = {("alice", "weather"): _FakeState(log)}
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=3,
    )
    # First poll seeds at 0, no fire.
    trigger.poll_once()
    assert fires == []
    # Bump to 2 — below threshold delta.
    log.set_count(2)
    trigger.poll_once()
    assert fires == []
    # Bump to 3 — delta meets threshold, fires.
    log.set_count(3)
    trigger.poll_once()
    assert fires == [("alice", "weather")]
    # last_seen advanced to 3, so next fire requires another 3 messages.
    assert trigger.last_seen("alice", "weather") == 3
    log.set_count(5)
    trigger.poll_once()
    assert fires == [("alice", "weather")]
    log.set_count(6)
    trigger.poll_once()
    assert fires == [("alice", "weather"), ("alice", "weather")]


def test_below_threshold_does_not_fire():
    log = _FakeLog(count=0)
    states = {("alice", "weather"): _FakeState(log)}
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=10,
    )
    trigger.poll_once()  # seed at 0
    for n in (1, 5, 9):
        log.set_count(n)
        trigger.poll_once()
    assert fires == []


def test_per_pair_counters_independent():
    weather_log = _FakeLog(count=0)
    finance_log = _FakeLog(count=0)
    states = {
        ("alice", "weather"): _FakeState(weather_log),
        ("alice", "finance"): _FakeState(finance_log),
    }
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=5,
    )
    # Seed both.
    trigger.poll_once()
    # Weather crosses; finance stays put.
    weather_log.set_count(5)
    trigger.poll_once()
    assert fires == [("alice", "weather")]
    # Finance now crosses independently.
    finance_log.set_count(5)
    trigger.poll_once()
    assert fires == [("alice", "weather"), ("alice", "finance")]


def test_count_regression_reseeds_without_firing():
    """If ``count()`` shrinks (chat history cleared), the trigger
    re-seeds to the new count rather than firing every poll until the
    count climbs back."""
    log = _FakeLog(count=20)
    states = {("alice", "weather"): _FakeState(log)}
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=5,
    )
    trigger.poll_once()  # seed at 20
    log.set_count(0)  # history wiped
    trigger.poll_once()
    assert fires == []
    assert trigger.last_seen("alice", "weather") == 0
    # And the next forward delta from the new baseline fires normally.
    log.set_count(5)
    trigger.poll_once()
    assert fires == [("alice", "weather")]


# ──────────────────────────────────────────────────────────────────────
# Resolution edge cases
# ──────────────────────────────────────────────────────────────────────

def test_evicted_agent_is_skipped_silently():
    """``get_state_fn`` returning ``None`` (the agent was evicted
    between the list call and the resolve call) is not an error: skip
    without seeding."""
    states = {("alice", "weather"): _FakeState(_FakeLog(count=5))}
    list_pairs, get_state = _make_pair_resolver(states)

    def get_state_evicted(uid: str, slug: str):
        return None  # always evicted

    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state_evicted,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=1,
    )
    trigger.poll_once()
    assert fires == []
    assert not trigger.is_seeded("alice", "weather")


def test_list_pairs_raise_does_not_kill_trigger():
    states = {("alice", "weather"): _FakeState(_FakeLog(count=0))}
    _, get_state = _make_pair_resolver(states)
    raises_first = [True]

    def list_pairs():
        if raises_first[0]:
            raises_first[0] = False
            raise RuntimeError("boom")
        return list(states.keys())

    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=3,
    )
    # First poll: list_pairs raises, no seed.
    trigger.poll_once()
    # Second poll: works normally.
    states[("alice", "weather")].conversation_log.set_count(0)
    trigger.poll_once()
    assert trigger.is_seeded("alice", "weather")


def test_get_state_raise_skips_pair():
    """A raising ``get_state_fn`` for one pair doesn't poison the rest."""
    weather_log = _FakeLog(count=0)
    finance_log = _FakeLog(count=0)
    states = {
        ("alice", "weather"): _FakeState(weather_log),
        ("alice", "finance"): _FakeState(finance_log),
    }
    list_pairs, _ = _make_pair_resolver(states)

    def get_state(uid: str, slug: str):
        if slug == "weather":
            raise RuntimeError("boom")
        return states.get((uid, slug))

    fires: list[tuple[str, str]] = []
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda u, s, _: fires.append((u, s)),
        threshold=3,
    )
    trigger.poll_once()  # seeds finance, weather raises
    finance_log.set_count(3)
    trigger.poll_once()
    assert fires == [("alice", "finance")]


def test_callback_exception_does_not_break_loop():
    """A raising ``on_threshold_fn`` for one pair must not prevent the
    same poll cycle from delivering callbacks for other pairs (the
    callback runs *after* the lock is released, so an exception can't
    corrupt the trigger's internal state either)."""
    a_log = _FakeLog(count=0)
    b_log = _FakeLog(count=0)
    states = {
        ("alice", "a"): _FakeState(a_log),
        ("alice", "b"): _FakeState(b_log),
    }
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []

    def callback(uid, slug, _state):
        if slug == "a":
            raise RuntimeError("boom")
        fires.append((uid, slug))

    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=callback,
        threshold=2,
    )
    trigger.poll_once()  # seed both
    a_log.set_count(2)
    b_log.set_count(2)
    trigger.poll_once()
    assert ("alice", "b") in fires
    # Even though A raised, its last_seen still advanced — we don't
    # want a buggy callback to repeatedly re-fire on every poll.
    assert trigger.last_seen("alice", "a") == 2


# ──────────────────────────────────────────────────────────────────────
# Threading lifecycle
# ──────────────────────────────────────────────────────────────────────

def test_start_and_stop_drive_the_loop():
    """``start`` spawns a worker thread that calls ``poll_once`` until
    ``stop``. We use a very short poll interval so the test finishes
    quickly; the worker should fire at least one ``poll_once`` and
    then exit cleanly on ``stop``."""
    log = _FakeLog(count=0)
    states = {("alice", "weather"): _FakeState(log)}
    list_pairs, get_state = _make_pair_resolver(states)
    fires: list[tuple[str, str]] = []
    polled = threading.Event()

    def on_threshold(u, s, _):
        fires.append((u, s))
        polled.set()

    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=on_threshold,
        threshold=2,
        poll_interval=0.05,
    )
    trigger.start()
    try:
        # Seed runs on the first cycle; bump count for a fire on the second.
        time.sleep(0.1)
        log.set_count(2)
        # Worker should pick up within the next interval.
        assert polled.wait(timeout=2.0), "trigger never fired"
    finally:
        trigger.stop(timeout=2.0)
    assert ("alice", "weather") in fires


def test_double_start_is_idempotent():
    states: dict[tuple[str, str], _FakeState] = {}
    list_pairs, get_state = _make_pair_resolver(states)
    trigger = MessageCountTrigger(
        list_pairs_fn=list_pairs,
        get_state_fn=get_state,
        on_threshold_fn=lambda *_: None,
        poll_interval=0.05,
    )
    trigger.start()
    trigger.start()  # no-op
    try:
        # Internal _thread reference points at one worker only.
        assert trigger._thread is not None  # noqa: SLF001
        assert trigger._thread.is_alive()  # noqa: SLF001
    finally:
        trigger.stop(timeout=2.0)
