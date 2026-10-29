"""Nightly feedback orchestration: map over agents, reduce over memory.

For one day:
  1. Discover agents that had turns (``discovery.active_agents``).
  2. MAP — one analyst session per active agent → per-agent feedback,
     persisted report-only; recall-misses collected per user.
  3. REDUCE — one analyst session per user over the day's units + that
     user's collected observations → memory feedback, POSTed to Recordatio.
  4. RECONCILE — ask Recordatio to reconcile memory (best-effort): it deletes
     superseded memories and re-checks every pending memory-feedback op,
     applying or dismissing it, so the analyst's queue drains each night.
  5. Record the run + cost, then email the owner a summary with a
     dashboard link.

Every session is independently guarded: one wedged or erroring agent is
recorded as a failed agent-row and the night continues. Collaborators are
injected so the whole flow is unit-testable with a fake analyst + a stub
Recordatio client.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from telos.log import ConversationLog
from telos.services.nightly.analyst import NightlyAnalyst
from telos.services.nightly.discovery import active_agents, day_bounds
from telos.services.nightly.prompts import (
    AGENT_SYSTEM,
    MEMORY_SYSTEM,
    agent_task_prompt,
    memory_task_prompt,
)
from telos.services.nightly.recordatio_client import RecordatioFeedbackClient
from telos.services.nightly.store import FeedbackStore, NightlyRunRow
from telos.services.nightly.tools import (
    ToolContext,
    build_agent_toolset,
    build_memory_toolset,
)

log = logging.getLogger("telos.services.nightly")

# (to, subject, html_body) -> None
EmailSink = Callable[[str, str, str], None]


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars with an ellipsis — keeps the digest
    scannable when the analyst's prose runs long (the full text is in-app)."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


@dataclass
class NightlyConfig:
    storage_root: Path
    owner_email: str | None
    dashboard_url: str
    model: str


@dataclass(frozen=True)
class MemoryBacklog:
    """Snapshot of the pending memory-hygiene op queue across the day's users.

    The analyst proposes ops (tombstone/merge/correct) every night; Recordatio's
    nightly reconcile re-checks and applies or dismisses them. What is left
    pending after it ran are ops it could not decide yet (judge unavailable or
    per-run budget spent) — this line makes that remainder visible.
    """

    pending: int
    users_with_pending: int
    oldest_age_days: int | None
    by_type: dict[str, int]


@dataclass(frozen=True)
class MemoryReconcile:
    """Summary of Recordatio's nightly memory reconcile (all users)."""

    deleted: int
    by_relation: dict[str, int]
    ops_applied: int
    ops_dismissed: int
    ops_pending: int
    judge_calls: int
    judge_errors: int
    cost_usd: float


class NightlyFeedbackService:
    def __init__(
        self,
        config: NightlyConfig,
        *,
        store: FeedbackStore,
        analyst: NightlyAnalyst,
        make_client: Callable[[], RecordatioFeedbackClient],
        email_sink: EmailSink | None = None,
    ) -> None:
        self._cfg = config
        self._store = store
        self._analyst = analyst
        self._make_client = make_client
        self._email_sink = email_sink

    async def run_for_day(self, day: date) -> NightlyRunRow:
        """Run the full nightly loop for ``day``; returns the run row.

        Never raises for a per-agent/per-user failure — those are recorded.
        A top-level failure marks the run ``failed`` and re-raises after
        finishing the row so the scheduler sees it.
        """
        day_str = day.isoformat()
        run_id = self._store.start_run(day_str)
        start, end = day_bounds(day)
        total_cost = 0.0
        memory_summaries: list[str] = []
        backlog: MemoryBacklog | None = None
        reconcile: MemoryReconcile | None = None
        client = self._make_client()
        try:
            agents = active_agents(self._cfg.storage_root, day)
            log.info("nightly %s: %d active agent(s)", day_str, len(agents))
            # user -> observations gathered from that user's agent sessions
            observations: dict[str, list[dict]] = {}
            for a in agents:
                cost = await self._run_agent(run_id, a.user_id, a.slug, day_str, start, end, client, observations)
                total_cost += cost
            for user_id in sorted({a.user_id for a in agents}):
                cost, summary = await self._run_memory(
                    user_id, day_str, sum(1 for a in agents if a.user_id == user_id),
                    observations.get(user_id, []), client,
                )
                total_cost += cost
                if summary:
                    memory_summaries.append(f"{user_id}: {summary}")
            memory_summary = "\n".join(memory_summaries) or None
            # After the reduce POSTs, so tonight's new ops are re-checked too;
            # before the backlog snapshot, so the digest shows what is left.
            reconcile = await self._reconcile_memory(client)
            self._store.finish_run(run_id, status="completed", memory_summary=memory_summary, cost_usd=total_cost)
            backlog = await self._memory_backlog(day, sorted({a.user_id for a in agents}), client)
        except Exception as exc:
            log.exception("nightly %s failed", day_str)
            self._store.finish_run(run_id, status="failed", error=f"{exc.__class__.__name__}: {exc}", cost_usd=total_cost)
            await client.aclose()
            self._send_digest(run_id, backlog, reconcile)
            raise
        await client.aclose()
        self._send_digest(run_id, backlog, reconcile)
        run = self._store.get_run(run_id)
        assert run is not None
        return run

    async def _run_agent(
        self,
        run_id: int,
        user_id: str,
        slug: str,
        day_str: str,
        start: str,
        end: str,
        client: RecordatioFeedbackClient,
        observations: dict[str, list[dict]],
    ) -> float:
        """Run one per-agent map session; record it; return its cost."""
        try:
            history = self._cfg.storage_root / user_id / slug / "history.db"
            history_log = ConversationLog(history)
            turns = history_log.turns.turns_between(start, end)
            ctx = ToolContext(user_id=user_id, slug=slug, day=day_str, client=client, turns=turns,
                              event_reader=history_log.execution_events.page)
            ts = build_agent_toolset(ctx)
            outcome = await self._analyst.run_session(
                system_prompt=AGENT_SYSTEM,
                prompt=agent_task_prompt(user_id, slug, day_str, len(turns)),
                toolset=ts,
            )
            result = ts.result
            if result.feedback is None:
                self._store.record_agent_feedback(
                    run_id, user_id=user_id, slug=slug, status="failed",
                    error=outcome.error or "analyst finished without submitting feedback",
                )
                return outcome.cost_usd
            fb = result.feedback
            self._store.record_agent_feedback(
                run_id, user_id=user_id, slug=slug, status="completed", feedback=fb.model_dump(),
            )
            # Collect memory-relevant observations for the reduce phase.
            if fb.recall_misses:
                observations.setdefault(user_id, []).append({
                    "slug": slug,
                    "recall_misses": [m.model_dump() for m in fb.recall_misses],
                })
            return outcome.cost_usd
        except Exception as exc:
            log.exception("nightly agent %s/%s failed", user_id, slug)
            self._store.record_agent_feedback(
                run_id, user_id=user_id, slug=slug, status="failed",
                error=f"{exc.__class__.__name__}: {exc}",
            )
            return 0.0

    async def _run_memory(
        self,
        user_id: str,
        day_str: str,
        agent_count: int,
        observations: list[dict],
        client: RecordatioFeedbackClient,
    ) -> tuple[float, str | None]:
        """Run one per-user reduce session; POST feedback to Recordatio."""
        try:
            ctx = ToolContext(user_id=user_id, slug=None, day=day_str, client=client)
            ts = build_memory_toolset(ctx, observations)
            outcome = await self._analyst.run_session(
                system_prompt=MEMORY_SYSTEM,
                prompt=memory_task_prompt(user_id, day_str, agent_count),
                toolset=ts,
            )
            result = ts.result
            if result.feedback is None:
                return outcome.cost_usd, None
            fb = result.feedback
            await client.post_memory_feedback(
                user_id,
                day=day_str,
                summary=fb.summary,
                findings=[f.model_dump() for f in fb.findings],
                ops=[o.model_dump() for o in fb.ops],
            )
            return outcome.cost_usd, fb.summary
        except Exception:
            log.exception("nightly memory reduce for %s failed", user_id)
            return 0.0, None

    async def _reconcile_memory(self, client: RecordatioFeedbackClient) -> MemoryReconcile | None:
        """Trigger Recordatio's memory reconcile; ``None`` if it failed or is
        unavailable, so a memory-side problem never blocks the digest."""
        try:
            data = await client.reconcile_memory()
            totals = data.get("totals", {})
            return MemoryReconcile(
                deleted=int(totals.get("deleted", 0)),
                by_relation={str(k): int(v) for k, v in (totals.get("by_relation") or {}).items()},
                ops_applied=int(totals.get("ops_apply", 0)),
                ops_dismissed=int(totals.get("ops_dismiss", 0)),
                ops_pending=int(totals.get("ops_pending", 0)),
                judge_calls=int(totals.get("judge_calls", 0)),
                judge_errors=int(totals.get("judge_errors", 0)),
                cost_usd=float(totals.get("judge_cost_usd", 0.0)),
            )
        except Exception:
            log.exception("nightly memory reconcile failed")
            return None

    async def _memory_backlog(
        self, day: date, user_ids: list[str], client: RecordatioFeedbackClient
    ) -> MemoryBacklog | None:
        """Best-effort snapshot of the pending memory-hygiene op backlog for the
        day's users. Returns ``None`` if Recordatio can't be reached, so a
        memory-side outage never blocks the digest."""
        try:
            pending = 0
            users_with_pending = 0
            oldest: date | None = None
            by_type: dict[str, int] = {}
            for user_id in user_ids:
                data = await client.get_memory_feedback(user_id)
                user_pending = 0
                for rec in data.get("records", []):
                    try:
                        rec_day: date | None = date.fromisoformat(rec.get("day", ""))
                    except ValueError:
                        rec_day = None
                    for op in rec.get("ops", []):
                        if op.get("status") != "pending":
                            continue
                        pending += 1
                        user_pending += 1
                        op_type = op.get("op_type", "?")
                        by_type[op_type] = by_type.get(op_type, 0) + 1
                        if rec_day is not None and (oldest is None or rec_day < oldest):
                            oldest = rec_day
                if user_pending:
                    users_with_pending += 1
            age = (day - oldest).days if oldest is not None else None
            return MemoryBacklog(
                pending=pending, users_with_pending=users_with_pending,
                oldest_age_days=age, by_type=by_type,
            )
        except Exception:
            log.exception("nightly memory backlog snapshot failed")
            return None

    def _send_digest(
        self, run_id: int, backlog: MemoryBacklog | None = None, reconcile: MemoryReconcile | None = None
    ) -> None:
        """Email the owner a summary of the run (best-effort)."""
        if self._email_sink is None or not self._cfg.owner_email:
            return
        run = self._store.get_run(run_id)
        if run is None:
            return
        rows = self._store.list_agent_feedback(run_id)
        try:
            subject = f"Nightly feedback — {run.day} ({run.status})"
            self._email_sink(self._cfg.owner_email, subject, self._render_digest(run, rows, backlog, reconcile))
        except Exception:
            log.exception("nightly digest email failed")

    def _render_digest(
        self,
        run: NightlyRunRow,
        rows: list,
        backlog: MemoryBacklog | None = None,
        reconcile: MemoryReconcile | None = None,
    ) -> str:
        """Scannable digest: per-agent scores + truncated blurbs, not full prose.

        The complete per-agent feedback and memory findings live in the app
        (each agent's Nightly feedback) and the memory dashboard, so the
        email teases and links rather than reproducing walls of text.
        """
        lines = [f"<h2>Nightly feedback — {run.day}</h2>",
                 f"<p>Status: <strong>{run.status}</strong> · agents reviewed: {len(rows)} · "
                 f"cost: ${run.cost_usd:.2f}</p>"]
        if run.error:
            lines.append(f"<p style='color:#b00'>Run error: {run.error}</p>")
        lines.append("<h3>Agents</h3><ul>")
        for r in rows:
            if r.status != "completed" or not r.feedback:
                lines.append(f"<li><strong>{r.user_id}/{r.slug}</strong>: ⚠ {r.status} — {r.error or 'no feedback'}</li>")
                continue
            fb = r.feedback
            issues = len(fb.get("failures", []))
            misses = len(fb.get("recall_misses", []))
            lines.append(
                f"<li><strong>{r.user_id}/{r.slug}</strong> — score {fb.get('score', '?')}/5"
                f" · {issues} issue(s) · {misses} recall miss(es)<br>"
                f"<em>{_truncate(fb.get('summary', ''), 180)}</em></li>"
            )
        lines.append("</ul>")
        if run.memory_summary:
            excerpt = _truncate(run.memory_summary, 400)
            lines.append(f"<h3>Memory</h3><p>{excerpt}</p>")
        if reconcile is not None:
            kinds = ", ".join(f"{n} {r.replace('_', ' ')}" for r, n in sorted(reconcile.by_relation.items()))
            errors = f", {reconcile.judge_errors} failed" if reconcile.judge_errors else ""
            lines.append(
                f"<h3>Memory reconcile</h3><p>Deleted <strong>{reconcile.deleted}</strong> superseded "
                f"memor{'y' if reconcile.deleted == 1 else 'ies'}{f' ({kinds})' if kinds else ''} · ops: "
                f"{reconcile.ops_applied} applied, {reconcile.ops_dismissed} dismissed, {reconcile.ops_pending} "
                f"left pending · {reconcile.judge_calls} judge call(s){errors}, ${reconcile.cost_usd:.2f}.</p>"
            )
        if backlog is not None:
            if backlog.pending:
                breakdown = ", ".join(f"{n} {t}" for t, n in sorted(backlog.by_type.items()))
                age = (
                    f", oldest {backlog.oldest_age_days} day(s)"
                    if backlog.oldest_age_days is not None else ""
                )
                lines.append(
                    f"<h3>Memory-hygiene queue</h3><p><strong>{backlog.pending}</strong> op(s) pending "
                    f"across {backlog.users_with_pending} user(s){age} ({breakdown}). "
                    f"The nightly reconcile re-checks them automatically; these are the ones it "
                    f"could not decide yet.</p>"
                )
            else:
                lines.append("<h3>Memory-hygiene queue</h3><p>No pending ops — queue clear.</p>")
        lines.append(
            f"<hr><p><a href=\"{self._cfg.dashboard_url}\">Open the memory dashboard</a> "
            f"to review the full findings and the reconcile outcome of each proposed clean-up. "
            f"Per-agent feedback is in the app under each agent's Nightly feedback.</p>"
        )
        return "\n".join(lines)


__all__ = ["NightlyFeedbackService", "NightlyConfig", "MemoryReconcile"]
