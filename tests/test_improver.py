"""Tests for improvement-run persistence (issue #44)."""
import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.costs import CostLog
from telos.improver import CandidatePrompt, Improver
from telos.log import ConversationLog

# ──────────────────────────────────────────────────────────────────────
# ConversationLog.start_run / finish_run / list_runs / get_run
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_log(tmp_path):
    return ConversationLog(tmp_path / "history.db")


def test_start_run_creates_pending_row(tmp_log):
    rid = tmp_log.start_run(agent_slug="x", current_sha="abc", model="m")
    row = tmp_log.get_run(rid)
    assert row["decision"] == "pending"
    assert row["agent_slug"] == "x"
    assert row["current_sha"] == "abc"
    assert row["model"] == "m"
    assert row["finished_at"] is None


def test_finish_run_updates_decision(tmp_log):
    rid = tmp_log.start_run(agent_slug="x", current_sha="abc")
    tmp_log.finish_run(rid, decision="approved", proposed_sha="def", score_delta=0.2)
    row = tmp_log.get_run(rid)
    assert row["decision"] == "approved"
    assert row["proposed_sha"] == "def"
    assert row["score_delta"] == pytest.approx(0.2)
    assert row["finished_at"] is not None


def test_finish_run_rejects_invalid_decision(tmp_log):
    rid = tmp_log.start_run(agent_slug="x", current_sha="abc")
    with pytest.raises(ValueError):
        tmp_log.finish_run(rid, decision="invalid")


def test_list_runs_newest_first_and_filters(tmp_log):
    a = tmp_log.start_run(agent_slug="x", current_sha="s1")
    b = tmp_log.start_run(agent_slug="x", current_sha="s2")
    c = tmp_log.start_run(agent_slug="y", current_sha="s3")
    tmp_log.finish_run(a, decision="rejected")
    tmp_log.finish_run(b, decision="no_improvement")
    tmp_log.finish_run(c, decision="approved")

    rows = tmp_log.list_runs(agent_slug="x")
    assert [r["id"] for r in rows] == [b, a]  # newest first

    only_rej = tmp_log.list_runs(agent_slug="x", decision="rejected")
    assert [r["id"] for r in only_rej] == [a]

    other = tmp_log.list_runs(agent_slug="y")
    assert [r["id"] for r in other] == [c]


def test_list_runs_limit_offset(tmp_log):
    ids = [tmp_log.start_run(agent_slug="x", current_sha=f"s{i}") for i in range(5)]
    for rid in ids:
        tmp_log.finish_run(rid, decision="rejected")
    page1 = tmp_log.list_runs(agent_slug="x", limit=2, offset=0)
    page2 = tmp_log.list_runs(agent_slug="x", limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


# ──────────────────────────────────────────────────────────────────────
# Improver.run() — writes pending row at start, finishes with outcome
# ──────────────────────────────────────────────────────────────────────


def _make_improver(tmp_path, *, agent_slug="alpha"):
    log = ConversationLog(tmp_path / "history.db")
    costs = CostLog(tmp_path / "costs.db")
    client = MagicMock()
    return Improver(log, costs, client, model="claude-sonnet-4-6", agent_slug=agent_slug), log, costs


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_run_writes_no_improvement_when_research_returns_none(tmp_path):
    imp, log, _ = _make_improver(tmp_path)
    imp._research = AsyncMock(return_value=None)  # type: ignore[assignment]

    out = asyncio.run(imp.run("current prompt"))

    assert out is None
    runs = log.list_runs(agent_slug="alpha")
    assert len(runs) == 1
    r = runs[0]
    assert r["decision"] == "no_improvement"
    assert r["current_sha"] == _sha("current prompt")
    assert r["proposed_sha"] is None
    assert r["finished_at"] is not None


def test_run_writes_rejected_when_score_delta_non_positive(tmp_path):
    imp, log, _ = _make_improver(tmp_path)
    imp._research = AsyncMock(return_value={"prompt": "new", "rationale": "shorter"})  # type: ignore[assignment]
    imp._evaluate = AsyncMock(return_value=-0.2)  # type: ignore[assignment]

    out = asyncio.run(imp.run("current prompt"))

    assert out is None
    runs = log.list_runs(agent_slug="alpha")
    assert len(runs) == 1
    r = runs[0]
    assert r["decision"] == "rejected"
    assert r["proposed_sha"] == _sha("new")
    assert r["rationale"] == "shorter"
    assert r["score_delta"] == pytest.approx(-0.2)
    assert "current" in r["proposed_diff"]  # unified diff present


def test_run_writes_pending_with_diff_when_candidate_wins(tmp_path):
    imp, log, _ = _make_improver(tmp_path)
    imp._research = AsyncMock(return_value={"prompt": "new prompt", "rationale": "ok"})  # type: ignore[assignment]
    imp._evaluate = AsyncMock(return_value=0.3)  # type: ignore[assignment]

    out = asyncio.run(imp.run("current prompt"))

    assert isinstance(out, CandidatePrompt)
    assert out.run_id is not None
    runs = log.list_runs(agent_slug="alpha")
    assert len(runs) == 1
    r = runs[0]
    assert r["decision"] == "pending"
    assert r["proposed_sha"] == out.sha
    assert r["score_delta"] == pytest.approx(0.3)
    assert r["proposed_diff"] and "new prompt" in r["proposed_diff"]


def test_run_writes_error_on_exception(tmp_path):
    imp, log, _ = _make_improver(tmp_path)
    imp._research = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        asyncio.run(imp.run("current prompt"))

    runs = log.list_runs(agent_slug="alpha")
    assert len(runs) == 1
    r = runs[0]
    assert r["decision"] == "error"
    assert "boom" in (r["error"] or "")


# ──────────────────────────────────────────────────────────────────────
# Agent.approve / Agent.reject flip the run row
# ──────────────────────────────────────────────────────────────────────


def test_agent_approve_marks_run_approved(tmp_path):
    """When the user (or auto-deploy path) approves, the corresponding
    improvement_runs row flips from 'pending' to 'approved'."""
    from telos import Agent, Goal

    goal = Goal(name="t", description="x")
    agent = Agent(goal, user_id="u", storage_root=tmp_path)

    # Simulate: improver completed with a candidate. Use the real start_run
    # so the row is in the same per-agent history.db that the agent reads.
    rid = agent._log.start_run(
        agent_slug=agent._slug, current_sha=agent._current_sha, model="m"
    )
    agent._log.finish_run(
        rid,
        decision="pending",
        proposed_sha="newshax",
        rationale="rt",
        proposed_diff="diff",
        score_delta=0.2,
    )
    candidate = CandidatePrompt(
        sha="newshax", content="An improved prompt.", rationale="rt",
        score_delta=0.2, run_id=rid,
    )
    agent._approval.stage(candidate)

    agent.approve()

    row = agent._log.get_run(rid)
    assert row["decision"] == "approved"


def test_agent_reject_marks_run_rejected(tmp_path):
    from telos import Agent, Goal

    goal = Goal(name="t", description="x")
    agent = Agent(goal, user_id="u", storage_root=tmp_path)

    rid = agent._log.start_run(
        agent_slug=agent._slug, current_sha=agent._current_sha, model="m"
    )
    agent._log.finish_run(rid, decision="pending", proposed_sha="newshax",
                          rationale="rt", score_delta=0.2)
    candidate = CandidatePrompt(
        sha="newshax", content="x", rationale="rt", score_delta=0.2, run_id=rid
    )
    agent._approval.stage(candidate)

    agent.reject()

    row = agent._log.get_run(rid)
    assert row["decision"] == "rejected"


# ──────────────────────────────────────────────────────────────────────
# cost_events get run_id populated
# ──────────────────────────────────────────────────────────────────────


def test_cost_events_get_run_id_during_run(tmp_path):
    """When the Improver records costs mid-run, run_id must be set."""
    imp, log, costs = _make_improver(tmp_path)

    # Stub _research to call _record_response_cost like the real loop does,
    # then return a candidate so the run progresses to _evaluate.
    fake_response = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}

    async def fake_research(_current):
        imp._record_response_cost(fake_response, "improver_research", "claude-sonnet-4-6")
        return None

    imp._research = fake_research  # type: ignore[assignment]
    asyncio.run(imp.run("current prompt"))

    with costs._connect() as conn:
        rows = conn.execute(
            "SELECT source, run_id FROM cost_events"
        ).fetchall()
    assert any(r[0] == "improver_research" and r[1] is not None for r in rows)


def test_cost_log_record_cost_run_id_optional(tmp_path):
    """Legacy callers without a run_id still work; column stays NULL."""
    costs = CostLog(tmp_path / "costs.db")
    costs.record_cost(
        source="chat",
        model="claude-sonnet-4-6",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.0001,
    )
    with costs._connect() as conn:
        row = conn.execute("SELECT run_id FROM cost_events").fetchone()
    assert row[0] is None


# ──────────────────────────────────────────────────────────────────────
# /api/agents/{slug}/improvements endpoint
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Yield (TestClient, server module, signed_cookie_for("alice"))."""
    from fastapi.testclient import TestClient

    from telos.api import app as srv
    from telos.api.registry import AgentRegistry

    monkeypatch.setattr(srv.settings, "storage_root", tmp_path)
    fresh_registry = AgentRegistry(tmp_path / "registry.db")
    monkeypatch.setattr(srv, "_registry", fresh_registry)
    monkeypatch.setattr(srv, "_agent_runtime", srv._build_agent_runtime())

    client = TestClient(srv.app)
    cookie = srv._signer.dumps("alice")
    return client, srv, cookie


def test_improvements_api_requires_auth(server):
    client, _, _ = server
    resp = client.get("/api/agents/test-agent/improvements")
    # Cookie missing → 401 for /api/ paths
    assert resp.status_code == 401


def test_improvements_api_returns_runs(server, tmp_path):
    client, srv, cookie = server
    # Register an agent so the registry lookup succeeds.
    meta = srv._registry.create("alice", "Bot", "Helper", "system prompt")
    slug = meta["slug"]

    # Seed runs directly into the per-agent history.db.
    log = ConversationLog(tmp_path / "alice" / slug / "history.db")
    a = log.start_run(agent_slug=slug, current_sha="s1", model="m")
    log.finish_run(a, decision="rejected", score_delta=-0.1, rationale="r")
    b = log.start_run(agent_slug=slug, current_sha="s2", model="m")
    log.finish_run(b, decision="no_improvement")

    resp = client.get(f"/api/agents/{slug}/improvements", cookies={"user_id": cookie})
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == slug
    assert len(body["runs"]) == 2
    decisions = [r["decision"] for r in body["runs"]]
    assert decisions == ["no_improvement", "rejected"]  # newest first

    # Filter
    resp = client.get(
        f"/api/agents/{slug}/improvements?decision=rejected",
        cookies={"user_id": cookie},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["decision"] == "rejected"


def test_improvements_api_rejects_bad_decision(server):
    client, srv, cookie = server
    meta = srv._registry.create("alice", "Bot", "Helper", "system prompt")
    slug = meta["slug"]
    resp = client.get(
        f"/api/agents/{slug}/improvements?decision=bogus",
        cookies={"user_id": cookie},
    )
    assert resp.status_code == 400


def test_improvements_api_404_for_unknown_agent(server):
    client, _, cookie = server
    resp = client.get(
        "/api/agents/missing-slug/improvements",
        cookies={"user_id": cookie},
    )
    assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Housekeeper improvements — phase 5E. The housekeeper is now a regular
# registry entry: ``AgentRegistry.get(uid, HOUSEKEEPER_SLUG)`` auto-seeds
# the row on first access and returns it like any specialist. The
# improvements route uses plain ``get()`` with no special case.
# ──────────────────────────────────────────────────────────────────────


def test_improvements_api_returns_housekeeper_runs(server, tmp_path):
    """``GET /api/agents/_housekeeper/improvements`` returns 200 +
    persisted runs. The registry's auto-seed-on-get is what makes the
    plain ``_registry.get(uid, "_housekeeper")`` lookup succeed even
    on a fresh user."""
    from telos.housekeeper import HOUSEKEEPER_SLUG

    client, _, cookie = server
    log = ConversationLog(tmp_path / "alice" / HOUSEKEEPER_SLUG / "history.db")
    rid = log.start_run(agent_slug=HOUSEKEEPER_SLUG, current_sha="hk1", model="m")
    log.finish_run(rid, decision="no_improvement")

    resp = client.get(
        f"/api/agents/{HOUSEKEEPER_SLUG}/improvements",
        cookies={"user_id": cookie},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == HOUSEKEEPER_SLUG
    assert len(body["runs"]) == 1
    assert body["runs"][0]["decision"] == "no_improvement"


def test_registry_get_auto_seeds_housekeeper():
    """``AgentRegistry.get(uid, HOUSEKEEPER_SLUG)`` returns the seeded
    row on first access — populated from the constants in
    ``telos.housekeeper``."""
    from telos.housekeeper import HOUSEKEEPER_DESCRIPTION, HOUSEKEEPER_NAME, HOUSEKEEPER_SLUG, HOUSEKEEPER_SYSTEM_PROMPT
    from telos.api.registry import AgentRegistry

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        reg = AgentRegistry(Path(tmp) / "reg.db")
        # Fresh user — no row yet. ``get`` auto-seeds.
        meta = reg.get("anyone", HOUSEKEEPER_SLUG)
        assert meta is not None
        assert meta["slug"] == HOUSEKEEPER_SLUG
        assert meta["name"] == HOUSEKEEPER_NAME
        assert meta["description"] == HOUSEKEEPER_DESCRIPTION
        assert meta["system_prompt"] == HOUSEKEEPER_SYSTEM_PROMPT
        # Second call — same row, no duplicates.
        meta2 = reg.get("anyone", HOUSEKEEPER_SLUG)
        assert meta2 == meta


def test_registry_get_does_not_seed_for_specialist_slug():
    """Auto-seed only fires on the reserved slug — specialist reads stay
    pure. Unknown specialist slug returns ``None``, not a seed."""
    from telos.api.registry import AgentRegistry

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        reg = AgentRegistry(Path(tmp) / "reg.db")
        assert reg.get("alice", "missing") is None
        meta = reg.create("alice", "Bot", "Helper")
        assert reg.get("alice", meta["slug"]) == meta


def test_ensure_housekeeper_is_idempotent():
    """Calling ``ensure_housekeeper`` repeatedly is safe — the underlying
    ``INSERT OR IGNORE`` doesn't duplicate rows or mutate the existing
    one."""
    from telos.housekeeper import HOUSEKEEPER_SLUG
    from telos.api.registry import AgentRegistry

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        reg = AgentRegistry(Path(tmp) / "reg.db")
        first = reg.ensure_housekeeper("alice")
        second = reg.ensure_housekeeper("alice")
        assert first == second
        # Single row in the listing.
        rows = [r for r in reg.list_for_user("alice") if r["slug"] == HOUSEKEEPER_SLUG]
        assert len(rows) == 1


def test_list_specialists_excludes_housekeeper():
    """``list_specialists`` is the picker view — the housekeeper never
    appears there. ``list_for_user`` includes it (once seeded)."""
    from telos.housekeeper import HOUSEKEEPER_SLUG
    from telos.api.registry import AgentRegistry

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        reg = AgentRegistry(Path(tmp) / "reg.db")
        spec = reg.create("alice", "Weather", "describes weather")
        reg.ensure_housekeeper("alice")

        full = reg.list_for_user("alice")
        slugs_full = {r["slug"] for r in full}
        assert HOUSEKEEPER_SLUG in slugs_full
        assert spec["slug"] in slugs_full

        specs = reg.list_specialists("alice")
        slugs_specs = {r["slug"] for r in specs}
        assert HOUSEKEEPER_SLUG not in slugs_specs
        assert spec["slug"] in slugs_specs
