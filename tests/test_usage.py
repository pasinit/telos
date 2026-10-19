"""Tests for the /usage page + /api/usage rollup + recent-events endpoint."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from telos.costs import CostLog
from telos.housekeeper import HOUSEKEEPER_SLUG

# ──────────────────────────────────────────────────────────────────────
# Test rig: isolate the server's storage + registry to a tmp_path.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Yield (TestClient, server module, signed_cookie_for("alice")).

    Re-points the server module's storage_root + registry at a tmp dir
    so individual tests are hermetic.
    """
    from telos.api import app as srv
    from telos.api.registry import AgentRegistry

    # Repoint storage. The module-level _registry and settings.storage_root
    # are constructed at import time; we mutate them in place.
    monkeypatch.setattr(srv.settings, "storage_root", tmp_path)
    fresh_registry = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(srv, "_registry", fresh_registry)
    # Drop any cached agents from prior tests in the same process.
    monkeypatch.setattr(srv, "_agent_runtime", srv._build_agent_runtime())

    client = TestClient(srv.app)
    cookie = srv._signer.dumps("alice")
    return client, srv, cookie


def _seed_costs(storage_root: Path, user: str, slug: str, events: list[tuple]) -> None:
    """Append cost events to ``<storage>/<user>/<slug>/costs.db``.

    Each event is a tuple ``(source, model, prompt, completion, cost,
    ts_offset_seconds)``. ``ts_offset_seconds`` is interpreted relative
    to "now"; negative values land in the past.
    """
    db = CostLog(storage_root / user / slug / "costs.db")
    now = datetime.now(UTC)
    for source, model, p, c, cost, offset in events:
        ts = (now + timedelta(seconds=offset)).isoformat()
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO cost_events (ts, source, model, prompt_tokens, "
                "completion_tokens, cost_usd, turn_id) VALUES (?,?,?,?,?,?,?)",
                (ts, source, model, p, c, cost, None),
            )


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────


def test_usage_rollup_requires_auth(server):
    """Unauthenticated /api/usage returns 401 (PR 7I fix).

    Before Phase 7 the endpoint 303-redirected to /login via the legacy
    cookie-only ``_require_user`` helper. The SPA needs a structured
    rejection so the Usage page can surface a login prompt instead of
    following a redirect into HTML.
    """
    client, _, _ = server
    r = client.get("/api/usage", follow_redirects=False)
    assert r.status_code == 401
    body = r.json()
    assert body["detail"].startswith("Not authenticated")


def test_usage_rollup_rejects_invalid_session(server):
    """A tampered cookie is treated like a missing one — 401 (not 303).

    Mirrors the unauth test above and pins the symmetry: both branches
    of ``current_user``'s failure path return 401 on ``/api/*``.
    """
    client, _, _ = server
    r = client.get("/api/usage", cookies={"user_id": "garbage"}, follow_redirects=False)
    assert r.status_code == 401


# ──────────────────────────────────────────────────────────────────────
# Empty-state
# ──────────────────────────────────────────────────────────────────────


def test_rollup_empty_user(server):
    """A user with no agents still gets a well-formed rollup, with
    just the housekeeper present at $0."""
    client, _, cookie = server
    r = client.get("/api/usage", cookies={"user_id": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["total_usd"] == 0.0
    # Housekeeper is always included so the user sees the full roster.
    slugs = [e["slug"] for e in body["by_agent"]]
    assert slugs == [HOUSEKEEPER_SLUG]
    assert body["by_agent"][0]["total_usd"] == 0.0


# ──────────────────────────────────────────────────────────────────────
# Single agent with events
# ──────────────────────────────────────────────────────────────────────


def test_rollup_single_agent(server, tmp_path):
    client, srv, cookie = server
    # Create one agent
    meta = srv._registry.create("alice", "Finny", "Finance helper", "system prompt")
    slug = meta["slug"]
    _seed_costs(tmp_path, "alice", slug, [
        ("chat", "claude-sonnet-4-6", 1000, 500, 0.01, -10),
        ("summary", "claude-haiku-4-5", 100, 50, 0.0005, -5),
    ])

    r = client.get("/api/usage", cookies={"user_id": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["total_usd"] == pytest.approx(0.0105, abs=1e-6)
    # Sorted desc; the seeded agent should be first.
    assert body["by_agent"][0]["slug"] == slug
    assert body["by_agent"][0]["total_usd"] == pytest.approx(0.0105, abs=1e-6)
    assert "chat" in body["by_agent"][0]["by_source"]
    assert body["by_agent"][0]["by_source"]["chat"]["calls"] == 1
    # Housekeeper still appears with 0.
    hk = next(e for e in body["by_agent"] if e["slug"] == HOUSEKEEPER_SLUG)
    assert hk["total_usd"] == 0.0


# ──────────────────────────────────────────────────────────────────────
# Multiple agents — sorted by spend desc
# ──────────────────────────────────────────────────────────────────────


def test_rollup_multiple_agents_sorted_desc(server, tmp_path):
    client, srv, cookie = server
    a = srv._registry.create("alice", "Big Spender", "x", "p")
    b = srv._registry.create("alice", "Mid Spender", "y", "p")
    c = srv._registry.create("alice", "Tiny", "z", "p")
    _seed_costs(tmp_path, "alice", a["slug"], [("chat", "claude-sonnet-4-6", 0, 0, 1.00, -10)])
    _seed_costs(tmp_path, "alice", b["slug"], [("chat", "claude-sonnet-4-6", 0, 0, 0.50, -10)])
    _seed_costs(tmp_path, "alice", c["slug"], [("chat", "claude-sonnet-4-6", 0, 0, 0.05, -10)])

    r = client.get("/api/usage", cookies={"user_id": cookie})
    assert r.status_code == 200
    body = r.json()
    # First three (ignoring housekeeper at 0) should be desc by total.
    spending = [e for e in body["by_agent"] if e["total_usd"] > 0]
    totals = [e["total_usd"] for e in spending]
    assert totals == sorted(totals, reverse=True)
    assert spending[0]["slug"] == a["slug"]
    assert spending[1]["slug"] == b["slug"]
    assert spending[2]["slug"] == c["slug"]
    assert body["total_usd"] == pytest.approx(1.55, abs=1e-6)


# ──────────────────────────────────────────────────────────────────────
# `since` plumbing
# ──────────────────────────────────────────────────────────────────────


def test_rollup_since_short_token_filters_old_events(server, tmp_path):
    client, srv, cookie = server
    meta = srv._registry.create("alice", "Filtered", "x", "p")
    slug = meta["slug"]
    # One event 60 days ago, one fresh.
    _seed_costs(tmp_path, "alice", slug, [
        ("chat", "claude-sonnet-4-6", 0, 0, 0.99, -60 * 86400),
        ("chat", "claude-sonnet-4-6", 0, 0, 0.01, -10),
    ])

    # 24h window should hide the old one.
    r = client.get("/api/usage?since=24h", cookies={"user_id": cookie})
    assert r.status_code == 200
    entry = next(e for e in r.json()["by_agent"] if e["slug"] == slug)
    assert entry["total_usd"] == pytest.approx(0.01, abs=1e-6)

    # all-time picks both up.
    r = client.get("/api/usage?since=all", cookies={"user_id": cookie})
    assert r.status_code == 200
    entry = next(e for e in r.json()["by_agent"] if e["slug"] == slug)
    assert entry["total_usd"] == pytest.approx(1.00, abs=1e-6)


def test_rollup_since_iso_passthrough(server, tmp_path):
    """A raw ISO string in ?since= is forwarded straight to cost_summary."""
    client, srv, cookie = server
    meta = srv._registry.create("alice", "Iso", "x", "p")
    slug = meta["slug"]
    _seed_costs(tmp_path, "alice", slug, [
        ("chat", "claude-sonnet-4-6", 0, 0, 0.50, -3600),  # 1h ago
        ("chat", "claude-sonnet-4-6", 0, 0, 1.00, -10),    # ~now
    ])
    cutoff = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    r = client.get(f"/api/usage?since={cutoff}", cookies={"user_id": cookie})
    assert r.status_code == 200
    entry = next(e for e in r.json()["by_agent"] if e["slug"] == slug)
    assert entry["total_usd"] == pytest.approx(1.00, abs=1e-6)


# ──────────────────────────────────────────────────────────────────────
# /usage HTML page
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# Recent-events endpoint
# ──────────────────────────────────────────────────────────────────────


def test_recent_events_shape_and_limit(server, tmp_path, monkeypatch):
    client, srv, cookie = server
    meta = srv._registry.create("alice", "Recent", "x", "p")
    slug = meta["slug"]
    _seed_costs(tmp_path, "alice", slug, [
        ("chat", "claude-sonnet-4-6", 100, 50, 0.001, -i) for i in range(20)
    ])
    # Stub _get_agent because the real one constructs an Anthropic client
    # (and would touch network keys). The endpoint only needs cost_log.
    class _Stub:
        def __init__(self, db_path):
            self.cost_log = CostLog(db_path)
    async def _fake_get_agent(uid, s):
        return _Stub(tmp_path / uid / s / "costs.db")
    monkeypatch.setattr(srv, "_get_agent", _fake_get_agent)

    r = client.get(f"/api/agents/{slug}/costs/recent?limit=5", cookies={"user_id": cookie})
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == slug
    assert body["limit"] == 5
    assert len(body["events"]) == 5
    e = body["events"][0]
    assert set(e.keys()) >= {
        "ts", "source", "model", "prompt_tokens", "completion_tokens",
        "cost_usd", "turn_id",
    }
    assert e["source"] == "chat"
    assert e["cost_usd"] == pytest.approx(0.001, abs=1e-6)

    # Unauthenticated: 401 (under /api/).
    r = client.get(f"/api/agents/{slug}/costs/recent", follow_redirects=False)
    assert r.status_code == 401


def test_recent_events_clamps_limit(server, tmp_path, monkeypatch):
    """``limit`` is clamped to [1, 100] so callers can't drain the table."""
    client, srv, cookie = server
    meta = srv._registry.create("alice", "Clamp", "x", "p")
    slug = meta["slug"]
    _seed_costs(tmp_path, "alice", slug, [
        ("chat", "claude-sonnet-4-6", 0, 0, 0.001, -1)
    ])
    class _Stub:
        def __init__(self, db_path):
            self.cost_log = CostLog(db_path)
    async def _fake_get_agent(uid, s):
        return _Stub(tmp_path / uid / s / "costs.db")
    monkeypatch.setattr(srv, "_get_agent", _fake_get_agent)

    # 0 -> clamped up to 1.
    r = client.get(f"/api/agents/{slug}/costs/recent?limit=0", cookies={"user_id": cookie})
    assert r.status_code == 200
    assert r.json()["limit"] == 1
    # 9999 -> clamped down to 100.
    r = client.get(f"/api/agents/{slug}/costs/recent?limit=9999", cookies={"user_id": cookie})
    assert r.status_code == 200
    assert r.json()["limit"] == 100


# ──────────────────────────────────────────────────────────────────────
# CostLog.recent_events unit
# ──────────────────────────────────────────────────────────────────────


def test_costlog_recent_events_orders_newest_first(tmp_path):
    db = CostLog(tmp_path / "costs.db")
    db.record_cost("chat", "m", 0, 0, 0.01)
    db.record_cost("summary", "m", 0, 0, 0.02)
    db.record_cost("signal_classify", "m", 0, 0, 0.03)
    rows = db.recent_events(limit=2)
    assert len(rows) == 2
    assert rows[0]["source"] == "signal_classify"
    assert rows[1]["source"] == "summary"


def test_costlog_recent_events_limit_zero(tmp_path):
    db = CostLog(tmp_path / "costs.db")
    db.record_cost("chat", "m", 0, 0, 0.01)
    assert db.recent_events(limit=0) == []
