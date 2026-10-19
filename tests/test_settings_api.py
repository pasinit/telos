"""JSON-API surface for the settings page (PR 7G).

Covers:

- ``GET /api/agents/{slug}/settings`` — payload shape parity with the
  legacy ``settings.html`` render dict.
- ``POST /api/agents/{slug}/{approve,reject,rollback}`` with
  ``Accept: application/json`` returns ``{ok: true, new_prompt: ...}``.
- ``POST /api/agents/{slug}/tools`` updates the per-agent tool whitelist
  and evicts the runtime cache.

The rig follows ``tests/test_auth_dual.py`` — repoint
``storage_root`` + ``_registry`` at ``tmp_path`` so individual tests
don't interfere. Tests that prime agent state (e.g. stage a candidate)
are async so they can await ``_app._get_agent`` without poking the
event-loop singleton (which pytest-asyncio rotates per test).
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telos.improver import CandidatePrompt


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh ``_app`` module + ``TestClient`` repointed at ``tmp_path``."""
    from telos.api import app as _app
    from telos.api.registry import AgentRegistry
    from telos.routines import RoutineStore

    monkeypatch.setattr(_app.settings, "storage_root", tmp_path)
    monkeypatch.setattr(_app.settings, "public_host", "")
    fresh_registry = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(_app, "_registry", fresh_registry)
    monkeypatch.setattr(_app, "_routine_store", RoutineStore(tmp_path / "routines.db"))
    monkeypatch.setattr(_app, "_agent_runtime", _app._build_agent_runtime())

    client = TestClient(_app.app, follow_redirects=False)
    return client, _app


def _auth(srv_module, username: str) -> dict[str, str]:
    """Return a Bearer header signed by the prod signer."""
    token = srv_module._signer.dumps(username)
    return {"Authorization": f"Bearer {token}"}


def _make_agent(srv_module, uid: str, name: str = "Coach") -> str:
    """Create a specialist agent and return its slug."""
    meta = srv_module._registry.create(uid, name, "Helps with X.", "You are a coach.")
    return meta["slug"]


# ──────────────────────────────────────────────────────────────────────
# GET /api/agents/{slug}/settings — shape contract
# ──────────────────────────────────────────────────────────────────────


def test_get_settings_returns_full_payload(srv):
    """The JSON payload includes every key the SSR template was given."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)

    r = client.get(f"/api/agents/{slug}/settings", headers=_auth(_app, uid))
    assert r.status_code == 200
    body = r.json()

    expected_keys = {
        "user_id",
        "agent",
        "current_prompt",
        "prompt_history",
        "signal_summary",
        "has_pending",
        "candidate",
        "agent_tools",
        "tool_groups",
        "routines",
    }
    assert expected_keys.issubset(set(body.keys()))
    assert body["user_id"] == uid
    assert body["agent"]["slug"] == slug
    assert body["current_prompt"] == "You are a coach."
    assert body["has_pending"] is False
    assert body["candidate"] is None
    assert body["agent_tools"] == []
    assert isinstance(body["tool_groups"], list)
    assert isinstance(body["prompt_history"], list)
    assert isinstance(body["signal_summary"], dict)
    assert body["routines"] == []


async def test_get_settings_serialises_pending_candidate(srv):
    """When a candidate is staged, the JSON shape exposes its fields."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    agent = await _app._get_agent(uid, slug)
    candidate = CandidatePrompt(
        sha="abc123",
        content="You are an improved coach.",
        rationale="Cleaner phrasing.",
        score_delta=0.42,
        run_id=7,
    )
    agent._approval.stage(candidate)

    r = client.get(f"/api/agents/{slug}/settings", headers=_auth(_app, uid))
    assert r.status_code == 200
    body = r.json()
    assert body["has_pending"] is True
    assert body["candidate"] == {
        "sha": "abc123",
        "content": "You are an improved coach.",
        "rationale": "Cleaner phrasing.",
        "score_delta": 0.42,
        "run_id": 7,
    }


def test_get_settings_404_on_unknown_slug(srv):
    """Unknown slug → 404."""
    client, _app = srv
    r = client.get("/api/agents/nope/settings", headers=_auth(_app, "alice"))
    assert r.status_code == 404


def test_get_settings_requires_auth(srv):
    """No credentials → 401 on the JSON endpoint (not a 303 redirect)."""
    client, _ = srv
    r = client.get("/api/agents/anything/settings")
    assert r.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# Approve / reject / rollback — dual-shape behaviour
# ──────────────────────────────────────────────────────────────────────


async def test_approve_returns_json_when_requested(srv):
    """``Accept: application/json`` on POST /approve → ``{ok, new_prompt}``."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    agent = await _app._get_agent(uid, slug)
    agent._approval.stage(CandidatePrompt(
        sha="newsha",
        content="You are an even better coach.",
        rationale="Tighter wording.",
        score_delta=0.15,
        run_id=None,
    ))

    r = client.post(
        f"/api/agents/{slug}/approve",
        headers={**_auth(_app, uid), "Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["new_prompt"] == "You are an even better coach."
    assert agent.system_prompt == "You are an even better coach."


async def test_reject_returns_json_when_requested(srv):
    """``Accept: application/json`` on POST /reject → JSON payload."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    agent = await _app._get_agent(uid, slug)
    original = agent.system_prompt
    agent._approval.stage(CandidatePrompt(
        sha="y", content="rejected proposal", rationale="r", score_delta=0.1, run_id=None,
    ))

    r = client.post(
        f"/api/agents/{slug}/reject",
        headers={**_auth(_app, uid), "Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "new_prompt": original}
    assert agent.has_pending_improvement is False


async def test_rollback_returns_json_when_requested(srv):
    """``Accept: application/json`` on POST /rollback returns new prompt."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    agent = await _app._get_agent(uid, slug)
    sha_v1 = agent._log.save_prompt("v1 prompt")
    agent._log.deploy_prompt(sha_v1)
    agent._approval.ensure_version(sha_v1, "v1 prompt")
    sha_v2 = agent._log.save_prompt("v2 prompt")
    agent._log.deploy_prompt(sha_v2)
    agent._approval.ensure_version(sha_v2, "v2 prompt")

    r = client.post(
        f"/api/agents/{slug}/rollback",
        headers={**_auth(_app, uid), "Accept": "application/json"},
        data={"sha": sha_v1},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["new_prompt"] == "v1 prompt"


async def test_activate_routine_via_api_prefix(srv):
    """POST /api/agents/{slug}/routines/{id}/activate enables the routine.
    Regression for the latent /api-prefix bug: the SPA's apiPost always
    prepends /api, but the route used to live at /agents/... — every
    Accept-click on a proposed routine 404'd, leaving the row in the DB
    disabled. The user's symptom was "Action failed" while the routine
    appeared to have been created."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    routine = _app._routine_store.create(
        user_id=uid, agent_slug=slug, description="daily check",
        interval_seconds=86400, enabled=False,
    )

    r = client.post(
        f"/api/agents/{slug}/routines/{routine.id}/activate",
        headers={**_auth(_app, uid), "Accept": "application/json"},
        json={"notify_email": False},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _app._routine_store.get(routine.id).enabled is True


class _FakeRunner:
    """Records add/remove calls the routine routes make on the live runner."""

    def __init__(self) -> None:
        self.added = []
        self.removed = []

    def add(self, routine) -> None:
        self.added.append(routine)

    def remove(self, routine_id: str) -> None:
        self.removed.append(routine_id)


async def test_activate_routine_schedules_on_live_runner(srv, monkeypatch):
    """Activation must register the routine with the live RoutineRunner
    (``app.state.routine_runner``), not just flip the DB row — a row that
    is enabled but unscheduled silently never fires until restart."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    routine = _app._routine_store.create(
        user_id=uid, agent_slug=slug, description="daily check",
        interval_seconds=86400, enabled=False,
    )
    runner = _FakeRunner()
    monkeypatch.setattr(_app.app.state, "routine_runner", runner, raising=False)

    r = client.post(
        f"/api/agents/{slug}/routines/{routine.id}/activate",
        headers={**_auth(_app, uid), "Accept": "application/json"},
        json={"notify_email": True},
    )
    assert r.status_code == 200
    stored = _app._routine_store.get(routine.id)
    assert stored.enabled is True
    assert stored.notify_email is True
    assert [rt.id for rt in runner.added] == [routine.id]
    # The runner must be handed the post-activation state, or APScheduler
    # would schedule a job for a routine it believes is disabled.
    assert runner.added[0].enabled is True
    assert runner.added[0].notify_email is True


async def test_dismiss_routine_deletes_row_and_unschedules(srv, monkeypatch):
    """Dismissing a pending proposal (the pill's second button, also the
    routines panel's Dismiss) deletes the row and defensively removes any
    stale scheduler job."""
    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)
    routine = _app._routine_store.create(
        user_id=uid, agent_slug=slug, description="daily check",
        interval_seconds=86400, enabled=False,
    )
    runner = _FakeRunner()
    monkeypatch.setattr(_app.app.state, "routine_runner", runner, raising=False)

    r = client.post(
        f"/api/agents/{slug}/routines/{routine.id}/dismiss",
        headers={**_auth(_app, uid), "Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _app._routine_store.get(routine.id) is None
    assert runner.removed == [routine.id]
    assert runner.added == []


async def test_update_tools_persists_and_round_trips_via_api_prefix(srv):
    """POST /api/agents/{slug}/tools saves the whitelist and the GET
    endpoint reflects it on the next request. Regression for #124-fallout:
    the route used to live at /agents/{slug}/tools without /api, so the
    SPA's apiPost (which prepends /api) hit a 404 and the picker visibly
    reset to baseline after every save."""
    import json

    client, _app = srv
    uid = "alice"
    slug = _make_agent(_app, uid)

    r = client.post(
        f"/api/agents/{slug}/tools",
        headers={**_auth(_app, uid), "Accept": "application/json"},
        json={"tools": ["web_search", "web_fetch"]},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "tools": ["web_search", "web_fetch"]}

    meta = _app._registry.get(uid, slug)
    assert json.loads(meta["tools"]) == ["web_search", "web_fetch"]

    r = client.get(f"/api/agents/{slug}/settings", headers=_auth(_app, uid))
    assert r.status_code == 200
    assert r.json()["agent_tools"] == ["web_search", "web_fetch"]


