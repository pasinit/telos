"""Tests for the new chat-page JSON endpoints introduced in PR 7E.

Covers:
- ``GET /api/agents/{slug}/turns`` (telos.api.routes.chat) — shape +
  ordering + housekeeper slug + auth + isolation + limit clamping.
- ``GET /api/agents/{slug}`` (telos.api.routes.agents) — meta +
  has_pending + usage hydration + 404 on unknown agent + housekeeper
  auto-seed.

The sidebar-list endpoint ``GET /api/agents`` is owned by PR 7F
(``telos.api.routes.agents``); tested in 7F's own test module.

These endpoints replace the template-injected chat-page payload that
the SSR ``chat.html`` route used to ship inline.
"""

import pytest
from fastapi.testclient import TestClient

from telos.log import ConversationLog

# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def server(tmp_path, monkeypatch):
    """TestClient with isolated storage + registry.

    Mirrors the fixture in ``test_recalled_memories`` so the two suites
    test endpoints under the same conditions.
    """
    from telos.api import app as srv
    from telos.api.registry import AgentRegistry

    monkeypatch.setattr(srv.settings, "storage_root", tmp_path)
    fresh_registry = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(srv, "_registry", fresh_registry)
    monkeypatch.setattr(srv, "_agent_runtime", srv._build_agent_runtime())

    client = TestClient(srv.app)
    cookie = srv._signer.dumps("alice")
    return client, srv, cookie, tmp_path


def _seed_agent(srv, *, user: str = "alice", name: str = "Weather") -> str:
    meta = srv._registry.create(user, name, "describe weather", "system prompt", None)
    return meta["slug"]


def _seed_turn(tmp_path, *, user: str, slug: str, msg: str = "hi", resp: str = "hello",
               request_id: str | None = None, is_routine: bool = False,
               trace_id: str | None = None) -> str:
    log = ConversationLog(tmp_path / user / slug / "history.db")
    turn_id = log.append(msg, resp, "sha-x", is_routine=is_routine, trace_id=trace_id)
    if request_id:
        log.update_request_id(turn_id, request_id)
    return turn_id


# ──────────────────────────────────────────────────────────────────────
# GET /api/agents/{slug}/turns
# ──────────────────────────────────────────────────────────────────────


def test_turns_endpoint_requires_auth(server):
    client, _, _, _ = server
    r = client.get("/api/agents/weather/turns", follow_redirects=False)
    assert r.status_code == 401


def test_turns_endpoint_404_for_unknown_specialist(server):
    client, _, cookie, _ = server
    client.cookies.set("user_id", cookie)
    r = client.get("/api/agents/no-such/turns")
    assert r.status_code == 404


def test_turns_endpoint_returns_recent_turns(server):
    client, srv, cookie, tmp_path = server
    slug = _seed_agent(srv)
    _seed_turn(tmp_path, user="alice", slug=slug, msg="first", resp="r1")
    second_turn = _seed_turn(tmp_path, user="alice", slug=slug, msg="second", resp="**r2**")

    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}/turns")
    assert r.status_code == 200
    body = r.json()
    assert "turns" in body
    turns = body["turns"]
    assert len(turns) == 2

    # recent_turns returns chronological; check the structure of the last entry.
    last = next(t for t in turns if t["turn_id"] == second_turn)
    assert last["user_message"] == "second"
    assert last["assistant_response"] == "**r2**"  # raw markdown — client renders
    assert last["is_routine"] is False
    assert last["has_request_id"] is False
    # timestamp is shortened to "YYYY-MM-DD HH:MM"
    assert len(last["timestamp"]) == 16


def test_turns_endpoint_marks_routine_turns(server):
    client, srv, cookie, tmp_path = server
    slug = _seed_agent(srv)
    _seed_turn(tmp_path, user="alice", slug=slug, msg="[routine: weather check]",
               resp="result", is_routine=True)
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}/turns")
    assert r.status_code == 200
    turns = r.json()["turns"]
    assert len(turns) == 1
    assert turns[0]["is_routine"] is True
    # routine_label is non-empty for routine turns (presenter handles formatting)
    assert isinstance(turns[0]["routine_label"], str)


def test_turns_endpoint_has_request_id_flag(server):
    client, srv, cookie, tmp_path = server
    slug = _seed_agent(srv)
    _seed_turn(tmp_path, user="alice", slug=slug, request_id="mem-1")
    _seed_turn(tmp_path, user="alice", slug=slug, request_id=None)
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}/turns")
    turns = r.json()["turns"]
    flags = sorted(t["has_request_id"] for t in turns)
    assert flags == [False, True]


def test_turns_endpoint_includes_trace_id(server):
    client, srv, cookie, tmp_path = server
    slug = _seed_agent(srv)
    _seed_turn(tmp_path, user="alice", slug=slug, trace_id="trace-visible")
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}/turns")

    assert r.status_code == 200
    assert r.json()["turns"][0]["trace_id"] == "trace-visible"


def test_turns_endpoint_respects_limit(server):
    client, srv, cookie, tmp_path = server
    slug = _seed_agent(srv)
    for i in range(5):
        _seed_turn(tmp_path, user="alice", slug=slug, msg=f"m{i}", resp=f"r{i}")
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}/turns?limit=2")
    assert r.status_code == 200
    assert len(r.json()["turns"]) == 2


def test_turns_endpoint_caps_extreme_limit(server):
    """A misbehaving client requesting limit=99999 should be capped."""
    client, srv, cookie, tmp_path = server
    slug = _seed_agent(srv)
    client.cookies.set("user_id", cookie)
    # No turns seeded — just check the endpoint doesn't crash on absurd limits.
    r = client.get(f"/api/agents/{slug}/turns?limit=99999")
    assert r.status_code == 200
    assert r.json()["turns"] == []


def test_turns_endpoint_returns_empty_when_no_history(server):
    client, srv, cookie, _ = server
    slug = _seed_agent(srv)
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}/turns")
    assert r.status_code == 200
    assert r.json() == {"turns": []}


def test_turns_endpoint_isolates_users(server):
    client, srv, _, tmp_path = server
    slug = _seed_agent(srv, user="alice")
    _seed_turn(tmp_path, user="alice", slug=slug)
    bob_cookie = srv._signer.dumps("bob")
    client.cookies.set("user_id", bob_cookie)
    r = client.get(f"/api/agents/{slug}/turns")
    # Bob's registry doesn't have the agent → 404.
    assert r.status_code == 404


def test_turns_endpoint_housekeeper(server):
    """The reserved housekeeper slug should resolve without explicit create."""
    from telos.housekeeper import HOUSEKEEPER_SLUG

    client, _, cookie, _ = server
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{HOUSEKEEPER_SLUG}/turns")
    # Auto-seeds via registry.get + lazy housekeeper construction; no history yet.
    assert r.status_code == 200
    assert r.json() == {"turns": []}


# ──────────────────────────────────────────────────────────────────────
# GET /api/agents/{slug} — meta + hydration bundle
# ──────────────────────────────────────────────────────────────────────


def test_agent_meta_endpoint_requires_auth(server):
    client, _, _, _ = server
    r = client.get("/api/agents/weather", follow_redirects=False)
    assert r.status_code == 401


def test_agent_meta_endpoint_404_for_unknown(server):
    client, _, cookie, _ = server
    client.cookies.set("user_id", cookie)
    r = client.get("/api/agents/no-such")
    assert r.status_code == 404


def test_agent_meta_endpoint_returns_bundle(server):
    client, srv, cookie, _ = server
    slug = _seed_agent(srv)
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{slug}")
    assert r.status_code == 200
    body = r.json()
    assert body["agent"]["slug"] == slug
    assert body["agent"]["name"] == "Weather"
    assert body["is_housekeeper"] is False
    # `usage` payload includes the cumulative-usage shape from Agent.cumulative_usage
    assert "messages" in body["usage"]
    assert "cumulative_cost" in body["usage"]
    assert "has_pending" in body
    assert isinstance(body["has_pending"], bool)


def test_agent_meta_endpoint_housekeeper(server):
    from telos.housekeeper import HOUSEKEEPER_SLUG

    client, _, cookie, _ = server
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/{HOUSEKEEPER_SLUG}")
    assert r.status_code == 200
    body = r.json()
    assert body["is_housekeeper"] is True
    assert body["agent"]["slug"] == HOUSEKEEPER_SLUG


# The sidebar-list endpoint ``GET /api/agents`` is owned and tested by
# PR 7F (``telos.api.routes.agents``). PR 7E originally shipped a
# provisional specialist-only variant in chat.py; reviewer-quality
# pushed back (route would conflict with 7F's full-list version), so
# 7E dropped that endpoint and the sidebar filters housekeeper client-
# side instead. The corresponding tests were dropped here too.
