"""Bring the outcome of Cardea-executed routine runs back into Telos.

Cardea runs each mirrored routine slot (see :mod:`telos.cardea_bridge`). Every sync pass,
:class:`RunPoller` reads the supervisor's triggers and their recent slot events, fetches the run
view of each admitted task it has not ingested yet, and once the run is terminal:

- **shadow** run (any call operation name ends in ``preview``, e.g. ``mail.preview``,
  ``ledger.record_preview``): one ``cardea_shadow_runs`` row and
  nothing else, so shadow mode never touches agent history, memory or routine results;
- **live** run: a routine turn in the agent's ``history.db`` (one ``tool_call``/``tool_result``
  pair per Cardea call carrying its receipted state and failure code/reason/summary, a
  ``cardea_run`` row with the run outcome, cost, turns and per-call outcomes (the evidence the
  nightly review checks the recap against), an ``email_delivery`` row per delivered ``mail.send``, then
  ``turn_completed`` or ``turn_failed``), a ``routine_results`` row keyed by the task id,
  ``last_run`` set to the slot time and, behind ``TELOS_CARDEA_EXTRACT=1``, a Recordatio
  transcript so memory extraction sees the run.

Failed runs, refused (``failed``) or ``missed`` slots, mail calls left ``outcome_unknown`` and
slots with nothing admitted 20 minutes after they were due send Telos's usual FAILED routine
notice for live routines; shadow failures are only logged and stored.

Cardea's run view is bounded: calls are paged (joined by ``CardeaClient.run``), large arguments
and results come as previews (mail calls are completed from their receipts, since their full
text is delivery evidence) and an oversize output is read whole from the task's output. A run
view an older Cardea refuses as too large (413) is logged once and retried with back-off.

Everything is idempotent on the task id (or trigger event id), so a crash or restart replays
nothing twice.
"""

import html
import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from apscheduler.triggers.cron import CronTrigger

from telos.cardea_bridge import DEFAULT_OPERATIONS
from telos.core.persistence import Database
from telos.core.repos.routines import Routine
from telos.local_time import local_timezone
from telos.log import ConversationLog
from telos.routines import RoutineStore

log = logging.getLogger("telos.cardea.ingest")

TERMINAL = {"succeeded", "failed", "cancelled"}
WATCHDOG_GRACE = timedelta(minutes=20)
# Slot events older than this are recorded silently: the first poll after deploy must not
# replay a backlog of FAILED emails for slots the user has already lived through.
NOTIFY_WINDOW = timedelta(hours=6)
# A run view Cardea refuses as too large (413) is retried after this, doubling up to the cap,
# and logged once per task rather than on every one-minute pass.
OVERSIZE_RETRY = timedelta(minutes=30)
OVERSIZE_RETRY_CAP = timedelta(hours=6)
TOO_LARGE = 413
_TOOL_FOR = {operation: tool for tool, operation in DEFAULT_OPERATIONS.items()}
# Calls whose full arguments/result Telos records (delivery evidence), fetched per call when the
# bounded run view only has previews of them.
FULL_CALLS = frozenset({"mail.send", "mail.preview"})
# Per-call outcomes copied into the run's ``cardea_run`` evidence row (the nightly review reads
# it as the receipts' summary); Cardea already bounds failure summaries to 200 characters.
OUTCOMES_CAP = 100
SUMMARY_CHARS = 200


class RunSource(Protocol):
    def triggers(self) -> list[dict[str, Any]]: ...
    def run(self, task_id: str) -> dict[str, Any]: ...
    def operation(self, task_id: str, operation_id: str) -> dict[str, Any]: ...
    def output(self, task_id: str) -> Any: ...


class FailureNotifier(Protocol):
    def notify_failure(self, user_id: str, agent_slug: str, routine: Any, reason: str, partial: str = "", *,
                       date_str: str, turn_id: str | None = None) -> None: ...


def is_shadow(operations: Iterable[str]) -> bool:
    return any(name.endswith("preview") for name in operations)


def _result(call: dict[str, Any]) -> dict[str, Any]:
    """A call's operation output: Cardea wraps it as ``{"output": ...}`` (plus ``deduplicated``)."""
    result = call.get("result") or {}
    output = result.get("output", result) if isinstance(result, dict) else result
    return output if isinstance(output, dict) else {"value": output}


def _shown(call: dict[str, Any], part: str) -> Any:
    """Arguments or result for display: inline when Cardea sent them, else its bounded preview."""
    if call.get(part) is not None:
        return call[part]
    if call.get(f"{part}_preview") is not None:
        return {"preview": call[f"{part}_preview"], "bytes": call.get(f"{part}_bytes")}
    return None


def call_failure(call: dict[str, Any]) -> dict[str, Any] | None:
    """A non-succeeded call's receipted failure: code, reason and Cardea's sanitized summary.

    The summary carries what the agent saw (e.g. ``HTTP 410 opening <url>`` or a navigation
    timeout), so it is the evidence a recap's "blocked/timed out" statement is checked against.
    """
    if call.get("state") == "succeeded":
        return None
    details = call.get("failure_details")
    details = details if isinstance(details, dict) else {}
    summary = details.get("summary")
    failure = {
        "code": call.get("failure") or call.get("state"),
        "reason": details.get("reason"),
        "summary": summary[:SUMMARY_CHARS] if isinstance(summary, str) else None,
    }
    return {k: v for k, v in failure.items() if v}


def call_outcome(call: dict[str, Any]) -> dict[str, Any]:
    """One receipt in brief: what was called, how it ended and whether it was a deduplicated replay."""
    outcome: dict[str, Any] = {"operation": call["operation"], "operation_id": call["operation_id"], "state": call.get("state"),
                               "deduplicated": bool(call.get("deduplicated"))}
    if (failure := call_failure(call)) is not None:
        outcome["failure"] = failure
    return outcome


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class IngestLedger:
    """What has been ingested, plus the shadow-run table; lives in ``routines.db``."""

    def __init__(self, db: Database) -> None:
        self._db = db
        with db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cardea_shadow_runs (
                    task_id     TEXT PRIMARY KEY,
                    trigger_id  TEXT NOT NULL,
                    routine_id  TEXT NOT NULL,
                    slot        TEXT,
                    state       TEXT NOT NULL,
                    failure     TEXT,
                    output_text TEXT NOT NULL DEFAULT '',
                    calls_json  TEXT NOT NULL DEFAULT '[]',
                    cost_usd    REAL,
                    ingested_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cardea_ingested (
                    key         TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    routine_id  TEXT,
                    at          TEXT NOT NULL
                )
            """)

    def seen(self, key: str) -> bool:
        with self._db.connect() as conn:
            return conn.execute("SELECT 1 FROM cardea_ingested WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key: str, kind: str, routine_id: str | None) -> bool:
        with self._db.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO cardea_ingested (key, kind, routine_id, at) VALUES (?,?,?,?)",
                (key, kind, routine_id, datetime.now(UTC).isoformat()),
            )
        return cursor.rowcount == 1

    def save_shadow(self, *, task_id: str, trigger_id: str, routine_id: str, slot: str | None, run: dict[str, Any],
                    calls: list[dict[str, Any]]) -> None:
        output = run.get("output") or {}
        failure = (run.get("failure") or {}).get("code")
        with self._db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO cardea_shadow_runs (task_id, trigger_id, routine_id, slot, state, failure, output_text, "
                "calls_json, cost_usd, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, trigger_id, routine_id, slot, run["state"], failure, output.get("text") or "",
                 json.dumps(calls, ensure_ascii=False, default=str), output.get("cost_usd"), datetime.now(UTC).isoformat()),
            )

    def shadow_runs(self, routine_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM cardea_shadow_runs"
        args: tuple[str, ...] = ()
        if routine_id:
            query, args = query + " WHERE routine_id=?", (routine_id,)
        with self._db.connect() as conn:
            rows = conn.execute(query + " ORDER BY slot", args).fetchall()
        return [{**dict(r), "calls": json.loads(r["calls_json"])} for r in rows]


class TranscriptSink:
    """Recordatio contract C5: ``POST /v1/agents/{user}/{slug}/transcripts`` (local callers only)."""

    def __init__(self, base_url: str, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=30.0, transport=transport)

    def __call__(self, user_id: str, slug: str, *, input: str, output: str, occurred_at: str, idempotency_key: str) -> None:
        response = self.http.post(
            f"/v1/agents/{user_id}/{slug}/transcripts",
            json={"input": input, "output": output, "occurred_at": occurred_at, "source": "cardea",
                  "idempotency_key": idempotency_key},
        )
        response.raise_for_status()


class RunPoller:
    """Ingests terminal Cardea runs and raises FAILED notices; call :meth:`poll` from the sync loop."""

    def __init__(
        self,
        client: RunSource,
        routines: RoutineStore,
        ledger: IngestLedger,
        storage_root: Path,
        notifier: FailureNotifier,
        *,
        transcripts: Callable[..., None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.routines = routines
        self.ledger = ledger
        self.storage_root = storage_root
        self.notifier = notifier
        self.transcripts = transcripts
        self.clock = clock
        # task id -> (consecutive 413s, earliest next fetch); in memory, so a restart retries once.
        self._oversize: dict[str, tuple[int, datetime]] = {}

    def poll(self) -> dict[str, str]:
        """One pass over every ``telos.*`` trigger; returns what happened per task/event key."""
        outcomes: dict[str, str] = {}
        for trigger in self.client.triggers():
            identity = trigger["trigger_id"]
            if not identity.startswith("telos.") or trigger.get("removed"):
                continue
            routine = self.routines.get(identity.removeprefix("telos."))
            if routine is None:
                continue
            for event in sorted(trigger.get("events") or [], key=lambda e: e["event_id"]):
                try:
                    outcome = self._event(trigger, routine, event)
                except httpx.HTTPError as error:
                    # Transient: the task stays unseen and the next pass retries it.
                    log.warning("cardea run fetch failed for %s: %s", event.get("task_id"), error)
                    continue
                except Exception:
                    log.exception("cardea ingestion failed for %s event %s", identity, event.get("event_id"))
                    continue
                if outcome:
                    outcomes[event.get("task_id") or f"{identity}#{event['event_id']}"] = outcome
            try:
                if (overdue := self._watchdog(trigger, routine)) is not None:
                    outcomes[f"{identity}@{overdue}"] = "overdue"
            except Exception:
                log.exception("cardea slot watchdog failed for %s", identity)
        return outcomes

    # -- slot events ------------------------------------------------------------------

    def _event(self, trigger: dict[str, Any], routine: Routine, event: dict[str, Any]) -> str | None:
        kind = event["kind"]
        if kind == "admitted" and event.get("task_id"):
            return self._task(trigger, routine, event["task_id"], event.get("slot"))
        if kind not in {"failed", "missed"}:
            return None
        key = f"event:{trigger['trigger_id']}:{event['event_id']}"
        if self.ledger.seen(key):
            return None
        if kind == "failed":
            reason, when = f"slot refused by Cardea ({event.get('code') or 'unknown'})", event.get("slot")
        else:
            detail = event.get("detail") or {}
            reason, when = f"{detail.get('count', 1)} slot(s) missed by Cardea", detail.get("last") or event.get("at")
        shadow = is_shadow(trigger["definition"]["template"].get("operations", []))
        recent = (_parse(event.get("at")) or self.clock()) >= self.clock() - NOTIFY_WINDOW
        if shadow or not recent:
            log.warning("cardea %s routine %s: %s", "shadow" if shadow else "stale", routine.id, reason)
        else:
            self._notify(routine, reason, when=when)
        self.ledger.mark(key, kind, routine.id)
        return kind

    def _task(self, trigger: dict[str, Any], routine: Routine, task_id: str, slot: str | None) -> str | None:
        if self.ledger.seen(f"task:{task_id}"):
            return None
        run = self._fetch(task_id, routine)
        if run is None:
            return None
        if run["state"] not in TERMINAL:
            return None
        if run.get("output_truncated"):
            run = {**run, "output": self.client.output(task_id)}
        calls = [self._full(task_id, c) for c in run.get("calls") or []]
        operations = [c["operation"] for c in calls] or trigger["definition"]["template"].get("operations", [])
        if is_shadow(operations):
            self.ledger.save_shadow(task_id=task_id, trigger_id=trigger["trigger_id"], routine_id=routine.id,
                                    slot=slot, run=run, calls=calls)
            if run["state"] != "succeeded":
                log.warning("cardea shadow run %s for routine %s ended %s: %s", task_id, routine.id,
                            run["state"], (run.get("failure") or {}).get("code"))
            self.ledger.mark(f"task:{task_id}", "shadow", routine.id)
            return f"shadow:{run['state']}"
        outcome = self._live(routine, task_id, slot, run, calls)
        self.ledger.mark(f"task:{task_id}", "live", routine.id)
        return outcome

    def _full(self, task_id: str, call: dict[str, Any]) -> dict[str, Any]:
        """``call`` with full arguments and result from its receipt when the view only had previews."""
        missing = call.get("arguments") is None or (call.get("result") is None and call.get("result_bytes"))
        if call["operation"] not in FULL_CALLS or not missing:
            return call
        receipt = self.client.operation(task_id, call["operation_id"])
        return {**call, "arguments": (receipt.get("request") or {}).get("arguments", {}).get("arguments"),
                "result": receipt.get("result")}

    def _fetch(self, task_id: str, routine: Routine) -> dict[str, Any] | None:
        """The task's run view, or None while a too-large view is backing off."""
        failures, retry_at = self._oversize.get(task_id, (0, self.clock()))
        if self.clock() < retry_at:
            return None
        try:
            run = self.client.run(task_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != TOO_LARGE:
                raise
            delay = min(OVERSIZE_RETRY * 2**failures, OVERSIZE_RETRY_CAP)
            self._oversize[task_id] = (failures + 1, self.clock() + delay)
            if failures == 0:
                log.error("cardea run view for task %s (routine %s) is too large to fetch (413); retrying every %s at most",
                          task_id, routine.id, delay)
            return None
        self._oversize.pop(task_id, None)
        return run

    # -- live runs ----------------------------------------------------------------------

    def _live(self, routine: Routine, task_id: str, slot: str | None, run: dict[str, Any], calls: list[dict[str, Any]]) -> str:
        text = (run.get("output") or {}).get("text") or ""
        reason = self._failure(run, calls)
        turn_id = self._history(routine, task_id, slot, run, calls, text, reason)
        if reason is None:
            self.routines.save_result(routine.id, text, source_task_id=task_id)
        slot_time = _parse(slot) or _parse(run.get("updated_at")) or self.clock()
        self.routines.mark_run(routine.id, slot_time.astimezone(UTC).isoformat())
        if reason is not None and self._delivered_recap(calls):
            # Cardea can end a run non-successfully (e.g. deadline_exceeded/expectation_exceeded) only
            # AFTER the agent has already delivered a full recap via mail.send. Firing the usual
            # "This run failed before completing … No result was produced." notice on top of that recap
            # contradicts a deliverable the user already received moments earlier, so suppress it. The
            # turn is still recorded as turn_failed (below, via _history) for operators; this only drops
            # the contradictory email. This mirrors the in-process guard (test_routine_execution.py:144/247).
            log.warning("cardea routine %s ended %s but a recap was already delivered; suppressing FAILED notice",
                        routine.id, reason)
        elif reason is not None:
            recent = (_parse(run.get("updated_at")) or self.clock()) >= self.clock() - NOTIFY_WINDOW
            if recent:
                self._notify(routine, reason, partial=text, when=slot, turn_id=turn_id)
        elif self.transcripts is not None:
            try:
                self.transcripts(routine.user_id, routine.agent_slug, input=routine.description, output=text,
                                 occurred_at=slot_time.isoformat(), idempotency_key=f"cardea:{task_id}")
            except Exception as error:
                log.warning("recordatio transcript for cardea task %s failed: %s", task_id, error)
        return "live:failed" if reason else "live:succeeded"

    @staticmethod
    def _failure(run: dict[str, Any], calls: list[dict[str, Any]]) -> str | None:
        if run["state"] != "succeeded":
            return (run.get("failure") or {}).get("code") or run["state"]
        if any(c["operation"] == "mail.send" and c.get("state") == "outcome_unknown" for c in calls):
            return "mail_outcome_unknown"
        return None

    @staticmethod
    def _delivered_recap(calls: list[dict[str, Any]]) -> bool:
        """True iff the run already delivered a recap (a succeeded ``mail.send``), so a FAILED
        'no result was produced' notice on top of it would contradict a deliverable the user has."""
        return any(c["operation"] == "mail.send" and c.get("state") == "succeeded" for c in calls)

    def _history(self, routine: Routine, task_id: str, slot: str | None, run: dict[str, Any], calls: list[dict[str, Any]],
                 text: str, reason: str | None) -> str:
        """Write the run as one routine turn; returns its turn id (an existing one on replay)."""
        history = ConversationLog(self.storage_root / routine.user_id / routine.agent_slug / "history.db")
        trace = f"cardea:{task_id}"
        with history._db.connect() as conn:  # noqa: SLF001 (lookup by trace id has no repo method)
            row = conn.execute("SELECT turn_id FROM turns WHERE trace_id=?", (trace,)).fetchone()
        if row is not None:
            return str(row[0])
        turn_id = history.append(routine.description, text, "cardea", is_routine=True, trace_id=trace)
        events = history.execution_events
        output = run.get("output") or {}
        outcomes = [call_outcome(c) for c in calls]
        events.append(turn_id, "cardea_run", {
            "task_id": task_id, "slot": slot, "state": run["state"], "failure": run.get("failure"),
            "attempt": run.get("attempt"), "turns": output.get("turns"), "cost_usd": output.get("cost_usd"),
            # Cardea's receipts are the authoritative record of what each call of this run did.
            "evidence": "cardea_receipts", "calls_total": len(calls),
            "failed_calls": sum(1 for o in outcomes if o["state"] != "succeeded"),
            "outcomes": outcomes[:OUTCOMES_CAP], "outcomes_omitted": max(len(outcomes) - OUTCOMES_CAP, 0),
        })
        for call in calls:
            name = call["operation"]
            failure = call_failure(call)
            events.append(turn_id, "tool_call", {
                "call_id": call["operation_id"], "name": name, "tool": _TOOL_FOR.get(name), "input": _shown(call, "arguments") or {},
            })
            events.append(turn_id, "tool_result", {
                "call_id": call["operation_id"], "name": name, "is_error": call.get("state") != "succeeded",
                "state": call.get("state"), "result": shown if (shown := _shown(call, "result")) is not None else failure,
                "failure": failure,
                "reconciled": bool(call.get("reconciled")), "deduplicated": bool(call.get("deduplicated")),
            })
            if name == "mail.send" and call.get("state") in {"succeeded", "outcome_unknown"}:
                # The only delivery evidence for Cardea sends: tool rows are named after the
                # operation, so recent_email_deliveries counts each email exactly once.
                arguments = call.get("arguments") or {}
                result = _result(call)
                events.append(turn_id, "email_delivery", {
                    "to": arguments.get("to", ""), "subject": arguments.get("subject", ""), "body": arguments.get("body", ""),
                    "status": "accepted_by_smtp" if call["state"] == "succeeded" else "unknown",
                    "message_id": result.get("message_id"), "source": "cardea",
                    "notification_completed_at": call.get("updated_at"),
                })
        if reason is None:
            events.append(turn_id, "turn_completed", {"response_chars": len(text)})
        else:
            events.append(turn_id, "turn_failed", {"reason": reason, "response_chars": len(text)})
        return turn_id

    # -- missed-slot watchdog -----------------------------------------------------------

    def _watchdog(self, trigger: dict[str, Any], routine: Routine) -> str | None:
        """FAILED notice once per slot still not admitted ``WATCHDOG_GRACE`` after it was due."""
        if not trigger.get("enabled"):
            return None
        definition = trigger["definition"]
        # Slots count from the last admitted/closed slot, or from (re)installation when later.
        starts = [_parse(e["at"]) for e in trigger.get("events") or [] if e["kind"] in {"installed", "enabled"}]
        bases = [b for b in (_parse(trigger.get("last_slot")), *starts) if b is not None]
        if not bases:
            return None
        base = max(bases)
        now = self.clock()
        cron = CronTrigger.from_crontab(definition["schedule"], timezone=ZoneInfo(definition.get("timezone") or "UTC"))
        due = cron.get_next_fire_time(base, now)
        if due is None or due > now - WATCHDOG_GRACE:
            return None
        slot = due.isoformat()
        if not self.ledger.mark(f"watchdog:{trigger['trigger_id']}:{slot}", "overdue", routine.id):
            return None
        reason = "Cardea admitted no run within 20 minutes of the scheduled slot"
        if is_shadow(definition["template"].get("operations", [])):
            log.warning("cardea shadow routine %s: %s (%s)", routine.id, reason, slot)
        else:
            self._notify(routine, reason, when=slot)
        return slot

    def _notify(self, routine: Routine, reason: str, *, partial: str = "", when: str | None = None,
                turn_id: str | None = None) -> None:
        at = (_parse(when) or self.clock()).astimezone(local_timezone())
        log.warning("cardea routine %s failed: %s", routine.id, reason)
        self.notifier.notify_failure(routine.user_id, routine.agent_slug, routine, reason, partial,
                                     date_str=at.strftime("%d %b %Y"), turn_id=turn_id)


# -- operator alerts ----------------------------------------------------------------------

_operator_alert: Callable[[str, str], object] | None = None


def configure_operator_alerts(send: Callable[[str, str, str], object], recipient: str) -> None:
    """Route the operator hooks below to ``send(to, subject, body)``; an empty recipient disables them."""
    global _operator_alert
    _operator_alert = (lambda subject, body: send(recipient, subject, body)) if recipient else None


def _alert(subject: str, body: str) -> None:
    if _operator_alert is None:
        log.error("cardea operator alert (no operator email configured): %s", subject)
        return
    try:
        _operator_alert(subject, body)
    except Exception:
        log.exception("cardea operator alert failed")


def on_sync_failure(count: int, error: str) -> None:
    """Bridge hook: Cardea sync failed ``count`` times in a row. The bridge decides when to call it."""
    _alert(
        f"Telos: Cardea sync failing ({count} consecutive failures)",
        f"<p>Telos could not sync with Cardea {count} times in a row.</p><p>Last error: {html.escape(error)}</p>"
        "<p>Mirrored routines may not be running or reporting. Check the Cardea host.</p>",
    )


def on_refusal(routine_id: str, reason: str) -> None:
    """Bridge hook: a live routine is running in Telos instead of Cardea."""
    _alert(
        f"Telos: routine {routine_id} not running on Cardea",
        f"<p>Routine <code>{html.escape(routine_id)}</code> is running in Telos instead of Cardea.</p>"
        f"<p>Reason: {html.escape(reason)}</p>",
    )


def extract_enabled() -> bool:
    return os.getenv("TELOS_CARDEA_EXTRACT", "0").strip() == "1"


__all__ = ["IngestLedger", "RunPoller", "TranscriptSink", "configure_operator_alerts", "extract_enabled",
           "is_shadow", "on_refusal", "on_sync_failure"]
