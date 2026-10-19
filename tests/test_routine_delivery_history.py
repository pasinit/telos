import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.log import ConversationLog
from telos.services.routine_delivery_history import delivery_history, email_links
from telos.services.routine_execution import RoutineExecutor


def record_email(history, body, *, recipient="owner@example.com", error=False, confirmed=True, failed_turn=False):
    turn = history.append("daily", "Email sent", "prompt", is_routine=True)
    history.execution_events.append(turn, "tool_call", {
        "name": "send_email", "call_id": "send", "input": {"to": recipient, "subject": "Recap", "body": body},
    })
    if confirmed:
        history.execution_events.append(turn, "tool_result", {"call_id": "send", "is_error": error})
    if failed_turn:
        history.execution_events.append(turn, "turn_failed", {"reason": "truncated"})
    return turn


def test_only_confirmed_sends_for_recipient(tmp_path):
    history = ConversationLog(tmp_path / "history.db")
    record_email(history, "draft", confirmed=False)
    record_email(history, "failure", error=True)
    record_email(history, "other", recipient="other@example.com")
    record_email(history, "delivered before stream failure", failed_turn=True)
    rows = history.execution_events.recent_email_deliveries("OWNER@example.com")
    assert [r["body"] for r in rows] == ["delivered before stream failure"]


def test_scheduler_failure_notices_and_unknown_delivery_are_excluded(tmp_path):
    history = ConversationLog(tmp_path / "history.db")
    for status, failed in (("accepted_by_smtp", True), ("unknown", False), ("failed_or_unconfigured", False),
                           ("accepted_by_smtp", False)):
        turn = history.append("daily", "", "prompt", is_routine=True)
        if failed:
            history.execution_events.append(turn, "turn_failed", {"reason": "transport:ConnectError"})
        history.execution_events.append(turn, "email_delivery", {
            "to": "owner@example.com", "body": "recap", "status": status,
        })
    assert len(history.execution_events.recent_email_deliveries("owner@example.com")) == 1


def test_result_must_belong_to_same_turn_and_call(tmp_path):
    history = ConversationLog(tmp_path / "history.db")
    turn = record_email(history, "not confirmed", confirmed=False)
    other = history.append("other", "", "prompt")
    history.execution_events.append(other, "tool_result", {"call_id": "send", "is_error": False})
    history.execution_events.append(turn, "tool_result", {"call_id": "different", "is_error": False})
    assert history.execution_events.recent_email_deliveries("owner@example.com") == []


def test_markdown_html_bare_links_and_tracking_variants():
    body = '''[Flat](https://www.example.com/flat/123/?utm_source=email#photos)
<a href="https://example.com/flat/123/">Same flat</a>
https://example.com/listing?id=7&utm_medium=email
[Other flat](https://example.com/listing?id=8)
[Unsafe](javascript:alert(1))
[Credentials](https://name:password@example.com/flat/9)
'''
    assert email_links(body) == ["https://example.com/flat/123", "https://example.com/listing?id=7",
                                 "https://example.com/listing?id=8"]


def test_overlapping_emails_preserve_dates_counts_and_latest_body(tmp_path):
    history = ConversationLog(tmp_path / "history.db")
    record_email(history, "Old flat https://example.com/flat/1/")
    record_email(history, "Unchanged https://example.com/flat/1?utm_source=daily\nNew https://example.com/flat/2")
    evidence = delivery_history(history.execution_events, "owner@example.com")
    assert evidence["deliveries_checked"] == 2
    assert evidence["latest_email"]["body"].startswith("Unchanged")
    records = {r["url"]: r for r in evidence["previously_mentioned_links"]}
    repeat = records["https://example.com/flat/1"]
    assert repeat["email_count"] == 2
    assert repeat["first_mentioned_in_window"] < repeat["last_mentioned"]
    assert records["https://example.com/flat/2"]["email_count"] == 1
    assert "first_seen" not in repeat


def test_bounded_context_discloses_overflow_and_body_truncation(tmp_path):
    history = ConversationLog(tmp_path / "history.db")
    for i in range(32):
        record_email(history, "text " * 1000 + f"https://example.com/flat/{i}")
    evidence = delivery_history(history.execution_events, "owner@example.com", max_chars=2048)
    assert evidence["deliveries_checked"] == 30
    assert evidence["older_deliveries_omitted"]
    assert evidence["links_truncated"]
    assert evidence["latest_email"]["body_truncated"]
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 2048


@pytest.mark.asyncio
async def test_executor_injects_same_agent_delivery_evidence(tmp_path):
    history = ConversationLog(tmp_path / "owner/homey/history.db")
    record_email(history, "Already emailed https://example.com/flat/1")
    other = ConversationLog(tmp_path / "other/homey/history.db")
    record_email(other, "PRIVATE https://example.com/flat/secret")
    messages = []

    async def stream(message, ctx):
        messages.append(message)
        yield "text", "No changes"

    runtime = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(astream=stream)),
                              make_context=MagicMock())
    executor = RoutineExecutor(runtime, MagicMock(), lambda _: "owner@example.com", lambda *_: "Homey", "",
                               storage_root=tmp_path)
    assert await executor.execute("owner", "homey", "daily") == "No changes"
    assert "https://example.com/flat/1" in messages[0]
    assert "PRIVATE" not in messages[0]
    assert "not necessarily recommended" in messages[0]


@pytest.mark.asyncio
async def test_executor_discloses_history_read_failure(tmp_path, monkeypatch):
    ConversationLog(tmp_path / "owner/homey/history.db")
    messages = []

    def fail(*_):
        raise RuntimeError("unavailable")

    async def stream(message, ctx):
        messages.append(message)
        yield "text", "Partial continuity"

    monkeypatch.setattr("telos.services.routine_execution.delivery_history", fail)
    runtime = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(astream=stream)), make_context=MagicMock())
    executor = RoutineExecutor(runtime, MagicMock(), lambda _: "owner@example.com", lambda *_: "Homey", "",
                               storage_root=tmp_path)
    await executor.execute("owner", "homey", "daily")
    assert "Prior email history retrieval failed" in messages[0]


def test_homey_delta_policy_preserves_manual_verification():
    # The operational policy is intentionally separate from obsolete full templates.
    policy = Path("docs/homey/daily_updates.md").read_text()
    assert "Missing evidence remains acceptable" in policy
    assert "changes-only" in policy
    assert "Do not expire, reject" in policy
