"""Tests for ``telos.services.improvement.ImprovementService`` — Phase 5.

Phase 5 replaced the legacy ``TurnComplete``-driven counter with a
polling-based :class:`MessageCountTrigger` and moved the auto-deploy
step from in-process Agent mutation to ``PUT
/v1/agents/{user_id}/{slug}/config`` against Recordatio. These tests pin:

- Trigger fires submit an improver run on the scheduler.
- Low-delta candidate → staged in ApprovalManager, no deploy PUT.
- High-delta candidate → staged + PUT to Recordatio with the version
  captured at the start of the cycle.
- Missing Recordatio AgentConfig (None) → skip cycle, log warning.
- 409 on PUT → refetch + retry once; success on retry counts as
  auto-deploy.
- Two 409s → abort cycle, log, no crash.
- Improver returning None → no stage, no deploy.
- Improver raising → no stage, no deploy, service stays alive.
- Scheduler shutdown drops the submission quietly.

The tests inject a fake ``RecordatioConfigClient`` via the service's
``config_client_factory`` seam — no httpx machinery is exercised.
"""
import logging
import threading
import time
from collections.abc import Callable

import pytest

from telos.domain import AgentImprovementState
from telos.improver import AUTO_DEPLOY_THRESHOLD, CandidatePrompt
from telos.services.improvement import ImprovementService
from telos.services.scheduler import BackgroundScheduler

# ──────────────────────────────────────────────────────────────────────
# Test doubles
# ──────────────────────────────────────────────────────────────────────

class _FakeLog:
    def __init__(self, count: int = 0) -> None:
        self._count = count

    def count(self) -> int:
        return self._count

    def set_count(self, n: int) -> None:
        self._count = n


class _FakeImprover:
    """Async ``run`` returns ``return_value`` or raises ``raises``."""

    def __init__(
        self,
        return_value: CandidatePrompt | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.return_value = return_value
        self.raises = raises
        self.calls: list[str] = []
        self._done = threading.Event()

    async def run(self, current_prompt: str) -> CandidatePrompt | None:
        import asyncio
        self.calls.append(current_prompt)
        await asyncio.sleep(0)
        if self.raises is not None:
            self._done.set()
            raise self.raises
        self._done.set()
        return self.return_value

    def wait_done(self, timeout: float = 2.0) -> bool:
        return self._done.wait(timeout)


class _FakeApproval:
    def __init__(self) -> None:
        self.staged: list[CandidatePrompt] = []

    def stage(self, candidate: CandidatePrompt) -> None:
        self.staged.append(candidate)


class _FakeConfigClient:
    """Scriptable Recordatio config client.

    ``get_responses`` and ``put_responses`` are queues. The first
    response in each is consumed by the corresponding call. Tests assert
    on ``put_calls`` / ``get_calls`` to verify the wire round-trips.
    """

    def __init__(
        self,
        *,
        get_responses: list[dict | None] | None = None,
        put_responses: list[tuple[dict | None, int]] | None = None,
    ) -> None:
        self._get_responses = list(get_responses or [])
        self._put_responses = list(put_responses or [])
        self.get_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, str, dict]] = []
        self.closed = False

    def get(self, user_id: str, slug: str):
        self.get_calls.append((user_id, slug))
        if not self._get_responses:
            raise AssertionError("unexpected get — no response queued")
        return self._get_responses.pop(0)

    def put(self, user_id: str, slug: str, config: dict):
        self.put_calls.append((user_id, slug, dict(config)))
        if not self._put_responses:
            raise AssertionError("unexpected put — no response queued")
        return self._put_responses.pop(0)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _make_state(
    *,
    log: _FakeLog | None = None,
    improver: _FakeImprover | None = None,
    approval: _FakeApproval | None = None,
    current_prompt: str = "v1",
    approve_fn: Callable[[], None] | None = None,
) -> AgentImprovementState:
    return AgentImprovementState(
        improver=improver or _FakeImprover(),
        approval=approval or _FakeApproval(),
        conversation_log=log or _FakeLog(),
        get_current_prompt=lambda: current_prompt,
        approve_candidate=approve_fn or (lambda: None),
    )


@pytest.fixture
def scheduler():
    s = BackgroundScheduler(max_workers=2, name="test-improvement")
    yield s
    s.shutdown(wait=True)


def _make_service(
    scheduler: BackgroundScheduler,
    *,
    state: AgentImprovementState,
    client: _FakeConfigClient,
    threshold: int = 1,
) -> ImprovementService:
    """Build a service wired to a single ``("alice", "weather")`` agent."""
    return ImprovementService(
        scheduler,
        list_pairs_fn=lambda: [("alice", "weather")],
        get_agent_state_fn=lambda u, s: state if (u, s) == ("alice", "weather") else None,
        recordatio_url="http://recordatio.test",
        recordatio_api_key="test-key",
        improve_every=threshold,
        config_client_factory=lambda: client,
    )


# ──────────────────────────────────────────────────────────────────────
# Trigger → scheduler hand-off
# ──────────────────────────────────────────────────────────────────────

def test_below_threshold_does_not_submit(scheduler):
    log = _FakeLog(count=0)
    improver = _FakeImprover()
    state = _make_state(log=log, improver=improver)
    client = _FakeConfigClient()  # never called → asserts via no queued responses
    svc = _make_service(scheduler, state=state, client=client, threshold=5)
    svc.trigger.poll_once()  # seed at 0, no fire
    log.set_count(1)
    svc.trigger.poll_once()
    time.sleep(0.05)  # let any erroneous submission show up
    assert improver.calls == []
    assert client.get_calls == []
    assert client.put_calls == []


def test_threshold_crossing_submits_improver_run(scheduler):
    log = _FakeLog(count=0)
    candidate = CandidatePrompt(
        sha="abc", content="new", rationale="r",
        score_delta=0.05,  # low — stage only, no deploy
    )
    improver = _FakeImprover(return_value=candidate)
    approval = _FakeApproval()
    state = _make_state(log=log, improver=improver, approval=approval)
    initial_cfg = {"system_prompt": "v1", "version": 3, "model": "m"}
    client = _FakeConfigClient(get_responses=[initial_cfg])
    svc = _make_service(scheduler, state=state, client=client, threshold=2)
    svc.trigger.poll_once()  # seed at 0
    log.set_count(2)
    svc.trigger.poll_once()  # crosses threshold
    assert improver.wait_done(timeout=2.0)
    time.sleep(0.1)
    assert improver.calls == ["v1"]
    assert approval.staged == [candidate]
    # Low-delta path: no PUT (the GET is for the initial config snapshot).
    assert client.get_calls == [("alice", "weather")]
    assert client.put_calls == []


# ──────────────────────────────────────────────────────────────────────
# Auto-deploy via Recordatio PUT
# ──────────────────────────────────────────────────────────────────────

def test_high_delta_deploys_via_recordatio_put(scheduler):
    log = _FakeLog(count=0)
    candidate = CandidatePrompt(
        sha="abc", content="new prompt", rationale="r",
        score_delta=AUTO_DEPLOY_THRESHOLD + 0.05,
    )
    improver = _FakeImprover(return_value=candidate)
    approval = _FakeApproval()
    state = _make_state(log=log, improver=improver, approval=approval)
    initial_cfg = {
        "system_prompt": "old", "version": 7, "model": "m",
        "mcp_servers": [], "subagents": [], "allowed_tools": None,
    }
    client = _FakeConfigClient(
        get_responses=[initial_cfg],
        put_responses=[({"system_prompt": "new prompt", "version": 8}, 200)],
    )
    svc = _make_service(scheduler, state=state, client=client, threshold=1)
    svc.trigger.poll_once()  # seed
    log.set_count(1)
    svc.trigger.poll_once()  # crosses

    assert improver.wait_done(timeout=2.0)
    time.sleep(0.1)
    assert approval.staged == [candidate]
    assert len(client.put_calls) == 1
    _, _, sent = client.put_calls[0]
    assert sent["system_prompt"] == "new prompt"
    # The PUT must carry the version we read at the START of the cycle.
    assert sent["version"] == 7


def test_missing_recordatio_config_skips_cycle(scheduler, caplog):
    """If Recordatio has never seen the agent, we can't deploy. Skip the
    whole cycle instead of running the improver against a config we
    can't ship to.
    """
    log = _FakeLog(count=0)
    improver = _FakeImprover()
    state = _make_state(log=log, improver=improver)
    client = _FakeConfigClient(get_responses=[None])  # 404 from Recordatio
    svc = _make_service(scheduler, state=state, client=client, threshold=1)
    svc.trigger.poll_once()  # seed
    log.set_count(1)
    with caplog.at_level(logging.WARNING, logger="telos.services.improvement"):
        svc.trigger.poll_once()
    time.sleep(0.05)
    # Improver never ran because there's nowhere to deploy.
    assert improver.calls == []
    assert any("no Recordatio AgentConfig" in r.message for r in caplog.records)


def test_409_then_200_succeeds_after_refetch(scheduler):
    """First PUT 409s → refetch → second PUT 200. End state is
    auto-deployed against the refetched version."""
    log = _FakeLog(count=0)
    candidate = CandidatePrompt(
        sha="abc", content="new", rationale="r",
        score_delta=AUTO_DEPLOY_THRESHOLD + 0.05,
    )
    improver = _FakeImprover(return_value=candidate)
    state = _make_state(log=log, improver=improver)
    initial_cfg = {"system_prompt": "old", "version": 3, "model": "m"}
    refetched = {"system_prompt": "concurrent", "version": 4, "model": "m"}
    client = _FakeConfigClient(
        # First get: initial_config snapshot before the improver runs.
        # Second get: refetch after the 409.
        get_responses=[initial_cfg, refetched],
        put_responses=[(None, 409), ({"version": 5}, 200)],
    )
    svc = _make_service(scheduler, state=state, client=client, threshold=1)
    svc.trigger.poll_once()
    log.set_count(1)
    svc.trigger.poll_once()
    assert improver.wait_done(timeout=2.0)
    time.sleep(0.1)
    assert len(client.put_calls) == 2
    # Second PUT used the refetched version.
    assert client.put_calls[1][2]["version"] == 4


def test_two_409s_aborts_cycle(scheduler, caplog):
    log = _FakeLog(count=0)
    candidate = CandidatePrompt(
        sha="abc", content="new", rationale="r",
        score_delta=AUTO_DEPLOY_THRESHOLD + 0.05,
    )
    improver = _FakeImprover(return_value=candidate)
    state = _make_state(log=log, improver=improver)
    initial_cfg = {"system_prompt": "old", "version": 3}
    refetched = {"system_prompt": "concurrent", "version": 4}
    client = _FakeConfigClient(
        get_responses=[initial_cfg, refetched],
        put_responses=[(None, 409), (None, 409)],
    )
    svc = _make_service(scheduler, state=state, client=client, threshold=1)
    svc.trigger.poll_once()
    log.set_count(1)
    with caplog.at_level(logging.ERROR, logger="telos.services.improvement"):
        svc.trigger.poll_once()
    assert improver.wait_done(timeout=2.0)
    time.sleep(0.1)
    assert len(client.put_calls) == 2
    assert any("auto-deploy failed" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────
# Resilience
# ──────────────────────────────────────────────────────────────────────

def test_improver_returning_none_stages_nothing(scheduler):
    log = _FakeLog(count=0)
    approval = _FakeApproval()
    improver = _FakeImprover(return_value=None)
    state = _make_state(log=log, improver=improver, approval=approval)
    initial_cfg = {"system_prompt": "old", "version": 3}
    client = _FakeConfigClient(get_responses=[initial_cfg])
    svc = _make_service(scheduler, state=state, client=client, threshold=1)
    svc.trigger.poll_once()
    log.set_count(1)
    svc.trigger.poll_once()
    assert improver.wait_done(timeout=2.0)
    time.sleep(0.05)
    assert approval.staged == []
    assert client.put_calls == []


def test_improver_exception_does_not_break_service(scheduler, caplog):
    log = _FakeLog(count=0)
    improver = _FakeImprover(raises=RuntimeError("boom"))
    state = _make_state(log=log, improver=improver)
    initial_cfg = {"system_prompt": "old", "version": 3}
    client = _FakeConfigClient(get_responses=[initial_cfg])
    svc = _make_service(scheduler, state=state, client=client, threshold=1)
    svc.trigger.poll_once()
    log.set_count(1)
    with caplog.at_level(logging.ERROR, logger="telos.services.improvement"):
        svc.trigger.poll_once()
    assert improver.wait_done(timeout=2.0)
    time.sleep(0.05)
    # Service still alive, trigger still tracking last_seen correctly.
    assert svc.trigger.last_seen("alice", "weather") == 1


def test_scheduler_shutdown_drops_submission_quietly(caplog):
    log = _FakeLog(count=0)
    candidate = CandidatePrompt(
        sha="abc", content="new", rationale="r",
        score_delta=AUTO_DEPLOY_THRESHOLD + 0.05,
    )
    improver = _FakeImprover(return_value=candidate)
    state = _make_state(log=log, improver=improver)
    initial_cfg = {"system_prompt": "old", "version": 3}
    client = _FakeConfigClient(get_responses=[initial_cfg])
    sched = BackgroundScheduler(max_workers=1, name="shutdown-test")
    sched.shutdown(wait=True)
    svc = _make_service(sched, state=state, client=client, threshold=1)
    svc.trigger.poll_once()
    log.set_count(1)
    with caplog.at_level(logging.INFO, logger="telos.services.improvement"):
        svc.trigger.poll_once()
    assert improver.calls == []
    assert any("scheduler shutting down" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────
# Wiring
# ──────────────────────────────────────────────────────────────────────

def test_service_does_not_subscribe_to_event_bus(scheduler):
    """Phase 5 explicitly removes the ``TurnComplete`` subscription —
    the trigger is the only fan-in path. This test guards against a
    regression that re-adds the subscription.
    """
    log = _FakeLog(count=0)
    state = _make_state(log=log)
    client = _FakeConfigClient()
    svc = _make_service(scheduler, state=state, client=client, threshold=5)
    # The bus is no longer a constructor argument, but if someone
    # added the subscription back as a side effect, we'd see it via the
    # trigger reaching for it. The simplest check: the service exposes
    # only the trigger seam.
    assert hasattr(svc, "trigger")
    assert not hasattr(svc, "_event_bus")
