"""Tests for the nightly implementer (Phase 2 — draft PRs from proposals).

The SDK session is faked (same injectability pattern as the analyst tests);
the workspace-clone path uses a real throwaway git repo so the subprocess
plumbing is exercised for real.
"""
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from telos.services.nightly.implementer import (
    ImplementerCandidate,
    ImplementerReportStore,
    NightlyImplementer,
    implementer_task_prompt,
)
from telos.services.nightly.synth_schemas import CodeImprovement, ImprovementReport, UxImprovement


@dataclass
class _FakeSessionOutcome:
    cost_usd: float = 0.5
    error: str | None = None
    structured_output: Any | None = None


class _FakeSession:
    """Records the run kwargs and returns a canned outcome."""

    def __init__(self, structured_output: Any | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._structured_output = structured_output

    async def run(self, *, config, system_prompt, prompt):
        self.calls.append({"config": config, "system_prompt": system_prompt, "prompt": prompt})
        return _FakeSessionOutcome(structured_output=self._structured_output)


def _git_repo(path: Path) -> Path:
    """A minimal committed git repo to clone from."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )
    return path


def _report(*improvements: CodeImprovement) -> ImprovementReport:
    return ImprovementReport(summary="s", code_improvements=list(improvements))


def _proposal(repo: str = "telos", title: str = "fix it", severity: str = "high") -> CodeImprovement:
    return CodeImprovement(repo=repo, title=title, detail="d", files=["a.py"], severity=severity)


def _candidate(title: str = "fix it", severity: str = "high", repo: str = "telos", ux_origin: bool = False) -> ImplementerCandidate:
    return ImplementerCandidate(
        title=title, detail="d", severity=severity,
        repo=None if ux_origin else repo,
        files=[] if ux_origin else ["a.py"],
        ux_origin=ux_origin,
    )


def _implementer(tmp_path: Path, session: _FakeSession, **kw) -> NightlyImplementer:
    roots = {"telos": _git_repo(tmp_path / "roots" / "telos")}
    return NightlyImplementer(
        model="claude-test",
        repo_roots=kw.pop("repo_roots", roots),
        repo_slugs=kw.pop("repo_slugs", {"telos": "pasinit/telos"}),
        github_token=kw.pop("github_token", "tok"),
        report_store=kw.pop("report_store", ImplementerReportStore(tmp_path / "impl_reports.db")),
        session=session,  # type: ignore[arg-type]
        **kw,
    )


# ──────────────────────────────────────────────────────────────────────
# proposal selection
# ──────────────────────────────────────────────────────────────────────


def test_select_proposals_orders_by_severity_and_caps(tmp_path):
    session = _FakeSession()
    impl = _implementer(tmp_path, session, max_prs=2)
    report = _report(
        _proposal(title="low one", severity="low"),
        _proposal(title="high one", severity="high"),
        _proposal(title="normal one", severity="normal"),
    )
    selection = impl.select_proposals(report)
    assert [p.title for p in selection.selected] == ["high one", "normal one"]
    # The one past the cap is dropped with a reason, not silently lost.
    assert [d["title"] for d in selection.dropped] == ["low one"]
    assert "beyond max_prs cap of 2" in selection.dropped[0]["reason"]


def test_select_proposals_drops_unknown_repos(tmp_path):
    session = _FakeSession()
    impl = _implementer(tmp_path, session)
    report = _report(_proposal(repo="pontifex", title="elsewhere"), _proposal(title="here"))
    selection = impl.select_proposals(report)
    assert [p.title for p in selection.selected] == ["here"]
    (dropped,) = selection.dropped
    assert dropped["title"] == "elsewhere"
    assert "unknown repo" in dropped["reason"] and "pontifex" in dropped["reason"]


def test_select_proposals_folds_in_ux_candidates(tmp_path):
    session = _FakeSession()
    impl = _implementer(tmp_path, session, max_prs=10)
    report = ImprovementReport(
        summary="s",
        code_improvements=[_proposal(title="code fix", severity="normal")],
        ux_improvements=[UxImprovement(title="ux friction", detail="settings page is slow")],
    )
    selection = impl.select_proposals(report)
    titles = [c.title for c in selection.selected]
    assert "code fix" in titles and "ux friction" in titles
    ux = next(c for c in selection.selected if c.title == "ux friction")
    assert ux.ux_origin is True and ux.repo is None


# ──────────────────────────────────────────────────────────────────────
# run
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_without_proposals_never_spawns_a_session(tmp_path):
    store = ImplementerReportStore(tmp_path / "reports.db")
    session = _FakeSession()
    impl = _implementer(tmp_path, session, report_store=store)
    outcome = await impl.run(_report())
    assert outcome.prs == [] and outcome.error is None
    assert session.calls == []
    # Even the no-proposals path records an outcome row.
    (row,) = store.list_reports()
    assert row["summary"] == "no eligible code proposals"


@pytest.mark.asyncio
async def test_run_without_token_refuses(tmp_path):
    store = ImplementerReportStore(tmp_path / "reports.db")
    session = _FakeSession()
    impl = _implementer(tmp_path, session, github_token="", report_store=store)
    outcome = await impl.run(_report(_proposal()))
    assert outcome.error == "GITHUB_TOKEN not configured"
    assert session.calls == []
    (row,) = store.list_reports()
    assert row["error"] == "GITHUB_TOKEN not configured"


@pytest.mark.asyncio
async def test_run_clones_workspace_and_parses_structured_output(tmp_path):
    structured = {
        "summary": "one pr",
        "prs": [{"repo": "telos", "branch": "nightly/x", "url": "https://github.com/pasinit/telos/pull/1",
                 "title": "fix it", "verification": "pytest: 560 passed"}],
        "skipped": [{"title": "other", "reason": "could not confirm diagnosis"}],
    }
    emails: list[tuple[str, str, str]] = []
    session = _FakeSession(structured_output=structured)
    impl = _implementer(
        tmp_path, session,
        email_sink=lambda to, subject, body: emails.append((to, subject, body)),
        owner_email="owner@example.com",
    )
    outcome = await impl.run(_report(_proposal()))

    assert [pr["url"] for pr in outcome.prs] == ["https://github.com/pasinit/telos/pull/1"]
    assert outcome.skipped[0]["reason"] == "could not confirm diagnosis"

    (call,) = session.calls
    # The session got a real clone inside a temp workspace (cleaned up after).
    workdir = Path(call["config"].cwd)
    assert not workdir.exists(), "temp workspace must be removed after the run"
    assert "fix it" in call["prompt"]
    assert "pasinit/telos" in call["prompt"]
    assert call["config"].output_schema is not None
    assert call["config"].extra_env["GITHUB_TOKEN"] == "tok"
    assert "DRAFT" in call["system_prompt"]

    (email,) = emails
    assert email[0] == "owner@example.com"
    assert "1 draft PR(s) opened" in email[2]
    assert "pytest: 560 passed" in email[2]


@pytest.mark.asyncio
async def test_run_reports_clone_failure_as_outcome(tmp_path):
    session = _FakeSession()
    impl = _implementer(
        tmp_path, session,
        repo_roots={"telos": tmp_path / "does-not-exist"},
    )
    outcome = await impl.run(_report(_proposal()))
    assert outcome.error and "clone failed" in outcome.error
    assert session.calls == []


# ──────────────────────────────────────────────────────────────────────
# prompt shape
# ──────────────────────────────────────────────────────────────────────


def test_task_prompt_carries_proposals_and_branch_prefix(tmp_path):
    prompt = implementer_task_prompt(
        [_candidate(title="tokenize raw search")],
        {"telos": tmp_path / "w" / "telos"},
        {"telos": "pasinit/telos", "Recordatio": "pasinit/recordatio"},
        "20260806",
    )
    assert "tokenize raw search" in prompt
    assert "nightly/20260806-" in prompt
    # Only slugs for repos actually in the workspace are advertised.
    payload = prompt.split("GitHub slugs for push/PR: ")[1].split("\n")[0]
    assert json.loads(payload) == {"telos": "pasinit/telos"}


def test_task_prompt_labels_ux_candidates(tmp_path):
    prompt = implementer_task_prompt(
        [_candidate(title="code one"), _candidate(title="settings feels slow", ux_origin=True)],
        {"telos": tmp_path / "w" / "telos"},
        {"telos": "pasinit/telos"},
        "20260809",
    )
    assert "code one" in prompt
    assert "UX-origin candidates" in prompt
    assert "settings feels slow" in prompt


# ──────────────────────────────────────────────────────────────────────
# persistence + cap + UX flow through run()
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_records_prs_skipped_and_cap_dropped(tmp_path):
    structured = {
        "summary": "one pr",
        "prs": [{"repo": "telos", "branch": "nightly/x", "url": "u", "title": "keep me", "verification": "pytest"}],
        "skipped": [{"title": "skip me", "reason": "ambiguous"}],
    }
    store = ImplementerReportStore(tmp_path / "reports.db")
    session = _FakeSession(structured_output=structured)
    impl = _implementer(tmp_path, session, report_store=store, max_prs=1)
    report = _report(
        _proposal(title="keep me", severity="high"),
        _proposal(title="overflow", severity="low"),
    )
    await impl.run(report)

    (row,) = store.list_reports()
    assert json.loads(row["prs_json"])[0]["title"] == "keep me"
    assert json.loads(row["skipped_json"])[0]["title"] == "skip me"
    assert json.loads(row["cap_dropped_json"])[0]["title"] == "overflow"
    assert row["summary"] == "one pr"


@pytest.mark.asyncio
async def test_run_records_cap_overflow_instead_of_losing_it(tmp_path):
    structured = {"summary": "one pr", "prs": [], "skipped": []}
    store = ImplementerReportStore(tmp_path / "reports.db")
    session = _FakeSession(structured_output=structured)
    impl = _implementer(tmp_path, session, report_store=store, max_prs=1)
    report = _report(
        _proposal(title="keep me", severity="high"),
        _proposal(title="drop me", severity="low"),
        _proposal(title="drop me too", severity="low"),
    )
    outcome = await impl.run(report)

    dropped = {d["title"] for d in outcome.cap_dropped}
    assert dropped == {"drop me", "drop me too"}
    assert all("beyond max_prs cap of 1" in d["reason"] for d in outcome.cap_dropped)
    (row,) = store.list_reports()
    assert {d["title"] for d in json.loads(row["cap_dropped_json"])} == {"drop me", "drop me too"}


@pytest.mark.asyncio
async def test_run_passes_ux_candidates_into_task_prompt(tmp_path):
    structured = {"summary": "", "prs": [], "skipped": []}
    session = _FakeSession(structured_output=structured)
    impl = _implementer(tmp_path, session, max_prs=10)
    report = ImprovementReport(
        summary="s",
        code_improvements=[_proposal(title="code fix")],
        ux_improvements=[UxImprovement(title="tidy the sidebar", detail="cramped")],
    )
    await impl.run(report)

    (call,) = session.calls
    assert "UX-origin candidates" in call["prompt"]
    assert "tidy the sidebar" in call["prompt"]


# ──────────────────────────────────────────────────────────────────────
# store
# ──────────────────────────────────────────────────────────────────────


def test_implementer_report_store_roundtrip(tmp_path):
    store = ImplementerReportStore(tmp_path / "reports.db")
    store.record(summary="s1", prs=[{"url": "u"}], skipped=[],
                 cap_dropped=[{"title": "d", "reason": "r"}], cost_usd=1.5, error=None)
    store.record(summary="s2", prs=[], skipped=[{"title": "x", "reason": "y"}],
                 cap_dropped=[], cost_usd=0.0, error="boom")

    rows = store.list_reports()
    assert len(rows) == 2
    # Newest first.
    assert rows[0]["summary"] == "s2" and rows[0]["error"] == "boom"
    assert json.loads(rows[1]["prs_json"])[0]["url"] == "u"
    assert json.loads(rows[1]["cap_dropped_json"])[0]["title"] == "d"
    assert rows[1]["cost_usd"] == 1.5
