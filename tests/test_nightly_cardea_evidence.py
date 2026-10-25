"""Nightly evidence for Cardea-run routines: receipts' per-call outcomes reach the analyst.

A live Cardea run is ingested into the agent's history (see ``telos.cardea_ingest``); the nightly
analyst reads it back through ``get_turns``/``get_execution_events``. A failed ``browser.open``
receipt (e.g. an HTTP 410) must surface with its failure summary, so a recap that reports the
source as unavailable is checkable instead of looking fabricated.
"""

import json
from pathlib import Path
from typing import Any

from telos import cardea_ingest
from telos.cardea_ingest import call_failure, call_outcome
from telos.log import ConversationLog
from telos.services.nightly.prompts import AGENT_SYSTEM
from telos.services.nightly.synthesizer import SYNTH_SYSTEM
from telos.services.nightly.tools import ToolContext, build_agent_toolset
from tests.test_cardea_ingest import admitted, call, env, run, trigger  # noqa: F401  (env is a fixture)

GONE = "HTTP 410 opening https://listings.example.test/flat-1; access denied or unavailable, not empty results"


def browser_failure(op_id: str, summary: str = GONE) -> dict[str, Any]:
    return call("browser.open", "failed", op_id=op_id, failure="operation_rejected",
                failure_details={"reason": "rejected", "summary": summary})


def live_calls() -> list[dict[str, Any]]:
    return [
        call("listings.rightmove_search", op_id="op-01", deduplicated=True),
        browser_failure("op-02"),
        call("mail.send", op_id="op-03"),
    ]


class _NoRecordatio:
    async def aclose(self) -> None:
        return None


def _decode(response: dict[str, Any]) -> Any:
    return json.loads(response["content"][0]["text"])


async def _nightly_view(history: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """What the nightly analyst sees: the day's turn summaries and the first turn's events."""
    log = ConversationLog(history)
    turns = log.recent_turns(10)
    ctx = ToolContext(user_id="sam", slug="scout", day="2026-10-20", client=_NoRecordatio(),  # type: ignore[arg-type]
                      turns=turns, event_reader=log.execution_events.page)
    toolset = build_agent_toolset(ctx)
    summaries = _decode(await toolset.handlers["get_turns"]({"page": 0}))["turns"]
    events: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = _decode(await toolset.handlers["get_execution_events"](
            {"turn_id": turns[0].turn_id, "offset": offset, "limit": 20}))
        events += page["events"]
        offset += len(page["events"])
        if not page["events"] or offset >= page["total"]:
            return summaries, events


def test_call_failure_keeps_code_reason_and_bounded_summary():
    assert call_failure(call("web.search")) is None
    assert call_failure(browser_failure("op-1")) == {"code": "operation_rejected", "reason": "rejected", "summary": GONE}
    long = call_failure(browser_failure("op-1", summary="x" * 500))
    assert long is not None and len(long["summary"]) == cardea_ingest.SUMMARY_CHARS
    # Receipts written before failure reasons existed carry {} details: the code still shows.
    assert call_failure(call("browser.open", "failed", failure="operation_timeout", failure_details={})) == {
        "code": "operation_timeout"}
    assert call_failure(call("mail.send", "outcome_unknown", failure=None)) == {"code": "outcome_unknown"}
    assert call_outcome(call("web.search", deduplicated=True)) == {
        "operation": "web.search", "operation_id": "op-01", "state": "succeeded", "deduplicated": True}


async def test_live_run_receipts_reach_nightly_evidence(env):  # noqa: F811
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run(calls=live_calls(), text="Listing flat-1 is gone (HTTP 410).")

    assert env["poller"].poll() == {"task-1": "live:succeeded"}
    summaries, events = await _nightly_view(env["history"])

    assert summaries[0]["executed_by"] == "cardea" and summaries[0]["is_routine"] is True
    run_event = events[0]
    assert run_event["kind"] == "cardea_run"
    evidence = run_event["payload"]
    assert evidence["evidence"] == "cardea_receipts"
    assert evidence["state"] == "succeeded" and evidence["turns"] == 4 and evidence["cost_usd"] == 0.12
    assert evidence["calls_total"] == 3 and evidence["failed_calls"] == 1 and evidence["outcomes_omitted"] == 0
    assert evidence["outcomes"] == [
        {"operation": "listings.rightmove_search", "operation_id": "op-01", "state": "succeeded", "deduplicated": True},
        {"operation": "browser.open", "operation_id": "op-02", "state": "failed", "deduplicated": False,
         "failure": {"code": "operation_rejected", "reason": "rejected", "summary": GONE}},
        {"operation": "mail.send", "operation_id": "op-03", "state": "succeeded", "deduplicated": False},
    ]
    failed = next(e["payload"] for e in events if e["kind"] == "tool_result" and e["payload"]["call_id"] == "op-02")
    assert failed["is_error"] is True and failed["state"] == "failed"
    assert failed["failure"]["summary"] == GONE and failed["result"] == failed["failure"]
    ok = next(e["payload"] for e in events if e["kind"] == "tool_result" and e["payload"]["call_id"] == "op-01")
    assert ok["failure"] is None and ok["deduplicated"] is True


async def test_outcomes_are_capped_per_run(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cardea_ingest, "OUTCOMES_CAP", 2)
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-1")])]
    env["cardea"].runs["task-1"] = run(calls=[browser_failure(f"op-{i:02d}") for i in range(4)] + [call("mail.send", op_id="op-99")])

    env["poller"].poll()
    _, events = await _nightly_view(env["history"])

    evidence = events[0]["payload"]
    assert len(evidence["outcomes"]) == 2 and evidence["outcomes_omitted"] == 3
    assert evidence["calls_total"] == 5 and evidence["failed_calls"] == 4


async def test_shadow_run_stays_out_of_nightly_evidence_but_keeps_receipts(env, tmp_path):  # noqa: F811
    rid = env["routine"].id
    env["cardea"].trigger_list = [trigger(rid, [admitted("task-s")], shadow=True)]
    env["cardea"].runs["task-s"] = run(calls=[browser_failure("op-01"), call("mail.preview", op_id="op-02")])

    assert env["poller"].poll() == {"task-s": "shadow:succeeded"}

    # Shadow runs never reach the agent's history: the nightly reviews Telos's own run of the routine.
    assert not env["history"].exists()
    stored = env["ledger"].shadow_runs(rid)[0]["calls"]
    assert stored[0]["failure_details"]["summary"] == GONE


async def test_telos_run_turn_is_marked_as_telos(tmp_path):
    history = tmp_path / "sam" / "scout" / "history.db"
    history.parent.mkdir(parents=True)
    log = ConversationLog(history)
    log.append("Search listings in E8.", "Nothing new.", "sha", is_routine=True)

    summaries, _ = await _nightly_view(history)

    assert summaries[0]["executed_by"] == "telos"


def test_prompts_name_receipts_as_authoritative():
    assert "executed_by" in AGENT_SYSTEM and "AUTHORITATIVE" in AGENT_SYSTEM and "confabulated" in AGENT_SYSTEM
    assert "receipts" in SYNTH_SYSTEM and "Cardea" in SYNTH_SYSTEM
