"""Tests for the improvement synthesizer (Phase 1, proposals-first).

Same seam as the nightly-analyst tests: a scripted analyst drives the REAL
toolset handlers (tool validation, repo read-only guards, result capture);
staging goes through a fake improvement-state exposing a real-shaped
ApprovalManager double.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from telos.services.nightly.analyst import SessionOutcome
from telos.services.nightly.store import FeedbackStore
from telos.services.nightly.synth_tools import SynthContext, build_synth_toolset, fetch_open_prs
from telos.services.nightly.synthesizer import ImprovementSynthesizer, SynthReportStore

REPORT = {
    "summary": "jobber keeps inventing submissions; feedback dashboard lacked bulk apply",
    "prompt_improvements": [{
        "user_id": "dana", "slug": "jobber",
        "new_prompt": "You are Jobber. Never assert an application was submitted unless it appears in memory. " * 3,
        "rationale": "usage_gap recurred on 3 days (07-29..07-31): recalled facts ignored in Section 7.",
    }],
    "code_improvements": [{
        "repo": "telos", "title": "Analyst emits truncated unit ids",
        "detail": "schemas.py should demand full uuids in memory op targets.",
        "files": ["telos/services/nightly/schemas.py"], "severity": "high",
    }],
    "ux_improvements": [{"title": "Bulk apply", "detail": "feedback dashboard needs accept-all."}],
}


class _StubClient:
    def __init__(self) -> None:
        class _H:
            async def get(self, url):  # /v1/users/{u}/memory-feedback
                import httpx
                return httpx.Response(200, json={"user_id": "u", "records": []},
                                      request=httpx.Request("GET", url))
        self._http = _H()

    async def aclose(self) -> None:
        pass


class _ScriptedAnalyst:
    """Drives the real (read-only) synth tools, then RETURNS the report as the
    session's structured output — the model no longer 'submits' via a tool.

    ``report`` is what the session returns as its final structured message:
    the REPORT dict on a normal night, ``None`` for a session that produced no
    valid output, or an invalid dict to exercise the harness's validation.
    """

    def __init__(self, *, report=REPORT, exercise_repo=True):
        self._report = report
        self._exercise_repo = exercise_repo
        self.tool_outputs: dict = {}

    async def run_session(self, *, system_prompt, prompt, toolset, output_schema=None) -> SessionOutcome:
        assert output_schema is not None, "synth must run with a native output_schema"
        assert "submit_improvements" not in toolset.handlers, "the write-tool must be gone"
        h = toolset.handlers
        self.tool_outputs["judgements"] = json.loads((await h["get_judgements"]({"days": 7}))["content"][0]["text"])
        if self._exercise_repo:
            self.tool_outputs["dir"] = await h["list_dir"]({"repo": "telos", "path": ""})
            self.tool_outputs["read"] = await h["read_file"]({"repo": "telos", "path": "marker.py"})
            self.tool_outputs["escape"] = await h["read_file"]({"repo": "telos", "path": "../../etc/passwd"})
            self.tool_outputs["grep"] = await h["grep"]({"repo": "telos", "pattern": "MARKER_", "glob": "*.py"})
        return SessionOutcome(cost_usd=1.5, is_error=False, error=None, num_turns=5,
                              structured_output=self._report)


class _FakeApproval:
    def __init__(self) -> None:
        self.staged = []

    def stage(self, candidate) -> None:
        self.staged.append(candidate)


class _FakeState:
    def __init__(self) -> None:
        self.approval = _FakeApproval()
        self.applied = 0

    def approve_candidate(self) -> None:
        # Mirrors Agent.approve(): deploy the currently-staged candidate.
        assert self.approval.staged, "approve_candidate called with nothing staged"
        self.applied += 1


@pytest.fixture()
def env(tmp_path: Path):
    fs = FeedbackStore(tmp_path / "feedback.db")
    run_id = fs.start_run("2026-07-31")
    fs.record_agent_feedback(run_id, user_id="dana", slug="jobber", status="completed",
                             feedback={"score": 2, "summary": "invented submissions",
                                       "failures": [], "prompt_suggestions": [],
                                       "tool_use_issues": [], "recall_misses": []})
    fs.finish_run(run_id, status="completed")
    # user tree + repo root with a marker file
    (tmp_path / "dana" / "jobber" / "prompts").mkdir(parents=True)
    (tmp_path / "dana" / "jobber" / "prompts" / "current.txt").write_text("old prompt")
    (tmp_path / "dana" / "jobber").joinpath("history.db").touch()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "marker.py").write_text("MARKER_CONSTANT = 1\n")
    states = {("dana", "jobber"): _FakeState()}
    reports = SynthReportStore(tmp_path / "synth.db")
    emails: list = []

    async def get_state(u, s):  # AgentRuntime.ensure_improvement_state is async
        return states.get((u, s))

    def make_synth(analyst):
        return ImprovementSynthesizer(
            analyst=analyst, feedback_store=fs, report_store=reports,
            make_client=lambda: _StubClient(), storage_root=tmp_path,
            repo_roots={"telos": repo},
            get_agent_state=get_state,
            email_sink=lambda to, subj, body: emails.append((to, subj, body)),
            owner_email="owner@example.com",
        )

    return make_synth, states, reports, emails


async def test_synthesizer_full_run(env) -> None:
    make_synth, states, reports, emails = env
    analyst = _ScriptedAnalyst()
    synth = make_synth(analyst)

    outcome = await synth.run()

    # Judgements were readable; repo tools worked and stayed read-only/rooted.
    assert analyst.tool_outputs["judgements"]["runs"][0]["agents"][0]["slug"] == "jobber"
    assert not analyst.tool_outputs["read"].get("is_error")
    assert analyst.tool_outputs["escape"].get("is_error")  # traversal refused
    grep_out = json.loads(analyst.tool_outputs["grep"]["content"][0]["text"])
    assert grep_out["match_count"] == 1

    # Prompt auto-applied through the approval seam with provenance.
    assert outcome.staged_prompts == 1
    st = states[("dana", "jobber")]
    cand = st.approval.staged[0]
    assert cand.rationale.startswith("[nightly-synthesizer]")
    assert cand.sha and len(cand.sha) == 12
    assert st.applied == 1  # deployed directly, not left pending

    # Report persisted + emailed with code/UX sections.
    rows = reports.list_reports()
    assert len(rows) == 1 and rows[0]["staged_prompts"] == 1
    assert outcome.cost_usd == 1.5
    to, subj, body = emails[0]
    assert "improvement proposals" in subj.lower()
    # Slim digest: one-line titles kept...
    assert "Analyst emits truncated unit ids" in body
    assert "Bulk apply" in body
    # ...full proposal detail bodies dropped...
    assert "demand full uuids" not in body   # code detail body
    assert "needs accept-all" not in body    # ux detail body
    # ...and the staged agent links to its Settings page.
    assert "/agents/jobber" in body


async def test_synthesizer_no_report_records_failure(env) -> None:
    """A session that returns no structured output is a no-op night: recorded
    as an error, nothing staged, no email — but never a crash."""
    make_synth, states, reports, emails = env
    synth = make_synth(_ScriptedAnalyst(report=None, exercise_repo=False))
    outcome = await synth.run()
    assert outcome.report is None and outcome.staged_prompts == 0
    assert reports.list_reports()[0]["error"] is not None
    assert emails == []  # no proposals email on a no-report night


async def test_synthesizer_invalid_structured_output_is_a_no_op(env) -> None:
    """The harness — not the model — validates the returned report. A malformed
    payload yields no report, an error row, and no email."""
    make_synth, states, reports, emails = env
    synth = make_synth(_ScriptedAnalyst(report={"summary": 1}, exercise_repo=False))  # invalid schema
    outcome = await synth.run()
    assert outcome.report is None and outcome.staged_prompts == 0
    assert reports.list_reports()[0]["error"] is not None
    assert emails == []


async def test_synthesizer_caps_and_skips_unknown_agents(env) -> None:
    make_synth, states, reports, emails = env
    pi = REPORT["prompt_improvements"][0]
    many = {**REPORT, "prompt_improvements": [
        pi,
        {**pi, "slug": "ghost"},   # unknown agent -> skipped
        {**pi, "slug": "jobber"},  # counts (same agent again)
        {**pi, "slug": "jobber"},  # beyond cap of 3 entries
    ]}
    synth = make_synth(_ScriptedAnalyst(report=many, exercise_repo=False))
    outcome = await synth.run()
    # cap=3 entries considered; 'ghost' skipped -> 2 staged
    assert outcome.staged_prompts == 2


# ──────────────────────────────────────────────────────────────────────
# _parse_report: recover proposals the model serialized into `summary`
# ──────────────────────────────────────────────────────────────────────

_CODE = {"repo": "telos", "title": "Transport retry", "detail": "d",
         "files": ["telos/services/agent_runtime_v2.py"], "severity": "high"}


def _malformed_output(code_items: list[dict]) -> dict:
    """A structured output like the 2026-08-13 failure: real prose then the
    report's own XML-ish tags, with the typed arrays left empty."""
    return {
        "summary": (
            "Seven days; the real overview sentence.</summary>\n"
            "<prompt_improvements>[]</prompt_improvements>\n"
            f"<code_improvements>{json.dumps(code_items)}</code_improvements>\n"
            "<ux_improvements>[]</ux_improvements>"
        ),
        "prompt_improvements": [],
        "code_improvements": [],
        "ux_improvements": [],
    }


def test_parse_report_recovers_proposals_leaked_into_summary() -> None:
    rep = ImprovementSynthesizer._parse_report(_malformed_output([_CODE]))
    assert rep is not None
    # The proposal trapped in `summary` is recovered into the typed array...
    assert len(rep.code_improvements) == 1
    assert rep.code_improvements[0].title == "Transport retry"
    assert rep.code_improvements[0].files == ["telos/services/agent_runtime_v2.py"]
    # ...and `summary` is cleaned back to the real prose (no tags).
    assert rep.summary == "Seven days; the real overview sentence."
    assert "</summary>" not in rep.summary and "<code_improvements>" not in rep.summary


def test_parse_report_passes_through_a_well_formed_report() -> None:
    good = {"summary": "A clean one-sentence overview.",
            "prompt_improvements": [], "code_improvements": [_CODE], "ux_improvements": []}
    rep = ImprovementSynthesizer._parse_report(good)
    assert rep.summary == "A clean one-sentence overview."
    assert len(rep.code_improvements) == 1


def test_parse_report_quiet_night_is_not_treated_as_malformed() -> None:
    # Empty arrays + a clean summary (no field tags) is a genuine quiet night.
    quiet = {"summary": "Nothing worth proposing tonight.",
             "prompt_improvements": [], "code_improvements": [], "ux_improvements": []}
    rep = ImprovementSynthesizer._parse_report(quiet)
    assert rep.summary == "Nothing worth proposing tonight."
    assert not rep.code_improvements and not rep.prompt_improvements and not rep.ux_improvements


def test_parse_report_unrecoverable_summary_degrades_without_crashing() -> None:
    # Field tags present (so flagged malformed) but the embedded JSON is junk.
    bad = {"summary": "text</summary><code_improvements>{not json}</code_improvements>",
           "prompt_improvements": [], "code_improvements": [], "ux_improvements": []}
    rep = ImprovementSynthesizer._parse_report(bad)
    assert rep is not None  # no crash
    assert rep.code_improvements == []


# ──────────────────────────────────────────────────────────────────────
# get_open_prs — so the synth won't re-propose a fix an open PR already covers
# ──────────────────────────────────────────────────────────────────────

def _gh_transport(prs_by_slug: dict[str, list]) -> httpx.MockTransport:
    """Fake GitHub API: returns canned open-PR JSON per repo slug."""
    def handler(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.split("/repos/", 1)[1].rsplit("/pulls", 1)[0]
        return httpx.Response(200, json=prs_by_slug.get(slug, []))
    return httpx.MockTransport(handler)


async def test_fetch_open_prs_parses_and_includes_drafts() -> None:
    prs = {
        "pasinit/telos": [
            {"number": 204, "title": "retry routines", "draft": True,
             "head": {"ref": "nightly/x"}, "body": "b" * 500},
        ],
        "pasinit/recordatio": [
            {"number": 210, "title": "runner retry", "draft": False,
             "head": {"ref": "nightly/y"}, "body": None},
        ],
    }
    out = await fetch_open_prs(
        {"telos": "pasinit/telos", "Recordatio": "pasinit/recordatio"}, "tok",
        transport=_gh_transport(prs),
    )
    by = {(p["repo"], p["number"]): p for p in out}
    assert by[("telos", 204)]["draft"] is True          # drafts included
    assert by[("telos", 204)]["branch"] == "nightly/x"
    assert len(by[("telos", 204)]["body_excerpt"]) == 400  # truncated
    assert by[("Recordatio", 210)]["draft"] is False
    assert by[("Recordatio", 210)]["body_excerpt"] == ""      # None body -> ""


async def test_fetch_open_prs_no_token_returns_empty() -> None:
    assert await fetch_open_prs({"telos": "pasinit/telos"}, "") == []


async def test_fetch_open_prs_captures_api_error_inline() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})
    out = await fetch_open_prs({"telos": "pasinit/telos"}, "tok", transport=httpx.MockTransport(handler))
    assert len(out) == 1 and "error" in out[0]  # captured, not raised


async def test_get_open_prs_tool_surfaces_open_prs(tmp_path) -> None:
    ctx = SynthContext(
        feedback_store=MagicMock(), client=MagicMock(), storage_root=tmp_path,
        repo_slugs={"telos": "pasinit/telos"}, github_token="tok",
        github_transport=_gh_transport({"pasinit/telos": [
            {"number": 204, "title": "retry", "draft": True, "head": {"ref": "nightly/x"}, "body": "b"},
        ]}),
    )
    ts = build_synth_toolset(ctx)
    assert "get_open_prs" in ts.handlers
    payload = json.loads((await ts.handlers["get_open_prs"]({}))["content"][0]["text"])
    assert payload["count"] == 1
    assert payload["open_prs"][0]["number"] == 204
    assert payload["open_prs"][0]["draft"] is True


def test_synth_prompt_instructs_open_pr_dedup() -> None:
    from telos.services.nightly.synthesizer import SYNTH_SYSTEM
    assert "get_open_prs" in SYNTH_SYSTEM
    assert "same fix twice" in SYNTH_SYSTEM
