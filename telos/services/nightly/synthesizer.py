"""Improvement synthesizer — turns nightly judgements into proposals.

Runs after the nightly analysis (same standalone SDK-session pattern as the
analyst). It reads the recent judgements, the memory feedback, the agents'
current prompts, and the source repos (read-only), then proposes:

- PROMPT improvements — staged as pending candidates through the same
  ApprovalManager seam the Improver uses, so they appear in each agent's
  Settings with the normal approve/reject flow. Nothing deploys itself.
- CODE / UX improvements — persisted as a report row and emailed to the
  owner, grounded with repo file references.

Phase 1 is proposals-first by design: no repo writes, no auto-PRs, no
auto-deployed prompts. A later phase may turn accepted code proposals into
draft PRs.
"""
import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from telos.improver import CandidatePrompt
from telos.services.nightly.analyst import NightlyAnalyst
from telos.services.nightly.recordatio_client import RecordatioFeedbackClient
from telos.services.nightly.store import FeedbackStore
from telos.services.nightly.synth_schemas import ImprovementReport
from telos.services.nightly.synth_tools import SynthContext, build_synth_toolset

log = logging.getLogger("telos.services.nightly")

SYNTH_SYSTEM = """You are an improvement engineer for a personal multi-agent assistant platform (telos: agents/UI; Recordatio: the memory engine). Each night an analyst reviews every agent's day and files structured judgements. Your job is to turn those judgements into a small number of HIGH-VALUE, concrete improvement proposals.

Work like this:
1. `get_judgements(days=7)` — read the recent judgements. Prioritize RECURRING patterns over one-off slips: the same failure across days, systematic recall-miss classifications, repeated prompt_suggestions, chronic tool_use_issues. Carve-out: a SEVERE single-day failure also warrants a proposal even when it is not recurring — a new or single-day agent has no history to recur, so a fresh but serious failure must not fall through the recurring-pattern filter. Treat as severe (worth proposing on one day alone): a low score (≤2), fabrication or hallucination, ignoring or contradicting retrieved memory, a stale memory value presented as current, or a user correction. This widens coverage to severe one-offs; it is NOT license to propose for minor slips.
   Routines executed by the Cardea kernel are judged against Cardea's per-call receipts (state and failure summary per call), which are authoritative. A judgement that calls a Cardea run's reported failures (blocked, HTTP error, timed out) fabricated or confabulated is only credible if it cites a receipt showing that very call succeeded; never propose a prompt change that suppresses honest failure reporting the receipts confirm.
2. For each agent you want to improve, `get_agent_prompt` — read its CURRENT prompt carefully. A prompt improvement must be a complete revised prompt that keeps everything that works and surgically fixes what the judgements show failing. Do not rewrite style wholesale.
3. `get_memory_feedback` per user for the memory module's own health signals.
4. For suspected platform defects (not agent-prompt issues): use `grep`/`read_file`/`list_dir` on the repos to locate the actual code and confirm the mechanism before proposing. A code improvement must cite real files. Then call `get_open_prs` and map each candidate code_improvement against the OPEN pull requests (drafts INCLUDED): if an open PR already addresses the defect (same fix/area, even if worded differently), do NOT propose it — it is already in flight and the coder would implement the same fix twice. Only propose code fixes no open PR already covers.
5. Return ONE report as your FINAL message, matching the ImprovementReport schema. You do NOT call a tool to submit it — the harness reads your final structured message directly, saves it, and emails it. Put every piece of content in its TYPED FIELD; do NOT serialize the report into `summary` and do NOT emit markup/XML tags:
   - `summary`: ONE or two sentences of plain-prose overview — nothing else. Never place proposals, lists, JSON, or `<tags>` in here.
   - `prompt_improvements` (array): only for agents with clear evidence — a recurring pattern, or a severe single-day failure per step 1 (max 3 per night). Each item: full revised prompt + rationale citing the judgements.
   - `code_improvements` (array): real, grounded defects/enhancements, each with repo + files + severity.
   - `ux_improvements` (array): product-level friction the judgements reveal.
Every proposal MUST live in its array field — a proposal mentioned only in `summary` is lost. Quality over quantity: an empty array is better than speculation, and a report whose arrays are all empty is a valid, expected outcome on a quiet night — but you must still return the report. Never propose something the judgements don't support."""


def synth_task_prompt(days: int, users: list[str]) -> str:
    return (
        f"Synthesize improvements from the last {days} day(s) of judgements for users {', '.join(users)}. "
        f"Read the evidence with your tools, ground any code claims in the repos, then submit one report."
    )


@dataclass(frozen=True)
class SynthOutcome:
    report: ImprovementReport | None
    staged_prompts: int
    cost_usd: float
    error: str | None


class SynthReportStore:
    """Persists synthesizer reports (sidecar table in its own DB)."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS synth_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                report_json TEXT NOT NULL,
                staged_prompts INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                error TEXT
            );
            """
        )
        self._conn.commit()

    def record(self, *, summary: str, report: dict[str, Any], staged_prompts: int,
               cost_usd: float, error: str | None = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO synth_reports (created_at, summary, report_json, staged_prompts, cost_usd, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), summary, json.dumps(report), staged_prompts, cost_usd, error),
            )
            return int(cur.lastrowid or 0)

    def list_reports(self, *, limit: int = 30) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM synth_reports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


class ImprovementSynthesizer:
    """Runs one synthesis session and delivers its proposals."""

    def __init__(
        self,
        *,
        analyst: NightlyAnalyst,
        feedback_store: FeedbackStore,
        report_store: SynthReportStore,
        make_client: Callable[[], RecordatioFeedbackClient],
        storage_root: Path,
        repo_roots: dict[str, Path],
        # AgentRuntime.ensure_improvement_state — builds+caches the agent on
        # demand so a prompt candidate can be staged even when the agent has
        # not served a live turn since startup; returns None for an unknown
        # slug. (Awaitable because building an agent is async.)
        get_agent_state: Callable[[str, str], Awaitable[Any]],
        email_sink: Callable[[str, str, str], None] | None = None,
        owner_email: str | None = None,
        base_url: str = "",
        repo_slugs: dict[str, str] | None = None,
        github_token: str = "",
        max_prompt_candidates: int = 3,
    ) -> None:
        self._analyst = analyst
        self._feedback_store = feedback_store
        self._report_store = report_store
        self._make_client = make_client
        self._storage_root = storage_root
        self._repo_roots = repo_roots
        self._get_agent_state = get_agent_state
        self._email_sink = email_sink
        self._owner_email = owner_email
        self._base_url = base_url
        self._repo_slugs = repo_slugs or {}
        self._github_token = github_token
        self._max_prompt_candidates = max_prompt_candidates

    async def run(self, *, days: int = 7, users: list[str] | None = None) -> SynthOutcome:
        """One synthesis session; stages prompt candidates + records a report."""
        users = users or self._discover_users()
        client = self._make_client()
        try:
            ctx = SynthContext(
                feedback_store=self._feedback_store,
                client=client,
                storage_root=self._storage_root,
                repo_roots=self._repo_roots,
                users=users,
                repo_slugs=self._repo_slugs,
                github_token=self._github_token,
            )
            ts = build_synth_toolset(ctx)
            outcome = await self._analyst.run_session(
                system_prompt=SYNTH_SYSTEM,
                prompt=synth_task_prompt(days, users),
                toolset=ts,
                output_schema=ImprovementReport.model_json_schema(),
            )
            report = self._parse_report(outcome.structured_output)
            if report is None:
                reason = outcome.error or "session returned no valid ImprovementReport"
                log.warning("synth: no report this run (%s)", reason)
                self._report_store.record(
                    summary="(no report)", report={}, staged_prompts=0,
                    cost_usd=outcome.cost_usd, error=reason,
                )
                return SynthOutcome(report=None, staged_prompts=0,
                                    cost_usd=outcome.cost_usd, error=reason)
            applied = await self._apply_prompts(report)
            self._report_store.record(
                summary=report.summary, report=report.model_dump(),
                staged_prompts=applied, cost_usd=outcome.cost_usd,
            )
            self._send_report_email(report, applied)
            return SynthOutcome(report=report, staged_prompts=applied,
                                cost_usd=outcome.cost_usd, error=None)
        finally:
            await client.aclose()

    # The model sometimes ignores the typed schema and serializes the whole
    # report as XML-ish text into ``summary`` (arrays left empty) — observed
    # 2026-08-13, where 3 real code proposals were trapped in ``summary`` and
    # silently dropped. These are the report's own field tags; their presence
    # in ``summary`` alongside empty arrays is the signature of that failure.
    _FIELD_TAGS = ("prompt_improvements", "code_improvements", "ux_improvements")
    _MALFORMED_MARKERS = ("</summary>", *(f"<{t}>" for t in _FIELD_TAGS))

    @staticmethod
    def _parse_report(structured_output: Any) -> ImprovementReport | None:
        """Validate the session's structured output into an ImprovementReport.

        The model returns the report as its final message (native
        ``output_schema``); the harness owns validation + delivery, so a
        malformed or absent payload is a no-op night, never a crash. If the
        model serialized its proposals into ``summary`` instead of the typed
        arrays (see the note above), recover them rather than lose the night.
        """
        if not structured_output:
            return None
        try:
            report = ImprovementReport.model_validate(structured_output)
        except ValidationError as exc:
            log.warning("synth: structured output failed ImprovementReport validation: %s", exc)
            return None
        if ImprovementSynthesizer._is_malformed(report):
            log.error(
                "synth: malformed structured output — proposals leaked into `summary` "
                "with empty typed arrays; attempting recovery",
            )
            return ImprovementSynthesizer._recover_from_summary(report)
        return report

    @staticmethod
    def _is_malformed(report: ImprovementReport) -> bool:
        """True when every typed array is empty yet ``summary`` carries the
        report's own field tags — the signature of a serialized-into-summary
        failure, distinct from a genuinely quiet (arrays-empty, clean-summary)
        night."""
        if report.prompt_improvements or report.code_improvements or report.ux_improvements:
            return False
        return any(marker in (report.summary or "") for marker in ImprovementSynthesizer._MALFORMED_MARKERS)

    @staticmethod
    def _recover_from_summary(report: ImprovementReport) -> ImprovementReport:
        """Best-effort: pull the typed arrays back out of a ``summary`` that
        captured the model's whole XML-ish report. Returns a repaired report
        (real summary + recovered arrays); on any failure returns the original
        so the night degrades to its (empty) state instead of crashing."""
        summary = report.summary or ""
        # The real summary is whatever precedes the first field/summary tag.
        head = re.split(r"</?summary>|<(?:prompt|code|ux)_improvements>", summary, maxsplit=1)[0].strip()
        rebuilt: dict[str, Any] = {"summary": head or "(recovered report)"}
        for field in ImprovementSynthesizer._FIELD_TAGS:
            m = re.search(rf"<{field}>(.*?)</{field}>", summary, re.DOTALL)
            if not m:
                continue
            try:
                rebuilt[field] = json.loads(m.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                log.warning("synth: could not recover %s from malformed summary", field)
        try:
            recovered = ImprovementReport.model_validate(rebuilt)
        except ValidationError as exc:
            log.warning("synth: recovery from malformed summary failed validation: %s", exc)
            return report
        log.error(
            "synth: recovered from malformed output — %d prompt / %d code / %d ux",
            len(recovered.prompt_improvements), len(recovered.code_improvements),
            len(recovered.ux_improvements),
        )
        return recovered

    def _discover_users(self) -> list[str]:
        """Users = storage-root subdirs owning at least one agent history.db."""
        if not self._storage_root.is_dir():
            return []
        users = []
        for user_dir in self._storage_root.iterdir():
            if user_dir.is_dir() and any(user_dir.glob("*/history.db")):
                users.append(user_dir.name)
        return sorted(users)

    async def _apply_prompts(self, report: ImprovementReport) -> int:
        """Auto-apply each prompt improvement directly to the live agent (capped).

        Chosen behaviour (2026-08-12): synthesizer prompt proposals deploy
        immediately rather than staging for manual approval. Each is still
        recorded in the agent's prompt history (so it stays rollbackable from
        Settings) and the change is surfaced in the report email; a regression
        is caught by the next night's analyst. ``approve_candidate`` runs the
        agent's full approve sequence — swap current prompt, persist, clear
        pending — the same path the improver uses above its auto-deploy
        threshold. We stage first because that path approves a pending
        candidate.
        """
        applied = 0
        for pi in report.prompt_improvements[: self._max_prompt_candidates]:
            try:
                state = await self._get_agent_state(pi.user_id, pi.slug)
                if state is None:
                    log.warning("synthesizer: no agent state for %s/%s — skipping prompt",
                                pi.user_id, pi.slug)
                    continue
                sha = hashlib.sha256(pi.new_prompt.encode()).hexdigest()[:12]
                state.approval.stage(CandidatePrompt(
                    sha=sha, content=pi.new_prompt,
                    rationale=f"[nightly-synthesizer] {pi.rationale}",
                    score_delta=0.0,
                ))
                state.approve_candidate()  # deploy immediately to the live agent
                applied += 1
                log.info("synthesizer: auto-applied prompt for %s/%s (sha %s)", pi.user_id, pi.slug, sha)
            except Exception:
                log.exception("synthesizer: auto-applying prompt for %s/%s failed",
                              pi.user_id, pi.slug)
        return applied

    def _settings_link(self, slug: str, label: str) -> str:
        base = self._base_url.rstrip("/") if self._base_url else ""
        href = f"{base}/agents/{slug}" if base else f"/agents/{slug}"
        return f'<a href="{href}">{label}</a>'

    def _send_report_email(self, report: ImprovementReport, applied: int) -> None:
        """Scannable digest: the summary + one-line titles + where to act.

        Deliberately terse — the full proposal bodies live where they are
        acted on (applied prompts in each agent's Settings, code proposals as
        draft PRs), so the email links there instead of reproducing them.
        """
        if self._email_sink is None or not self._owner_email:
            return
        try:
            lines = ["<h2>Nightly improvement proposals</h2>", f"<p>{report.summary}</p>"]
            if report.prompt_improvements:
                links = ", ".join(
                    self._settings_link(pi.slug, f"{pi.user_id}/{pi.slug}")
                    for pi in report.prompt_improvements
                )
                lines.append(
                    f"<p><strong>{applied} prompt(s) auto-applied</strong> — "
                    f"review or roll back in Settings: {links}</p>"
                )
            if report.code_improvements:
                lines.append(
                    f"<p><strong>{len(report.code_improvements)} code proposal(s)</strong> "
                    f"— opened as draft PRs (see the implementer email for links):</p><ul>"
                )
                lines.extend(f"<li>[{c.repo}] {c.title} <em>({c.severity})</em></li>" for c in report.code_improvements)
                lines.append("</ul>")
            if report.ux_improvements:
                lines.append(f"<p><strong>{len(report.ux_improvements)} UX proposal(s):</strong></p><ul>")
                lines.extend(f"<li>{u.title}</li>" for u in report.ux_improvements)
                lines.append("</ul>")
            self._email_sink(self._owner_email, "Nightly improvement proposals", "\n".join(lines))
        except Exception:
            log.exception("synthesizer: report email failed")


__all__ = ["ImprovementSynthesizer", "SynthReportStore", "SynthOutcome", "SYNTH_SYSTEM"]
