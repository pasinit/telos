"""Tests for the nightly feedback engine (telos.services.nightly).

The SDK boundary is faked: a stub analyst drives the REAL toolset handlers
(so tool validation + result capture are exercised) instead of spawning the
Claude CLI. The Recordatio client is a stub. Together they cover discovery,
the tools, the feedback store, and the full map→reduce→digest orchestration
including per-agent failure isolation.
"""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from telos.log import ConversationLog
from telos.services.nightly.analyst import SessionOutcome
from telos.services.nightly.discovery import active_agents
from telos.services.nightly.service import NightlyConfig, NightlyFeedbackService
from telos.services.nightly.store import FeedbackStore
from telos.services.nightly.tools import ToolContext, Toolset, build_agent_toolset, build_memory_toolset

DAY = date(2026, 7, 31)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _StubClient:
    """Stand-in for RecordatioFeedbackClient. Records feedback posts; returns
    canned reads."""

    def __init__(
        self,
        *,
        search_results: list | None = None,
        feedback: dict | None = None,
        reconcile: dict | Exception | None = None,
    ) -> None:
        self.posted: list[dict] = []
        self.events: list[str] = []
        self.closed = False
        self._search = search_results or []
        self._feedback = feedback or {}
        self._reconcile = reconcile if reconcile is not None else {"totals": {}}

    async def aclose(self) -> None:
        self.closed = True

    async def get_memory_feedback(self, user_id):
        return {"user_id": user_id, "records": self._feedback.get(user_id, [])}

    async def list_units(self, user_id, *, day=None, limit=200):
        return {"user_id": user_id, "day": day, "count": 0, "units": []}

    async def memory_search(self, user_id, query, *, mode="production", slug=None, as_of=None, k=10):
        return {"user_id": user_id, "query": query, "mode": mode, "count": len(self._search), "results": self._search}

    async def get_briefing(self, request_id):
        return {"request_id": request_id, "units": []}

    async def post_memory_feedback(self, user_id, *, day, summary, findings, ops):
        self.events.append("post")
        self.posted.append({"user_id": user_id, "day": day, "summary": summary, "findings": findings, "ops": ops})
        return {"id": len(self.posted)}

    async def reconcile_memory(self, *, dry_run=False, timeout=1800.0):
        self.events.append("reconcile")
        if isinstance(self._reconcile, Exception):
            raise self._reconcile
        return self._reconcile


class _ScriptedAnalyst:
    """Drives the real toolset handlers to fill results, simulating the SDK.

    ``agent_payload`` / ``memory_payload`` are the submit-tool args; ``fail``
    slugs finish without submitting (to test failure recording)."""

    def __init__(self, *, agent_payload=None, memory_payload=None, fail: set[str] | None = None, cost=0.01):
        self._agent = agent_payload
        self._memory = memory_payload
        self._fail = fail or set()
        self._cost = cost
        self.calls: list[str] = []

    async def run_session(self, *, system_prompt, prompt, toolset: Toolset) -> SessionOutcome:
        is_memory = "nightly_memory" in toolset.mcp_servers
        self.calls.append("memory" if is_memory else "agent")
        if is_memory:
            if self._memory is not None:
                await toolset.handlers["submit_memory_feedback"](self._memory)
        else:
            # Skip submission for agents flagged to fail.
            if any(f in prompt for f in self._fail):
                return SessionOutcome(cost_usd=self._cost, is_error=True, error="boom", num_turns=1)
            if self._agent is not None:
                # Exercise a read tool too, then submit.
                await toolset.handlers["get_turns"]({"page": 0})
                await toolset.handlers["submit_agent_feedback"](self._agent)
        return SessionOutcome(cost_usd=self._cost, is_error=False, error=None, num_turns=3)


def _make_agent(storage_root: Path, user: str, slug: str, *, n_turns: int, at: datetime) -> None:
    log = ConversationLog(storage_root / user / slug / "history.db")
    for i in range(n_turns):
        tid = log.turns.append(f"user msg {i}", f"assistant reply {i}", "sha", trace_id=None)
        # Force the timestamp into the target day.
        with log._db.connect() as conn:  # noqa: SLF001 (test reaches into repo)
            conn.execute("UPDATE turns SET timestamp=? WHERE turn_id=?", (at.isoformat(), tid))


_AGENT_PAYLOAD = {
    "score": 4,
    "summary": "solid day, one weak turn",
    "failures": [{"turn_id": "t1", "issue": "vague answer", "evidence": "..."}],
    "prompt_suggestions": ["be more specific about sources"],
    "tool_use_issues": [],
    "recall_misses": [
        {"turn_id": "t1", "query_used": "London roles", "classification": "ranking_gap", "detail": "in raw not prod"}
    ],
}
_MEMORY_PAYLOAD = {
    "summary": "two duplicate units",
    "findings": [{"kind": "duplicate", "detail": "same fact twice", "unit_ids": ["a", "b"]}],
    "ops": [{"op_type": "merge", "target": {"canonical_id": "a", "duplicate_ids": ["b"]}, "reason": "identical"}],
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_active_agents_only_lists_agents_with_turns_that_day(tmp_path: Path) -> None:
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=3, at=at)
    _make_agent(tmp_path, "dana", "homey", n_turns=2, at=datetime(2026, 7, 20, 9, 0))  # different day
    _make_agent(tmp_path, "tommaso", "assistant", n_turns=1, at=at)
    agents = active_agents(tmp_path, DAY)
    got = {(a.user_id, a.slug): a.turn_count for a in agents}
    assert got == {("dana", "jobber"): 3, ("tommaso", "assistant"): 1}


# ---------------------------------------------------------------------------
# Tools (real handlers)
# ---------------------------------------------------------------------------


async def test_agent_tools_paginate_and_validate(tmp_path: Path) -> None:
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=3, at=at)
    turns = ConversationLog(tmp_path / "dana" / "jobber" / "history.db").turns.turns_between(
        *__import__("telos.services.nightly.discovery", fromlist=["day_bounds"]).day_bounds(DAY)
    )
    ctx = ToolContext(user_id="dana", slug="jobber", day="2026-07-31", client=_StubClient(), turns=turns)
    ts = build_agent_toolset(ctx)

    page = json.loads((await ts.handlers["get_turns"]({"page": 0}))["content"][0]["text"])
    assert page["total"] == 3

    # A bad submission is rejected (is_error) and does not fill the result.
    bad = await ts.handlers["submit_agent_feedback"]({"score": 9})
    assert bad.get("is_error")
    assert ts.result.feedback is None

    ok = await ts.handlers["submit_agent_feedback"](_AGENT_PAYLOAD)
    assert not ok.get("is_error")
    assert ts.result.feedback.score == 4
    assert ts.result.feedback.recall_misses[0].classification == "ranking_gap"


async def test_memory_tools_submit(tmp_path: Path) -> None:
    ctx = ToolContext(user_id="dana", slug=None, day="2026-07-31", client=_StubClient())
    ts = build_memory_toolset(ctx, [{"slug": "jobber", "recall_misses": []}])
    obs = json.loads((await ts.handlers["get_agent_observations"]({}))["content"][0]["text"])
    assert obs["observations"][0]["slug"] == "jobber"
    await ts.handlers["submit_memory_feedback"](_MEMORY_PAYLOAD)
    assert ts.result.feedback.ops[0].op_type == "merge"


# ---------------------------------------------------------------------------
# FeedbackStore
# ---------------------------------------------------------------------------


def test_feedback_store_run_idempotent_per_day(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.db")
    r1 = store.start_run("2026-07-31")
    store.record_agent_feedback(r1, user_id="dana", slug="jobber", status="completed", feedback={"score": 3})
    r2 = store.start_run("2026-07-31")  # re-run same day supersedes
    assert store.get_run(r1) is None
    assert store.list_agent_feedback(r1) == []
    store.finish_run(r2, status="completed", cost_usd=0.5)
    runs = store.list_runs()
    assert len(runs) == 1 and runs[0].cost_usd == 0.5


# ---------------------------------------------------------------------------
# Service orchestration
# ---------------------------------------------------------------------------


def _service(tmp_path: Path, analyst, client, emails: list) -> NightlyFeedbackService:
    cfg = NightlyConfig(
        storage_root=tmp_path,
        owner_email="owner@example.com",
        dashboard_url="https://telos.example/nightly",
        model="claude-sonnet-4-6",
    )
    return NightlyFeedbackService(
        cfg,
        store=FeedbackStore(tmp_path / "feedback.db"),
        analyst=analyst,
        make_client=lambda: client,
        email_sink=lambda to, subj, body: emails.append((to, subj, body)),
    )


async def test_service_full_run_maps_reduces_and_digests(tmp_path: Path) -> None:
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=2, at=at)
    _make_agent(tmp_path, "dana", "homey", n_turns=1, at=at)
    client = _StubClient()
    analyst = _ScriptedAnalyst(agent_payload=_AGENT_PAYLOAD, memory_payload=_MEMORY_PAYLOAD, cost=0.02)
    emails: list = []
    svc = _service(tmp_path, analyst, client, emails)

    run = await svc.run_for_day(DAY)

    assert run.status == "completed"
    # 2 agent map sessions + 1 memory reduce (single user).
    assert analyst.calls.count("agent") == 2 and analyst.calls.count("memory") == 1
    # Per-agent feedback persisted.
    rows = svc._store.list_agent_feedback(run.id)  # noqa: SLF001
    assert {r.slug for r in rows} == {"jobber", "homey"}
    assert all(r.status == "completed" and r.feedback["score"] == 4 for r in rows)
    # Memory feedback POSTed to Recordatio once, with the merge op.
    assert len(client.posted) == 1
    assert client.posted[0]["ops"][0]["op_type"] == "merge"
    # Cost aggregated (2 agents + 1 memory) * 0.02.
    assert run.cost_usd == pytest.approx(0.06)
    # Digest emailed to owner with a dashboard link.
    assert len(emails) == 1
    to, subj, body = emails[0]
    assert to == "owner@example.com" and "2026-07-31" in subj
    assert "https://telos.example/nightly" in body and "score 4/5" in body
    assert "queue clear" in body  # no pending backlog → reassuring clear line
    assert client.closed


async def test_digest_surfaces_pending_memory_backlog(tmp_path: Path) -> None:
    """Regression: the analyst's reversible ops pile up pending behind a
    dashboard nobody visits. The digest must push the backlog (count + oldest
    age + per-type breakdown) so it becomes a visible, actionable line."""
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=1, at=at)
    records = [
        {"day": "2026-07-29", "created_at": "2026-07-29T02:00:00+00:00", "summary": "s", "findings": [],
         "ops": [
             {"op_type": "tombstone", "status": "pending"},
             {"op_type": "merge", "status": "applied"},  # decided → must be excluded
         ]},
        {"day": "2026-07-15", "created_at": "2026-07-15T02:00:00+00:00", "summary": "s", "findings": [],
         "ops": [
             {"op_type": "tombstone", "status": "pending"},
             {"op_type": "correct", "status": "pending"},
         ]},
    ]
    client = _StubClient(feedback={"dana": records})
    analyst = _ScriptedAnalyst(agent_payload=_AGENT_PAYLOAD, memory_payload=_MEMORY_PAYLOAD)
    emails: list = []
    svc = _service(tmp_path, analyst, client, emails)

    await svc.run_for_day(DAY)

    _, _, body = emails[0]
    assert "Memory-hygiene queue" in body
    assert "<strong>3</strong> op(s) pending" in body   # 3 pending; the applied op is excluded
    assert "across 1 user(s)" in body
    assert "oldest 16 day(s)" in body                   # 2026-07-31 − 2026-07-15
    assert "2 tombstone" in body and "1 correct" in body


async def test_service_isolates_a_failing_agent(tmp_path: Path) -> None:
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=1, at=at)
    _make_agent(tmp_path, "dana", "brokenbot", n_turns=1, at=at)
    client = _StubClient()
    # brokenbot's task prompt contains its slug → analyst returns error, no submit.
    analyst = _ScriptedAnalyst(agent_payload=_AGENT_PAYLOAD, memory_payload=_MEMORY_PAYLOAD, fail={"brokenbot"})
    svc = _service(tmp_path, analyst, client, [])

    run = await svc.run_for_day(DAY)

    assert run.status == "completed"  # one bad agent doesn't sink the night
    rows = {r.slug: r for r in svc._store.list_agent_feedback(run.id)}  # noqa: SLF001
    assert rows["jobber"].status == "completed"
    assert rows["brokenbot"].status == "failed"
    assert rows["brokenbot"].error == "boom"


async def test_service_no_active_agents_is_clean(tmp_path: Path) -> None:
    client = _StubClient()
    analyst = _ScriptedAnalyst()
    emails: list = []
    svc = _service(tmp_path, analyst, client, emails)
    run = await svc.run_for_day(DAY)
    assert run.status == "completed"
    assert analyst.calls == []
    assert client.posted == []
    assert len(emails) == 1  # still sends a (empty) digest


async def test_service_reconciles_memory_after_reduce_and_reports_it(tmp_path: Path) -> None:
    """The nightly run triggers Recordatio's reconcile after the reduce POSTs
    (so tonight's ops are re-checked too) and summarises it in the digest."""
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=1, at=at)
    client = _StubClient(reconcile={"totals": {
        "deleted": 3, "by_relation": {"duplicate_subsumed": 2, "supersedes": 1},
        "ops_apply": 2, "ops_dismiss": 5, "ops_pending": 1, "judge_calls": 12, "judge_cost_usd": 0.04,
    }})
    analyst = _ScriptedAnalyst(agent_payload=_AGENT_PAYLOAD, memory_payload=_MEMORY_PAYLOAD)
    emails: list = []
    svc = _service(tmp_path, analyst, client, emails)

    await svc.run_for_day(DAY)

    assert client.events == ["post", "reconcile"]
    _, _, body = emails[0]
    assert "Memory reconcile" in body
    assert "Deleted <strong>3</strong> superseded memories (2 duplicate subsumed, 1 supersedes)" in body
    assert "2 applied, 5 dismissed, 1 left pending" in body


async def test_reconcile_failure_never_blocks_the_night(tmp_path: Path) -> None:
    at = datetime(2026, 7, 31, 9, 0)
    _make_agent(tmp_path, "dana", "jobber", n_turns=1, at=at)
    client = _StubClient(reconcile=RuntimeError("recordatio down"))
    analyst = _ScriptedAnalyst(agent_payload=_AGENT_PAYLOAD, memory_payload=_MEMORY_PAYLOAD)
    emails: list = []
    svc = _service(tmp_path, analyst, client, emails)

    run = await svc.run_for_day(DAY)

    assert run.status == "completed"
    assert len(emails) == 1 and "Memory reconcile" not in emails[0][2]
