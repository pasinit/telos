import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.log import ConversationLog
from telos.services.routine_execution import RoutineExecutor
from telos.services.routine_recall import RoutineRecall


class Client:
    def __init__(self, results):
        self.memory_search = AsyncMock(side_effect=results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def test_priority_recall_scoped_deduplicated_bounded_and_failures_visible():
    unit = {"id": "a", "content": "Applied to Example on 1 May", "asserted_at": "2026-05-01", "valid_from": "2026-05-01"}
    client = Client([{"results": [unit, {**unit, "id": "duplicate"}]}, RuntimeError("retrieval offline")])
    recall = RoutineRecall(lambda: client, {"owner/jobber": ["applications", "updates"]})
    assert await recall("other", "jobber") is None
    result = await recall("owner", "jobber")
    assert len(result["records"]) == 1 and result["records"][0]["asserted_at"] == "2026-05-01"
    assert "retrieval offline" in result["errors"][0]["error"]
    assert all(c.kwargs["slug"] == "jobber" and c.kwargs["as_of"] == result["as_of"] for c in client.memory_search.call_args_list)
    client = Client([{"results": [{**unit, "content": "x" * 10000}]}])
    result = await RoutineRecall(lambda: client, {"owner/jobber": ["applications"]}, max_chars=1000)("owner", "jobber")
    assert result["truncated"] and len(json.dumps(result)) < 1000


async def test_priority_recall_reserves_later_queries_and_reports_counts():
    early = [{"id": str(i), "content": str(i) + "x" * 200} for i in range(10)]
    status = {"id": "status", "content": "Application rejected", "asserted_at": "2026-09-11"}
    deadline = {"id": "deadline", "content": "Deadline September 15", "valid_until": "2026-09-15"}
    client = Client([{"results": early}, {"results": [status]}, {"results": [deadline]}])
    result = await RoutineRecall(lambda: client, {"owner/jobber": ["history", "status", "deadlines"]},
                                 max_chars=3000)("owner", "jobber")
    ids = {record["id"] for record in result["records"]}
    assert {"status", "deadline"} <= ids
    assert any(record["id"].isdigit() for record in result["records"])
    assert len(json.dumps(result)) <= 3000
    assert result["truncated"]
    assert result["query_counts"][1:] == [
        {"query": "status", "retrieved": 1, "included": 1, "omitted": 0},
        {"query": "deadlines", "retrieved": 1, "included": 1, "omitted": 0},
    ]
    counts = result["query_counts"][0]
    assert counts["retrieved"] == 10
    assert counts["included"] + counts["omitted"] == 10
    assert counts["included"] > 0 and counts["omitted"] > 0


async def test_priority_recall_surfaces_status_event_beyond_old_k_window_under_swarm():
    # A broad status probe returns a swarm of distinct-date "no response"
    # summaries (not collapsed by the exact-dup dedup) with the one interview
    # status event ranked last, beyond the old hard-coded k=10 window.
    swarm = [
        {"id": f"s{i}", "content": f"Submitted application; no response as of 2026-06-{i:02d}" + " " * 40,
         "asserted_at": f"2026-06-{i:02d}"}
        for i in range(1, 14)
    ]
    interview = {"id": "interview",
                 "content": "Interview scheduled with Christian Aid for 8-9 June 2026",
                 "asserted_at": "2026-06-08"}
    client = Client([{"results": swarm + [interview]}])
    result = await RoutineRecall(
        lambda: client, {"owner/jobber": ["Submitted applications subsequent status updates interviews rejections"]},
        max_chars=2200,
    )("owner", "jobber")
    ids = {record["id"] for record in result["records"]}
    # Old behaviour: interview (rank 14) is sliced away by k=10 and never
    # retrieved; even if present, the swarm would consume the char budget.
    assert "interview" in ids


async def test_priority_recall_runs_all_configured_queries_beyond_three():
    # A routine's reference queries are operator config. The old hard-coded
    # [:3] cap silently dropped every query beyond the third, so profile/
    # competency probes (configured as later queries) never ran and that
    # evidence never reached the briefing. All configured queries must run.
    queries = ["applications", "updates", "status", "parliamentary advocacy engagement",
               "supervising staff implementing partners budget leadership"]
    results = [{"results": [{"id": f"q{i}", "content": f"evidence for {q}", "asserted_at": "2026-10-01"}]}
               for i, q in enumerate(queries)]
    client = Client(results)
    result = await RoutineRecall(lambda: client, {"owner/jobber": queries})("owner", "jobber")
    assert client.memory_search.call_count == len(queries)
    assert result["queries"] == queries
    assert {record["id"] for record in result["records"]} == {f"q{i}" for i in range(len(queries))}


async def test_priority_recall_respects_optional_max_queries_cost_ceiling():
    queries = ["a", "b", "c", "d", "e"]
    client = Client([{"results": [{"id": q, "content": q}]} for q in queries])
    result = await RoutineRecall(lambda: client, {"owner/jobber": queries}, max_queries=2)("owner", "jobber")
    assert client.memory_search.call_count == 2
    assert result["queries"] == ["a", "b"]


async def test_priority_recall_floats_configured_status_terms_per_routine():
    # Per-routine status terms re-prioritise this routine's own evidence to the
    # front of the candidate list instead of only the global job-status terms.
    noise = {"id": "noise", "content": "weekly status note no change", "asserted_at": "2026-06-01"}
    competency = {"id": "comp", "content": "Supervising staff and implementing partners with budget leadership",
                  "asserted_at": "2026-06-20"}

    def run(**kwargs):
        client = Client([{"results": [noise, competency]}])
        return RoutineRecall(lambda: client, {"owner/jobber": ["competency evidence"]}, **kwargs)("owner", "jobber")

    # Default terms: neither record is a job-status event, rank order preserved.
    default = await run()
    assert [record["id"] for record in default["records"]] == ["noise", "comp"]
    # Configured competency term floats the matching record to the front.
    configured = await run(status_terms={"owner/jobber": ["Supervising staff"]})
    assert [record["id"] for record in configured["records"]] == ["comp", "noise"]


async def test_priority_recall_keeps_dated_observations_and_conflicts():
    old = {"id": "old", "content": "Application pending", "asserted_at": "2026-09-01"}
    new = {**old, "id": "new", "asserted_at": "2026-09-11"}
    conflict = {**new, "id": "conflict", "content": "Application rejected"}
    client = Client([{"results": [old, {**old, "id": "duplicate"}]}, {"results": [new, conflict]}])
    result = await RoutineRecall(lambda: client, {"owner/jobber": ["history", "status"]})("owner", "jobber")
    assert {record["id"] for record in result["records"]} == {"old", "new", "conflict"}
    assert result["query_counts"][0] == {"query": "history", "retrieved": 2, "included": 1, "omitted": 1}
    assert not result["truncated"]


async def test_routine_records_priority_context_and_complete_email_body(tmp_path):
    history = ConversationLog(tmp_path / "u" / "jobber" / "history.db")
    class Agent:
        async def astream(self, message, ctx):
            assert "Historical application status" in message
            assert "Applied to Example" in message
            turn = history.append(message, "answer", "sha", is_routine=True)
            yield "turn_id", {"turn_id": turn}
            yield "text", "the complete recap"
    runtime = SimpleNamespace(get=AsyncMock(return_value=Agent()), make_context=lambda *a, **kw: None)
    email = MagicMock()
    email.send_routine_notification.return_value = True
    executor = RoutineExecutor(runtime, email, lambda u: "owner@example.com", lambda u, s: "Jobber", "https://telos.example",
                               storage_root=tmp_path, recall_fn=AsyncMock(return_value={"records": [{"content": "Applied to Example"}]}))
    routine = SimpleNamespace(id="r", name="Brief", notify_email=True)
    assert await executor.execute_with_email("u", "jobber", "run", routine) == "the complete recap"
    events = history.execution_events.page(history.last_turn_id(), limit=20)["events"]
    assert [e["kind"] for e in events] == ["routine_invocation", "required_recall", "turn_completed", "email_delivery"]
    assert set(events[0]["payload"]) == {"reporting_date", "timezone", "invoked_at"}
    assert "the complete recap" in events[-1]["payload"]["body"]
    assert events[-1]["payload"]["status"] == "accepted_by_smtp"


@pytest.mark.parametrize("recipient,failed,notify", [("owner@example.com", False, False), ("other@example.com", False, True), ("owner@example.com", True, True)])
async def test_routine_deduplicates_only_confirmed_email_to_recipient(tmp_path, recipient, failed, notify):
    history = ConversationLog(tmp_path / "u" / "homey" / "history.db")
    class Agent:
        async def astream(self, message, ctx):
            turn = history.append(message, "answer", "sha", is_routine=True)
            yield "turn_id", {"turn_id": turn}
            history.execution_events.append(turn, "tool_call", {"call_id": "email1", "name": "send_email", "input": {"to": recipient, "body": "actual recap"}})
            history.execution_events.append(turn, "tool_result", {"call_id": "email1", "name": "send_email", "is_error": failed, "result": "Email result"})
            yield "text", "Email sent."
    runtime = SimpleNamespace(get=AsyncMock(return_value=Agent()), make_context=lambda *a, **kw: None)
    email = MagicMock()
    executor = RoutineExecutor(runtime, email, lambda u: "owner@example.com", lambda u, s: "Homey", "", storage_root=tmp_path)
    await executor.execute_with_email("u", "homey", "run", SimpleNamespace(id="r", name="Brief", notify_email=True))
    assert email.send_routine_notification.called is notify


@pytest.mark.parametrize("failure", ["empty_output", "exception", "cancelled", "stream_error"])
async def test_routine_records_one_terminal_failure_per_turn(tmp_path, failure):
    import asyncio

    from telos.services.routine_execution import RoutineStreamError

    history = ConversationLog(tmp_path / "u" / "homey" / "history.db")
    interrupted = RuntimeError("iterator failed")
    ready = asyncio.Event()

    class Agent:
        async def astream(self, message, ctx):
            turn = history.append(message, "", "sha", is_routine=True)
            yield "turn_id", {"turn_id": turn}
            yield "text", "  " if failure == "empty_output" else "partial"
            if failure == "exception":
                raise interrupted
            if failure == "cancelled":
                ready.set()
                await asyncio.Event().wait()
            if failure == "stream_error":
                yield "stream_error", {"reason": "truncated", "detail": "lost stream"}

    runtime = SimpleNamespace(get=AsyncMock(return_value=Agent()), make_context=lambda *a, **kw: None)
    executor = RoutineExecutor(runtime, MagicMock(), lambda u: None, lambda u, s: "Homey", "", storage_root=tmp_path)
    task = asyncio.create_task(executor.execute("u", "homey", "run"))
    if failure == "cancelled":
        await ready.wait()
        task.cancel()
    expected = asyncio.CancelledError if failure == "cancelled" else RuntimeError
    with pytest.raises(expected) as caught:
        await task
    if failure == "exception":
        assert caught.value is interrupted
    elif failure == "cancelled":
        assert task.cancelled()
    else:
        assert isinstance(caught.value, RoutineStreamError)
    events = history.execution_events.list(history.last_turn_id())
    assert [event["kind"] for event in events] == ["turn_failed"]
    reason = {"exception": "exception:RuntimeError", "stream_error": "truncated"}.get(failure, failure)
    assert events[0]["payload"]["reason"] == reason
    assert events[0]["payload"]["response_chars"] == (2 if failure == "empty_output" else 7)


@pytest.mark.parametrize("failure", ["empty_output", "truncated"])
async def test_routine_retry_records_failed_attempt_then_completed_turn(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("telos.services.routine_execution.ROUTINE_STREAM_RETRY_BACKOFF_S", 0)
    history = ConversationLog(tmp_path / "u" / "homey" / "history.db")
    turns = []

    class Agent:
        async def astream(self, message, ctx):
            turn = history.append(message, "", "sha", is_routine=True)
            turns.append(turn)
            yield "turn_id", {"turn_id": turn}
            if len(turns) == 1:
                if failure == "truncated":
                    yield "text", "partial"
                    yield "stream_error", {"reason": "truncated"}
            else:
                yield "text", "complete recap"

    runtime = SimpleNamespace(get=AsyncMock(return_value=Agent()), make_context=lambda *a, **kw: None)
    email = MagicMock()
    executor = RoutineExecutor(runtime, email, lambda u: "owner@example.com", lambda u, s: "Homey", "", storage_root=tmp_path)
    routine = SimpleNamespace(id="r", name="Brief", notify_email=True)
    assert await executor.execute_with_email("u", "homey", "run", routine) == "complete recap"
    assert len(turns) == 2 and turns[0] != turns[1]
    first = history.execution_events.list(turns[0])
    second = history.execution_events.list(turns[1])
    assert [e["kind"] for e in first] == ["routine_invocation", "turn_failed"]
    assert first[-1]["payload"] == {"reason": failure, "response_chars": 7 if failure == "truncated" else 0}
    assert [e["kind"] for e in second] == ["routine_invocation", "turn_completed", "email_delivery"]
    email.send_routine_notification.assert_called_once()
    assert "FAILED" not in email.send_routine_notification.call_args.kwargs["subject"]
