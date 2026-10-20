"""Cardea run ingestion: shadow vs live, idempotency, history rows, FAILED notices, watchdog."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from telos import cardea_ingest
from telos.cardea_ingest import (
    IngestLedger,
    RunPoller,
    TranscriptSink,
    configure_operator_alerts,
    is_shadow,
    on_refusal,
    on_sync_failure,
)
from telos.core.persistence import Database
from telos.log import ConversationLog
from telos.routines import RoutineStore
from telos.services.routine_execution import RoutineExecutor

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
SLOT = "2026-10-20T08:30:00+01:00"
EMAIL = {"to": "sam@example.test", "subject": "Daily search", "body": "Two new flats in E8."}


class FakeCardea:
    def __init__(self) -> None:
        self.trigger_list: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.fetched: list[str] = []
        self.receipts: dict[tuple[str, str], dict[str, Any]] = {}
        self.outputs: dict[str, Any] = {}
        self.receipt_fetches: list[str] = []

    def operation(self, task_id: str, operation_id: str) -> dict[str, Any]:
        self.receipt_fetches.append(operation_id)
        return self.receipts[(task_id, operation_id)]

    def output(self, task_id: str) -> Any:
        return self.outputs[task_id]

    def triggers(self) -> list[dict[str, Any]]:
        return self.trigger_list

    def run(self, task_id: str) -> dict[str, Any]:
        self.fetched.append(task_id)
        run = self.runs[task_id]
        if isinstance(run, Exception):
            raise run
        return run


def trigger(routine_id: str, events: list[dict[str, Any]], *, shadow: bool = False, last_slot: str | None = SLOT,
            enabled: bool = True) -> dict[str, Any]:
    mail = "mail.preview" if shadow else "mail.send"
    return {
        "trigger_id": f"telos.{routine_id}", "namespace": "telos-sam", "enabled": enabled, "removed": False,
        "last_slot": last_slot,
        "definition": {"schedule": "30 8 * * *", "timezone": "Europe/London",
                       "template": {"operations": ["listings.rightmove_search", mail]}},
        "events": events,
    }


def admitted(task_id: str, event_id: int = 1, slot: str = SLOT) -> dict[str, Any]:
    return {"event_id": event_id, "kind": "admitted", "slot": slot, "task_id": task_id, "code": None,
            "at": NOW.isoformat(), "detail": {"late": False}}


def call(operation: str, state: str = "succeeded", op_id: str = "op-01", **extra: Any) -> dict[str, Any]:
    arguments = EMAIL if operation.startswith("mail.") else {"area": "E8"}
    result = {"output": {"message_id": "<k@telos>", "to": EMAIL["to"], "status": "sent"}} if state == "succeeded" else None
    return {"operation_id": op_id, "operation": operation, "state": state, "arguments": arguments, "result": result,
            "failure": None if state == "succeeded" else state, "reconciled": False,
            "updated_at": NOW.isoformat(), **extra}


def run(state: str = "succeeded", calls: list[dict[str, Any]] | None = None, failure: str | None = None,
        text: str = "Two new flats in E8.") -> dict[str, Any]:
    calls = calls if calls is not None else [call("listings.rightmove_search", op_id="op-01"), call("mail.send", op_id="op-02")]
    return {"task_id": "t", "state": state, "failure": {"code": failure, "message": failure} if failure else None,
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "attempt": 1,
            "output": {"text": text, "turns": 4, "cost_usd": 0.12, "calls": []}, "calls": calls}


@pytest.fixture
def env(tmp_path: Path):
    store = RoutineStore(tmp_path / "routines.db")
    routine = store.create("sam", "scout", "Search listings in E8.", name="Daily search", interval_seconds=86400,
                           time_of_day="08:30", notify_email=True)
    email = MagicMock()
    email.send_routine_notification.return_value = True
    executor = RoutineExecutor(
        agent_runtime=MagicMock(), email_service=email, get_user_email_fn=lambda _u: "sam@example.test",
        get_agent_name_fn=lambda _u, _s: "Scout", base_url="https://telos.example.test", storage_root=tmp_path,
    )
    cardea = FakeCardea()
    ledger = IngestLedger(Database(tmp_path / "routines.db"))
    transcripts = MagicMock()
    poller = RunPoller(cardea, store, ledger, tmp_path, executor, transcripts=transcripts, clock=lambda: NOW)
    history = tmp_path / "sam" / "scout" / "history.db"
    return {"store": store, "routine": routine, "email": email, "cardea": cardea, "ledger": ledger,
            "poller": poller, "history": history, "transcripts": transcripts}


def events_of(history: Path) -> list[tuple[str, dict[str, Any]]]:
    log = ConversationLog(history)
    turn_id = log.last_turn_id()
    assert turn_id is not None
    return [(e["kind"], e["payload"]) for e in log.execution_events.list(turn_id, limit=20)]


def test_live_run_writes_turn_delivery_result_and_transcript(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run()

    assert env["poller"].poll() == {"task-1": "live:succeeded"}

    log = ConversationLog(env["history"])
    turn = log.recent_turns(1)[0]
    assert turn.assistant_response == "Two new flats in E8."
    kinds = [k for k, _ in events_of(env["history"])]
    assert kinds == ["cardea_run", "tool_call", "tool_result", "tool_call", "tool_result", "email_delivery", "turn_completed"]
    delivery = dict(events_of(env["history"]))["email_delivery"]
    assert delivery["status"] == "accepted_by_smtp" and delivery["subject"] == "Daily search"
    assert delivery["message_id"] == "<k@telos>"
    # Counted exactly once in the delivery history Telos feeds into routine context.
    assert [d["subject"] for d in log.execution_events.recent_email_deliveries("sam@example.test")] == ["Daily search"]

    results = env["store"].recent_results("sam", "scout")
    assert [r.response for r in results] == ["Two new flats in E8."]
    assert env["store"].has_source_task("task-1")
    assert env["store"].get(rid).last_run == "2026-10-20T07:30:00+00:00"
    env["transcripts"].assert_called_once()
    assert env["transcripts"].call_args.kwargs["idempotency_key"] == "cardea:task-1"
    env["email"].send_routine_notification.assert_not_called()


def test_shadow_run_stored_only_in_shadow_table(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-s")], shadow=True)]
    env["cardea"].runs["task-s"] = run(calls=[call("listings.rightmove_search"), call("mail.preview", op_id="op-02")])

    assert env["poller"].poll() == {"task-s": "shadow:succeeded"}

    rows = env["ledger"].shadow_runs(rid)
    assert len(rows) == 1 and rows[0]["state"] == "succeeded" and rows[0]["cost_usd"] == 0.12
    assert [c["operation"] for c in rows[0]["calls"]] == ["listings.rightmove_search", "mail.preview"]
    assert not env["history"].exists()
    assert env["store"].recent_results("sam", "scout") == []
    assert env["store"].get(rid).last_run is None
    env["transcripts"].assert_not_called()


@pytest.mark.parametrize(
    ("operations", "shadow"),
    [
        (["listings.rightmove_search", "mail.preview"], True),
        (["ledger.list", "ledger.record_preview"], True),
        (["ledger.list", "ledger.record", "mail.send"], False),
        ([], False),
    ],
)
def test_shadow_rule(operations, shadow):
    assert is_shadow(operations) is shadow


def test_ledger_preview_run_is_shadow(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-l")])]
    env["cardea"].runs["task-l"] = run(calls=[call("ledger.list"), call("ledger.record_preview", op_id="op-02")])

    assert env["poller"].poll() == {"task-l": "shadow:succeeded"}
    assert not env["history"].exists() and env["store"].recent_results("sam", "scout") == []


def test_shadow_failure_is_logged_and_stored_not_emailed(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-s")], shadow=True)]
    env["cardea"].runs["task-s"] = run("failed", calls=[], failure="expectation_unmet")

    env["poller"].poll()

    assert env["ledger"].shadow_runs()[0]["failure"] == "expectation_unmet"
    env["email"].send_routine_notification.assert_not_called()


def test_ingestion_is_idempotent_and_waits_for_terminal_state(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = {**run(), "state": "running"}
    assert env["poller"].poll() == {}
    assert not env["history"].exists()

    env["cardea"].runs["task-1"] = run()
    env["poller"].poll()
    env["poller"].poll()

    assert env["cardea"].fetched == ["task-1", "task-1"]
    assert ConversationLog(env["history"]).count() == 1
    assert len(env["store"].recent_results("sam", "scout")) == 1


def test_replay_after_crash_reuses_existing_turn(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run()
    env["poller"].poll()
    # Simulate a crash before the ingestion mark was written.
    with Database(env["store"]._db._path).connect() as conn:  # noqa: SLF001
        conn.execute("DELETE FROM cardea_ingested")
    env["poller"].poll()

    assert ConversationLog(env["history"]).count() == 1
    assert len(env["store"].recent_results("sam", "scout")) == 1


def test_fetch_error_is_retried_next_pass(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = httpx.ConnectError("down")
    assert env["poller"].poll() == {}
    env["cardea"].runs["task-1"] = run()
    assert env["poller"].poll() == {"task-1": "live:succeeded"}


def test_failed_live_run_sends_failed_notice_and_records_turn_failed(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run("failed", calls=[call("listings.rightmove_search")], failure="expectation_unmet",
                                       text="Partial notes")

    assert env["poller"].poll() == {"task-1": "live:failed"}

    kwargs = env["email"].send_routine_notification.call_args.kwargs
    assert kwargs["subject"] == "Scout: Daily search FAILED — 20 Oct 2026"
    assert "expectation_unmet" in kwargs["body"] and "Partial notes" in kwargs["body"]
    kinds = [k for k, _ in events_of(env["history"])]
    assert kinds[-2:] == ["turn_failed", "email_delivery"]
    # The FAILED notice is not a recap delivery.
    assert ConversationLog(env["history"]).execution_events.recent_email_deliveries("sam@example.test") == []
    assert env["store"].recent_results("sam", "scout") == []
    assert env["store"].get(rid).last_run is not None
    env["transcripts"].assert_not_called()


def test_mail_outcome_unknown_is_a_failure(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run(calls=[call("mail.send", "outcome_unknown")])

    assert env["poller"].poll() == {"task-1": "live:failed"}

    assert "mail_outcome_unknown" in env["email"].send_routine_notification.call_args.kwargs["body"]
    deliveries = [p for k, p in events_of(env["history"]) if k == "email_delivery"]
    # The Cardea send of unknown outcome, then the FAILED notice itself.
    assert [d["status"] for d in deliveries] == ["unknown", "accepted_by_smtp"]
    assert "FAILED" in deliveries[1]["subject"]


def test_failed_and_missed_trigger_events_notify_once(env):
    rid = env["routine"].id
    events = [
        {"event_id": 3, "kind": "failed", "slot": SLOT, "task_id": None, "code": "task_grant_denied",
         "at": NOW.isoformat(), "detail": {}},
        {"event_id": 2, "kind": "missed", "slot": None, "task_id": None, "code": None, "at": NOW.isoformat(),
         "detail": {"count": 2, "first": "2026-10-18T08:30:00+01:00", "last": "2026-10-19T08:30:00+01:00"}},
        {"event_id": 1, "kind": "installed", "slot": None, "task_id": None, "code": None,
         "at": (NOW - timedelta(days=3)).isoformat(), "detail": {}},
    ]
    env["cardea"].trigger_list = [trigger(rid, events)]

    assert env["poller"].poll() == {f"telos.{rid}#2": "missed", f"telos.{rid}#3": "failed"}
    env["poller"].poll()

    bodies = [c.kwargs["body"] for c in env["email"].send_routine_notification.call_args_list]
    assert len(bodies) == 2
    assert "2 slot(s) missed" in bodies[0] and "task_grant_denied" in bodies[1]


def test_old_events_are_marked_without_notifying(env):
    rid = env["routine"].id
    stale = (NOW - timedelta(days=2)).isoformat()
    env["cardea"].trigger_list = [trigger(rid, [{"event_id": 1, "kind": "failed", "slot": SLOT, "task_id": None,
                                                 "code": "x", "at": stale, "detail": {}}])]
    env["poller"].poll()
    env["email"].send_routine_notification.assert_not_called()


def test_watchdog_alerts_once_for_overdue_slot(env):
    rid = env["routine"].id
    # Last slot was yesterday; today's 08:30 London (07:30 UTC) slot is 90 minutes overdue.
    env["cardea"].trigger_list = [trigger(rid, [], last_slot="2026-10-19T08:30:00+01:00")]

    assert env["poller"].poll() == {f"telos.{rid}@2026-10-20T08:30:00+01:00": "overdue"}
    env["poller"].poll()

    env["email"].send_routine_notification.assert_called_once()
    assert "20 minutes" in env["email"].send_routine_notification.call_args.kwargs["body"]


@pytest.mark.parametrize(
    ("changes", "clock"),
    [
        ({}, NOW),  # today's slot already admitted
        ({"last_slot": "2026-10-19T08:30:00+01:00"}, datetime(2026, 10, 20, 7, 45, tzinfo=UTC)),  # within grace
        ({"last_slot": "2026-10-19T08:30:00+01:00", "enabled": False}, NOW),
        ({"last_slot": "2026-10-19T08:30:00+01:00", "shadow": True}, NOW),  # shadow: logged only
    ],
)
def test_watchdog_quiet_cases(env, changes, clock):
    env["poller"].clock = lambda: clock
    env["cardea"].trigger_list = [trigger(env["routine"].id, [], **changes)]
    env["poller"].poll()
    env["email"].send_routine_notification.assert_not_called()


def test_foreign_and_unknown_triggers_are_ignored(env):
    env["cardea"].trigger_list = [
        {**trigger("nope00000000", [admitted("task-x")]), "trigger_id": "other.job"},
        trigger("missing00000", [admitted("task-y")]),
    ]
    assert env["poller"].poll() == {}
    assert env["cardea"].fetched == []


def test_transcript_sink_posts_contract_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"accepted": True})

    sink = TranscriptSink("http://recordatio:8787/", "key", transport=httpx.MockTransport(handler))
    sink("sam", "scout", input="Search.", output="Found two.", occurred_at=SLOT, idempotency_key="cardea:task-1")

    assert seen[0].url.path == "/v1/agents/sam/scout/transcripts"
    assert seen[0].headers["authorization"] == "Bearer key"
    assert json.loads(seen[0].content) == {"input": "Search.", "output": "Found two.", "occurred_at": SLOT,
                                           "source": "cardea", "idempotency_key": "cardea:task-1"}


def test_operator_alerts_deliver_each_bridge_hook_call():
    sent = MagicMock()
    configure_operator_alerts(sent, "ops@example.test")
    try:
        on_sync_failure(3, "ConnectError: refused")
        on_refusal("73103f92fad9", "unmapped tools: <gdocs_read>")
        assert sent.call_count == 2
        to, subject, body = sent.call_args_list[0].args
        assert to == "ops@example.test" and "3 consecutive" in subject and "refused" in body
        _, subject, body = sent.call_args_list[1].args
        assert "73103f92fad9" in subject and "&lt;gdocs_read&gt;" in body
    finally:
        configure_operator_alerts(sent, "")
    assert cardea_ingest._operator_alert is None  # noqa: SLF001
    on_sync_failure(6, "still down")  # unconfigured: logged, not raised


def too_large() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://cardea.test/v0.2/tasks/task-1/run")
    response = httpx.Response(413, json={"code": "response_too_large"}, request=request)
    return httpx.HTTPStatusError("413 response_too_large", request=request, response=response)


def test_oversize_run_view_is_logged_once_and_backed_off(env, caplog):
    rid = env["routine"].id
    clock = {"now": NOW}
    env["poller"].clock = lambda: clock["now"]
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = too_large()
    caplog.set_level("WARNING", logger="telos.cardea.ingest")
    for _ in range(5):  # one-minute passes
        assert env["poller"].poll() == {}
        clock["now"] += timedelta(minutes=1)
    assert env["cardea"].fetched == ["task-1"]
    assert len([r for r in caplog.records if "too large" in r.getMessage()]) == 1
    clock["now"] = NOW + timedelta(minutes=31)
    env["poller"].poll()
    assert env["cardea"].fetched == ["task-1", "task-1"]  # retried after the back-off, then waits 60 min
    clock["now"] += timedelta(minutes=59)
    env["poller"].poll()
    assert len(env["cardea"].fetched) == 2
    env["cardea"].runs["task-1"] = run()
    clock["now"] += timedelta(minutes=2)
    assert env["poller"].poll() == {"task-1": "live:succeeded"}
    assert len([r for r in caplog.records if "too large" in r.getMessage()]) == 1


def test_oversize_backoff_is_capped(env):
    rid = env["routine"].id
    clock = {"now": NOW}
    env["poller"].clock = lambda: clock["now"]
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = too_large()
    for _ in range(12):
        env["poller"].poll()
        clock["now"] += timedelta(hours=6, seconds=1)
    assert len(env["cardea"].fetched) == 12  # never waits longer than 6 h


def test_tasks_that_413d_are_ingested_on_the_first_pass_after_a_deploy(env):
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = too_large()
    assert env["poller"].poll() == {}
    assert not env["ledger"].seen("task:task-1")  # never given up on
    # The fixed Telos restarts (fresh poller, same ledger) against the bounded run view.
    env["cardea"].runs["task-1"] = run()
    restarted = RunPoller(env["cardea"], env["store"], env["ledger"], env["history"].parents[2], env["poller"].notifier,
                          transcripts=env["transcripts"], clock=lambda: NOW + timedelta(minutes=1))
    assert restarted.poll() == {"task-1": "live:succeeded"}


# ── bounded run view (Cardea run views with previews, paged calls, omitted output) ───────


LONG_BODY = "Listing details. " * 400


def bounded(call_: dict[str, Any]) -> dict[str, Any]:
    """``call_`` as the bounded view shows a call whose arguments and result exceed the preview size."""
    arguments, result = json.dumps(call_["arguments"]), json.dumps(call_["result"])
    return {**call_, "arguments": None, "arguments_bytes": len(arguments), "arguments_sha256": "a" * 64,
            "arguments_preview": arguments[:2048], "result": None,
            "result_bytes": len(result) if call_["result"] else None, "result_sha256": "b" * 64 if call_["result"] else None,
            "result_preview": result[:2048] if call_["result"] else None, "receipt_id": f"r-{call_['operation_id']}",
            "deduplicated": False}


def test_bounded_mail_call_is_completed_from_its_receipt(env):
    rid = env["routine"].id
    email = {**EMAIL, "body": LONG_BODY}
    mail = call("mail.send", op_id="call-0002", arguments=email)
    search = call("listings.rightmove_search", op_id="call-0001")
    search = {**bounded({**search, "arguments": {"area": "E8", "notes": "x" * 3000}})}
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run(calls=[search, bounded(mail)])
    env["cardea"].receipts[("task-1", "call-0002")] = {
        "request": {"name": "mail.send", "arguments": {"arguments": email}}, "state": "succeeded",
        "result": {"output": {"message_id": "<full@telos>", "to": EMAIL["to"], "status": "sent"}},
        "failure": None, "reconciliation_evidence": [],
    }

    assert env["poller"].poll() == {"task-1": "live:succeeded"}
    assert env["cardea"].receipt_fetches == ["call-0002"]  # only the mail call is fetched in full
    events = events_of(env["history"])
    delivery = dict(events)["email_delivery"]
    assert delivery["body"] == LONG_BODY and delivery["message_id"] == "<full@telos>" and delivery["to"] == EMAIL["to"]
    search_input = next(p for k, p in events if k == "tool_call" and p["name"] == "listings.rightmove_search")["input"]
    assert search_input["bytes"] > 3000 and search_input["preview"].startswith('{"area": "E8"')
    log = ConversationLog(env["history"])
    assert [d["body"] for d in log.execution_events.recent_email_deliveries("sam@example.test")] == [LONG_BODY]


def test_truncated_output_is_read_whole(env):
    rid = env["routine"].id
    full = "A long digest. " * 6000
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = {**run(text=full[:1000]), "output_truncated": True}
    env["cardea"].outputs["task-1"] = {"text": full, "turns": 9, "cost_usd": 1.3, "attempts": 1, "calls": []}
    assert env["poller"].poll() == {"task-1": "live:succeeded"}
    assert [r.response for r in env["store"].recent_results("sam", "scout")] == [full]
    assert dict(events_of(env["history"]))["cardea_run"]["turns"] == 9


def test_truncated_shadow_output_is_stored_whole(env):
    rid = env["routine"].id
    full = "A long shadow digest. " * 5000
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")], shadow=True)]
    env["cardea"].runs["task-1"] = {**run(text="trimmed", calls=[call("mail.preview", op_id="call-0001")]), "output_truncated": True}
    env["cardea"].outputs["task-1"] = {"text": full, "turns": 3, "cost_usd": 0.4, "attempts": 1, "calls": []}
    assert env["poller"].poll() == {"task-1": "shadow:succeeded"}
    assert env["ledger"].shadow_runs(rid)[0]["output_text"] == full
