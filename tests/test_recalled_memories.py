"""Tests for the recalled-memories panel — issue #43.

Covers:
- ``ConversationLog`` request_id round-trip + idempotent migration.
- ``Agent.astream`` captures ``request_id`` from the briefing event +
  next chat-completion chunk and persists it on the turn.
- ``GET /api/agents/{slug}/turns/{turn_id}/memories`` for back-compat,
  hydrated payload, 404, and auth.
"""

import sqlite3
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from telos import Agent, Goal
from telos.domain import DelegationGraph, RunContext
from telos.log import ConversationLog


def _ctx(user_id: str = "u") -> RunContext:
    return RunContext(user_id=user_id, delegation_graph=DelegationGraph())

# ──────────────────────────────────────────────────────────────────────
# ConversationLog round-trip + migration
# ──────────────────────────────────────────────────────────────────────


def test_request_id_round_trip(tmp_path):
    log = ConversationLog(tmp_path / "history.db")
    turn_id = log.append("hello", "hi", "sha1")
    assert log.get_request_id(turn_id) is None

    log.update_request_id(turn_id, "recordatio-abc123")
    assert log.get_request_id(turn_id) == "recordatio-abc123"

    # Ensure recent_turns surfaces it too.
    turns = log.recent_turns()
    assert turns[0].request_id == "recordatio-abc123"


def test_migration_is_idempotent(tmp_path):
    """Constructing the log a second time over an existing DB must not raise."""
    db = tmp_path / "history.db"
    ConversationLog(db)
    ConversationLog(db)  # second call must not raise on existing column.

    # And the column must be present + nullable.
    with sqlite3.connect(db) as conn:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(turns)").fetchall()}
    assert "request_id" in cols
    # notnull flag (3rd field) is 0 → NULL allowed.
    assert cols["request_id"][3] == 0


def test_get_request_id_returns_none_for_unknown_turn(tmp_path):
    log = ConversationLog(tmp_path / "history.db")
    assert log.get_request_id("does-not-exist") is None


def test_turn_exists(tmp_path):
    log = ConversationLog(tmp_path / "history.db")
    turn_id = log.append("hi", "yo", "sha1")
    assert log.turn_exists(turn_id) is True
    assert log.turn_exists("nope") is False


# ──────────────────────────────────────────────────────────────────────
# Agent capture from briefing event → request_id persisted on turn.
# ──────────────────────────────────────────────────────────────────────


def _fake_task_events(frames: list[tuple[str, dict]]):
    """Stand-in for the recordatio agent wire (``stream_task_events``)."""
    async def _stream(*_a, **_kw):
        for frame in frames:
            yield frame
    return _stream


def _task_events_with_briefing(request_id: str, text: str = "ok"):
    """Fake Recordatio TaskEvent frames: a briefing (carrying its own
    ``request_id``), then the message + terminal completed event."""
    return _fake_task_events([
        ("briefing", {
            "request_id": request_id,
            "units": [{"type": "semantic", "content": "user prefers tea"}],
            "entities": [],
            "relations": [],
        }),
        ("message", {"text": text}),
        ("completed", {}),
    ])


def _task_events_no_briefing(text: str = "ok"):
    return _fake_task_events([
        ("message", {"text": text}),
        ("completed", {}),
    ])


@pytest.mark.asyncio
async def test_astream_captures_request_id_from_briefing(tmp_path):
    goal = Goal(name="t", description="d")
    agent = Agent(goal, user_id="u", storage_root=tmp_path)
    fake = _task_events_with_briefing("recordatio-deadbeef0001")
    with patch("telos.services.agent_runtime_v2.stream_task_events", new=fake):
        async for _ in agent.astream("hi", _ctx()):
            pass
    turns = agent._log.recent_turns()
    assert len(turns) == 1
    assert turns[0].request_id == "recordatio-deadbeef0001"


@pytest.mark.asyncio
async def test_astream_no_briefing_leaves_request_id_null(tmp_path):
    goal = Goal(name="t", description="d")
    agent = Agent(goal, user_id="u", storage_root=tmp_path)
    with patch(
        "telos.services.agent_runtime_v2.stream_task_events",
        new=_task_events_no_briefing(),
    ):
        async for _ in agent.astream("hi", _ctx()):
            pass
    turns = agent._log.recent_turns()
    assert turns[0].request_id is None


@pytest.mark.asyncio
async def test_astream_yields_turn_id_event(tmp_path):
    goal = Goal(name="t", description="d")
    agent = Agent(goal, user_id="u", storage_root=tmp_path)
    seen = []
    with patch(
        "telos.services.agent_runtime_v2.stream_task_events",
        new=_task_events_no_briefing(),
    ):
        async for kind, data in agent.astream("hi", _ctx()):
            if kind == "turn_id":
                seen.append(data)
    assert len(seen) == 1
    assert "turn_id" in seen[0]
    assert isinstance(seen[0]["turn_id"], str) and seen[0]["turn_id"]


# ──────────────────────────────────────────────────────────────────────
# /api/agents/{slug}/turns/{turn_id}/memories endpoint
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def server(tmp_path, monkeypatch):
    """TestClient with isolated storage + registry, mirroring test_usage."""
    from telos.api import app as srv
    from telos.api.registry import AgentRegistry

    monkeypatch.setattr(srv.settings, "storage_root", tmp_path)
    monkeypatch.setattr(srv.settings, "recordatio_url", "http://recordatio.test:8787")
    monkeypatch.setattr(srv.settings, "recordatio_public_url", "")
    fresh_registry = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(srv, "_registry", fresh_registry)
    monkeypatch.setattr(srv, "_agent_runtime", srv._build_agent_runtime())

    client = TestClient(srv.app)
    cookie = srv._signer.dumps("alice")
    return client, srv, cookie, tmp_path


def _seed_agent_with_turn(srv, tmp_path, *, user="alice", slug="weather",
                          request_id: str | None = None) -> str:
    """Create an agent registry entry + a turn row, return turn_id."""
    srv._registry.create(user, "Weather", "describe weather", "system prompt", None)
    # The fixture's slug auto-derives from name → "weather".
    log = ConversationLog(tmp_path / user / slug / "history.db")
    turn_id = log.append("what is the weather", "sunny", "sha-x")
    if request_id:
        log.update_request_id(turn_id, request_id)
    return turn_id


def test_memories_endpoint_requires_auth(server):
    client, srv, _, tmp_path = server
    turn_id = _seed_agent_with_turn(srv, tmp_path)
    r = client.get(
        f"/api/agents/weather/turns/{turn_id}/memories",
        follow_redirects=False,
    )
    # Unauthenticated /api/ -> 401.
    assert r.status_code == 401


def test_memories_endpoint_404_for_unknown_agent(server):
    client, _, cookie, _ = server
    client.cookies.set("user_id", cookie)
    r = client.get("/api/agents/no-such/turns/abc/memories")
    assert r.status_code == 404


def test_memories_endpoint_404_for_unknown_turn(server):
    client, srv, cookie, tmp_path = server
    _seed_agent_with_turn(srv, tmp_path)
    client.cookies.set("user_id", cookie)
    r = client.get("/api/agents/weather/turns/not-a-turn/memories")
    assert r.status_code == 404


def test_memories_endpoint_other_user_cannot_see_turn(server, monkeypatch):
    client, srv, _, tmp_path = server
    # Seed turn for alice.
    turn_id = _seed_agent_with_turn(srv, tmp_path, user="alice")
    # Authenticate as bob.
    bob_cookie = srv._signer.dumps("bob")
    client.cookies.set("user_id", bob_cookie)
    r = client.get(f"/api/agents/weather/turns/{turn_id}/memories")
    # Bob's registry doesn't have the agent → 404 (not 200).
    assert r.status_code == 404


def test_memories_endpoint_back_compat_no_request_id(server):
    client, srv, cookie, tmp_path = server
    turn_id = _seed_agent_with_turn(srv, tmp_path, request_id=None)
    client.cookies.set("user_id", cookie)
    r = client.get(f"/api/agents/weather/turns/{turn_id}/memories")
    assert r.status_code == 200
    body = r.json()
    assert body == {"recalled": None, "reason": "no_request_id"}


def test_memories_endpoint_returns_hydrated_payload(server, monkeypatch):
    client, srv, cookie, tmp_path = server
    turn_id = _seed_agent_with_turn(srv, tmp_path, request_id="recordatio-xyz")
    client.cookies.set("user_id", cookie)

    hydrated = {
        "id": 1,
        "ts": "2026-05-10T10:00:00+00:00",
        "request_id": "recordatio-xyz",
        "agent_id": "alice/weather",
        "query_text": "what is the weather",
        "topic_hint": "weather",
        "count_units": 1,
        "count_entities": 0,
        "count_relations": 0,
        "units": [{"id": "u1", "type": "semantic", "content": "user is in London"}],
        "entities": [],
        "relations": [],
    }

    async def _fake_get(self, url, *args, **kwargs):
        assert url.endswith("/v1/briefing-events/recordatio-xyz"), url
        return httpx.Response(200, json=hydrated)

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    r = client.get(f"/api/agents/weather/turns/{turn_id}/memories")
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"] == "recordatio-xyz"
    assert body["units"][0]["content"] == "user is in London"
    # Public URL absent -> empty string.
    assert body["recordatio_public_url"] == ""


def test_memories_endpoint_public_url_passed_through(server, monkeypatch):
    client, srv, cookie, tmp_path = server
    monkeypatch.setattr(srv.settings, "recordatio_public_url", "https://recordatio.example.com/")
    turn_id = _seed_agent_with_turn(srv, tmp_path, request_id="recordatio-xyz")
    client.cookies.set("user_id", cookie)

    async def _fake_get(self, url, *args, **kwargs):
        return httpx.Response(200, json={
            "request_id": "recordatio-xyz", "agent_id": "alice/weather",
            "units": [], "entities": [], "relations": [],
            "query_text": None, "topic_hint": None, "ts": "x",
            "count_units": 0, "count_entities": 0, "count_relations": 0, "id": 1,
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    r = client.get(f"/api/agents/weather/turns/{turn_id}/memories")
    assert r.status_code == 200
    # Trailing slash stripped.
    assert r.json()["recordatio_public_url"] == "https://recordatio.example.com"


def test_memories_endpoint_passes_through_404(server, monkeypatch):
    client, srv, cookie, tmp_path = server
    turn_id = _seed_agent_with_turn(srv, tmp_path, request_id="recordatio-gone")
    client.cookies.set("user_id", cookie)

    async def _fake_get(self, url, *args, **kwargs):
        return httpx.Response(404, json={"error": "not found"})

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    r = client.get(f"/api/agents/weather/turns/{turn_id}/memories")
    assert r.status_code == 404
    assert "recordatio-gone" in r.json()["detail"]


def test_memories_endpoint_502_on_recordatio_unreachable(server, monkeypatch):
    client, srv, cookie, tmp_path = server
    turn_id = _seed_agent_with_turn(srv, tmp_path, request_id="recordatio-x")
    client.cookies.set("user_id", cookie)

    async def _fake_get(self, url, *args, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    r = client.get(f"/api/agents/weather/turns/{turn_id}/memories")
    assert r.status_code == 502
