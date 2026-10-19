"""Tests for ``telos.improvement.deploy`` — Phase 5 deploy step.

The deploy helper PUTs a winning candidate's ``system_prompt`` to
Recordatio's AgentConfig API and absorbs the 409 race per
MIGRATION_PLAN.md §"Phase 5":

    On 409, refetch and retry the diff. (...) if it 409s again, abort
    that cycle and log.

Pins:

- 200 → success, returns server payload.
- 404 → terminal ``ConfigDeployFailed`` (agent not seeded in Recordatio).
- Single 409 followed by 200 on refetched config → success.
- Two 409s in a row → ``ConfigDeployFailed`` (cycle aborted).
- Non-200/409/404 status → ``ConfigDeployFailed``.
- Refetch returning ``None`` after a 409 → ``ConfigDeployFailed``.

The tests inject a fake :class:`RecordatioConfigClient` so no httpx
machinery is exercised; the client surface that
``deploy_candidate_to_recordatio`` touches is small (``get`` + ``put``).
"""
from typing import Any

import pytest

from telos.improvement.deploy import (
    ConfigDeployFailed,
    deploy_candidate_to_recordatio,
)

# ──────────────────────────────────────────────────────────────────────
# Fake client
# ──────────────────────────────────────────────────────────────────────

class _FakeClient:
    """Scriptable stand-in for :class:`RecordatioConfigClient`.

    ``put_responses`` is a list of ``(body, status_code)`` tuples the
    fake hands out in order on each ``put`` call. ``get_responses`` is
    the same for ``get``. Tests assert against ``put_calls`` /
    ``get_calls`` to verify the deploy helper made the right
    round-trips.
    """

    def __init__(
        self,
        *,
        put_responses: list[tuple[dict[str, Any] | None, int]],
        get_responses: list[dict[str, Any] | None] | None = None,
    ) -> None:
        self._put_responses = list(put_responses)
        self._get_responses = list(get_responses or [])
        self.put_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, str]] = []

    def get(self, user_id: str, slug: str) -> dict[str, Any] | None:
        self.get_calls.append((user_id, slug))
        if not self._get_responses:
            raise AssertionError(
                f"unexpected get({user_id!r}, {slug!r}) — no responses queued"
            )
        return self._get_responses.pop(0)

    def put(
        self, user_id: str, slug: str, config: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, int]:
        self.put_calls.append((user_id, slug, dict(config)))
        if not self._put_responses:
            raise AssertionError(
                f"unexpected put({user_id!r}, {slug!r}) — no responses queued"
            )
        return self._put_responses.pop(0)

    def close(self) -> None:  # pragma: no cover (not exercised in tests)
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_exc) -> None:
        pass


# ──────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────

def test_happy_path_single_put_returns_server_payload():
    initial = {
        "system_prompt": "old",
        "model": "claude-haiku-4-5",
        "version": 7,
        "mcp_servers": [],
        "subagents": [],
        "allowed_tools": None,
    }
    fake = _FakeClient(put_responses=[({"system_prompt": "new", "version": 8}, 200)])
    result = deploy_candidate_to_recordatio(
        client=fake,
        user_id="alice",
        slug="weather",
        new_system_prompt="new",
        initial_config=initial,
    )
    assert result == {"system_prompt": "new", "version": 8}
    # Single PUT carrying the original version and the new prompt;
    # every other field preserved.
    assert len(fake.put_calls) == 1
    _, _, sent = fake.put_calls[0]
    assert sent["system_prompt"] == "new"
    assert sent["version"] == 7
    assert sent["model"] == "claude-haiku-4-5"
    assert fake.get_calls == []  # happy path needs no GET


def test_404_raises_immediately():
    """The 404 path means Recordatio has never seen this agent. We don't
    retry — the next trigger cycle would just hit the same 404."""
    fake = _FakeClient(put_responses=[(None, 404)])
    with pytest.raises(ConfigDeployFailed) as excinfo:
        deploy_candidate_to_recordatio(
            client=fake,
            user_id="alice",
            slug="weather",
            new_system_prompt="new",
            initial_config={"version": 1, "system_prompt": "old"},
        )
    assert "not found" in str(excinfo.value).lower()
    assert fake.get_calls == []


def test_409_then_200_succeeds_after_refetch():
    """The plan's "one refetch + retry" path: first PUT 409s, we GET
    the new config, then PUT once more against the fresh version."""
    initial = {"system_prompt": "old", "version": 3, "model": "m"}
    refetched = {"system_prompt": "concurrent", "version": 4, "model": "m"}
    fake = _FakeClient(
        put_responses=[
            ({"error": "version mismatch", "expected": 3, "actual": 4}, 409),
            ({"system_prompt": "new", "version": 5}, 200),
        ],
        get_responses=[refetched],
    )
    result = deploy_candidate_to_recordatio(
        client=fake,
        user_id="alice",
        slug="weather",
        new_system_prompt="new",
        initial_config=initial,
    )
    assert result == {"system_prompt": "new", "version": 5}
    assert len(fake.put_calls) == 2
    # First PUT carried the stale version; second PUT carried the refetched one.
    assert fake.put_calls[0][2]["version"] == 3
    assert fake.put_calls[1][2]["version"] == 4
    assert fake.put_calls[1][2]["system_prompt"] == "new"
    assert fake.get_calls == [("alice", "weather")]


def test_two_409s_aborts_cycle():
    """Per the plan: if it 409s again, abort that cycle and log.

    The trigger will re-fire on the next threshold crossing; we don't
    keep retrying inside a single cycle.
    """
    initial = {"system_prompt": "old", "version": 3}
    refetched = {"system_prompt": "concurrent", "version": 4}
    fake = _FakeClient(
        put_responses=[
            (None, 409),
            (None, 409),  # second 409 → abort
        ],
        get_responses=[refetched],
    )
    with pytest.raises(ConfigDeployFailed):
        deploy_candidate_to_recordatio(
            client=fake,
            user_id="alice",
            slug="weather",
            new_system_prompt="new",
            initial_config=initial,
        )
    # Exactly one refetch attempted; no third PUT.
    assert len(fake.put_calls) == 2
    assert fake.get_calls == [("alice", "weather")]


def test_409_then_gone_aborts():
    """If the agent disappears between the 409 and the refetch
    (concurrent delete), abort cleanly rather than crashing on a
    NoneType subscript."""
    fake = _FakeClient(
        put_responses=[(None, 409)],
        get_responses=[None],  # GET returns 404 internally → None
    )
    with pytest.raises(ConfigDeployFailed) as excinfo:
        deploy_candidate_to_recordatio(
            client=fake,
            user_id="alice",
            slug="weather",
            new_system_prompt="new",
            initial_config={"version": 3, "system_prompt": "old"},
        )
    assert "disappeared" in str(excinfo.value).lower()


def test_non_409_non_200_status_raises():
    """Anything other than 200/404/409 (e.g. a 500 from Recordatio) is a
    terminal failure — no retry, abort cleanly."""
    fake = _FakeClient(put_responses=[({"error": "boom"}, 500)])
    with pytest.raises(ConfigDeployFailed):
        deploy_candidate_to_recordatio(
            client=fake,
            user_id="alice",
            slug="weather",
            new_system_prompt="new",
            initial_config={"version": 1, "system_prompt": "old"},
        )


def test_initial_config_is_not_mutated():
    """``deploy_candidate_to_recordatio`` must not mutate the caller's
    ``initial_config`` dict — the caller may want to log it post-PUT,
    or hand the same dict to a sibling improver run."""
    initial = {"system_prompt": "old", "version": 3, "model": "m"}
    snapshot = dict(initial)
    fake = _FakeClient(put_responses=[({"version": 4}, 200)])
    deploy_candidate_to_recordatio(
        client=fake,
        user_id="alice",
        slug="weather",
        new_system_prompt="new",
        initial_config=initial,
    )
    assert initial == snapshot
