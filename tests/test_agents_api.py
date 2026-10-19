"""Contract tests for the JSON ``/api/agents`` endpoints (PR 7F).

Covers list / create / delete shapes. The legacy SSR routes
(``GET /agents``, ``POST /agents`` form-POST) and the housekeeper-
proposal path (``POST /api/agents/create``) are NOT exercised here —
they have their own coverage and live until PR 7K.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telos.housekeeper import HOUSEKEEPER_SLUG


@pytest.fixture
def srv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield ``(client, telos.api.app, signed_cookie_for("alice"))``.

    Mirrors the rig in ``test_usage.py`` / ``test_auth_dual.py``:
    repoint ``storage_root`` + ``_registry`` at ``tmp_path`` so tests
    don't see each other's data.
    """
    from telos.api import app as _app
    from telos.api.registry import AgentRegistry

    monkeypatch.setattr(_app.settings, "storage_root", tmp_path)
    fresh = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(_app, "_registry", fresh)
    monkeypatch.setattr(_app, "_agent_runtime", _app._build_agent_runtime())

    client = TestClient(_app.app, follow_redirects=False)
    cookie = _app._signer.dumps("alice")
    return client, _app, cookie


# ──────────────────────────────────────────────────────────────────────
# GET /api/agents
# ──────────────────────────────────────────────────────────────────────


def test_list_requires_auth(srv):
    """No cookie / no bearer → 401 on the JSON endpoint."""
    client, _, _ = srv
    r = client.get("/api/agents")
    assert r.status_code == 401


def test_list_empty_user_returns_empty_array(srv):
    """A fresh user has no registry rows. The endpoint does NOT auto-seed
    the housekeeper — ``list_for_user`` is a pure read per
    ``RegistryRepo.list_for_user``."""
    client, _, cookie = srv
    r = client.get("/api/agents", cookies={"user_id": cookie})
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_seeded_agents_with_shape(srv):
    """Seeded specialists appear with the documented shape."""
    client, _app, cookie = srv
    _app._registry.create("alice", "Cooking Coach", "Helps with recipes", "system prompt")
    _app._registry.create("alice", "Travel Bot", "Plans trips", "system prompt 2")

    r = client.get("/api/agents", cookies={"user_id": cookie})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 2

    names = sorted(a["name"] for a in body)
    assert names == ["Cooking Coach", "Travel Bot"]

    # Shape contract
    sample = body[0]
    assert set(sample.keys()) >= {"slug", "name", "description", "created_at", "last_active"}
    assert isinstance(sample["slug"], str) and sample["slug"]
    assert isinstance(sample["name"], str) and sample["name"]


def test_list_includes_housekeeper_when_seeded(srv):
    """If the housekeeper row exists (via ``ensure_housekeeper`` or a
    prior ``get(uid, HOUSEKEEPER_SLUG)``), it must appear in the list —
    the SPA sidebar relies on this to render the housekeeper entry."""
    client, _app, cookie = srv
    _app._registry.ensure_housekeeper("alice")
    _app._registry.create("alice", "Specialist", "x", "y")

    r = client.get("/api/agents", cookies={"user_id": cookie})
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()]
    assert HOUSEKEEPER_SLUG in slugs
    assert any(s != HOUSEKEEPER_SLUG for s in slugs)


def test_list_isolated_per_user(srv):
    """One user's agents never leak into another's list."""
    client, _app, cookie_alice = srv
    _app._registry.create("alice", "Alice Agent", "x", "y")
    _app._registry.create("bob", "Bob Agent", "x", "y")
    r = client.get("/api/agents", cookies={"user_id": cookie_alice})
    assert r.status_code == 200
    names = [a["name"] for a in r.json()]
    assert names == ["Alice Agent"]


# ──────────────────────────────────────────────────────────────────────
# POST /api/agents
# ──────────────────────────────────────────────────────────────────────


def _stub_agenerate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``Goal.agenerate`` so the create endpoint doesn't reach LLMs."""
    from telos import Goal

    async def _fake(cls, name, description, **kwargs):  # type: ignore[no-untyped-def]
        return Goal(
            name=name.strip(),
            description=description.strip(),
            system_prompt=f"prompt for {name.strip()}",
            tools=[],
        )

    monkeypatch.setattr(Goal, "agenerate", classmethod(_fake))


def test_create_requires_auth(srv):
    client, _, _ = srv
    r = client.post("/api/agents", json={"name": "x", "description": "y"})
    assert r.status_code == 401


def test_create_returns_slug_and_name(srv, monkeypatch):
    """Happy path: JSON in, ``{slug, name}`` out, row persisted."""
    client, _app, cookie = srv
    _stub_agenerate(monkeypatch)
    r = client.post(
        "/api/agents",
        json={"name": "Recipe Bot", "description": "Suggests dinners"},
        cookies={"user_id": cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"slug", "name"}
    assert body["name"] == "Recipe Bot"
    assert body["slug"] == "recipe-bot"

    # The registry row was actually written.
    rows = _app._registry.list_for_user("alice")
    assert len(rows) == 1
    assert rows[0]["slug"] == "recipe-bot"
    assert rows[0]["system_prompt"] == "prompt for Recipe Bot"


def test_create_strips_whitespace(srv, monkeypatch):
    """Leading/trailing whitespace in name + description is trimmed."""
    client, _app, cookie = srv
    _stub_agenerate(monkeypatch)
    r = client.post(
        "/api/agents",
        json={"name": "  Trimmed  ", "description": "  helps with stuff "},
        cookies={"user_id": cookie},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Trimmed"
    assert r.json()["slug"] == "trimmed"


@pytest.mark.parametrize("payload", [
    {"name": "", "description": "x"},
    {"name": "   ", "description": "x"},
    {"name": "x", "description": ""},
    {"name": "x", "description": "   "},
    {"name": "x"},                         # missing field
    {"description": "x"},                  # missing field
    {},
])
def test_create_rejects_missing_or_blank_fields(srv, monkeypatch, payload):
    """Blank / missing name or description → 400, no row created."""
    client, _app, cookie = srv
    _stub_agenerate(monkeypatch)
    r = client.post("/api/agents", json=payload, cookies={"user_id": cookie})
    assert r.status_code == 400
    assert _app._registry.list_for_user("alice") == []


def test_create_does_not_notify_housekeeper(srv, monkeypatch):
    """The plain create path is distinct from ``/api/agents/create`` —
    it must NOT call ``append_user_context`` on the housekeeper.

    This is the contract that lets the housekeeper-proposal path stay
    a separate endpoint with its own side-effects.
    """
    client, _app, cookie = srv
    _stub_agenerate(monkeypatch)

    called = {"flag": False}

    async def _fake_get_housekeeper(uid):  # pragma: no cover - asserted below
        called["flag"] = True
        raise AssertionError("housekeeper must not be touched by POST /api/agents")

    monkeypatch.setattr(_app, "_get_housekeeper", _fake_get_housekeeper)
    r = client.post(
        "/api/agents",
        json={"name": "Quiet", "description": "no side effects"},
        cookies={"user_id": cookie},
    )
    assert r.status_code == 200
    assert called["flag"] is False


# ──────────────────────────────────────────────────────────────────────
# DELETE /agents/{slug} — already JSON; covered here for the full
# list / create / delete contract per PR 7F brief.
# ──────────────────────────────────────────────────────────────────────


def test_delete_api_path_requires_auth(srv):
    """``DELETE /api/agents/{slug}`` is the SPA-facing mirror; unauth
    surfaces as 401 (machine-readable for the SPA's 401 → /login dance)."""
    client, _, _ = srv
    r = client.delete("/api/agents/some-slug")
    assert r.status_code == 401


def test_delete_returns_ok_and_removes_row(srv, monkeypatch):
    """``DELETE /api/agents/{slug}`` returns ``{"ok": true}`` and the
    row disappears from subsequent list calls.
    """
    client, _app, cookie = srv
    _stub_agenerate(monkeypatch)
    create_r = client.post(
        "/api/agents",
        json={"name": "Doomed", "description": "to be deleted"},
        cookies={"user_id": cookie},
    )
    assert create_r.status_code == 200
    slug = create_r.json()["slug"]

    del_r = client.delete(f"/api/agents/{slug}", cookies={"user_id": cookie})
    assert del_r.status_code == 200
    assert del_r.json() == {"ok": True}

    # Gone from the list.
    list_r = client.get("/api/agents", cookies={"user_id": cookie})
    assert all(a["slug"] != slug for a in list_r.json())
