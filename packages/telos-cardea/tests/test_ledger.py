import ast
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from telos_cardea import ledger, routine
from telos_cardea._cardea import OperationContext, OperationRejected
from telos_cardea.config import Config

import _cardea_operations as vendored
from conftest import AGENT, EFFECT_KEY, PACKAGE_ROOT, TOKEN, assert_input, assert_output, load_descriptor

MakeContext = Callable[..., OperationContext]
BASE = "http://telos.test:7445/internal/cardea/v1"
CRITERIA = ("budget", "postcode", "bedrooms", "lounge", "ceilings", "flooring", "building", "size", "floor", "availability")
ASSESSMENT: dict[str, Any] = {
    "url": "https://www.example.com/properties/1001",
    "address": "Flat 2, 1 Example Road, London",
    "disposition": "needs_verification",
    "criteria": {
        "budget": {"status": "pass", "evidence": "£2,000 pcm", "source_url": "https://www.example.com/properties/1001",
                   "observed_at": "2026-10-19T08:00:00+01:00"},
        "ceilings": {"status": "unknown", "evidence": "Ceiling height not stated in the listing."},
    },
    "price_pcm": 2000,
    "reason": "Budget fits; ceilings unverified.",
    "next_action": "Ask the agent for ceiling height.",
}


class Recorder:
    """httpx mock transport handler that records requests and answers from a route table."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response | Callable[[httpx.Request], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes[(request.method, request.url.path)]
        return answer(request) if callable(answer) else answer

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


ROUTINE_CONTEXT = {
    "invocation": {"reporting_date": "2026-10-20", "timezone": "Europe/London", "slot_time": "2026-10-20T08:00:00+01:00"},
    "delivery_history": "No emails sent in the last 7 days.",
    "ledger_summary": "3 open leads.",
    "reserved_recall": "",
}


# ── headers and scope come from the context only ───────────────────────


async def test_scope_headers_come_from_context(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("GET", "/internal/cardea/v1/routine-context"): httpx.Response(200, json=ROUTINE_CONTEXT)})
    context = make_context(slot_time="2026-10-20T08:00:00+01:00")
    output = await routine.routine_context({}, context, config=config, client=recorder.client())
    assert output == ROUTINE_CONTEXT
    assert_output("telos.routine_context", output)
    request = recorder.requests[0]
    assert str(request.url) == f"{BASE}/routine-context"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-Cardea-Namespace"] == "telos-owner"
    assert request.headers["X-Cardea-Agent"] == AGENT
    assert request.headers["X-Cardea-Slot-Time"] == "2026-10-20T08:00:00+01:00"


async def test_arguments_never_set_scope_headers(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("GET", "/internal/cardea/v1/ledger"): httpx.Response(200, json={"items": [], "next_offset": None})})
    # Not valid per the schema, but even a forged argument must not reach the scope headers.
    await ledger.list_({"limit": 5, "namespace": "telos-other", "agent": "telos-other"}, make_context(), config=config,
                       client=recorder.client())
    request = recorder.requests[0]
    assert request.headers["X-Cardea-Namespace"] == "telos-owner" and request.headers["X-Cardea-Agent"] == AGENT
    assert "X-Cardea-Slot-Time" not in request.headers
    assert dict(request.url.params) == {"offset": "0", "limit": "5"}


async def test_token_file_is_reread_each_call(config: Config, make_context: MakeContext, token_file: Path) -> None:
    recorder = Recorder({("GET", "/internal/cardea/v1/routine-context"): httpx.Response(200, json=ROUTINE_CONTEXT)})
    await routine.routine_context({}, make_context(), config=config, client=recorder.client())
    token_file.write_text("rotated.token.value")
    await routine.routine_context({}, make_context(), config=config, client=recorder.client())
    assert [r.headers["Authorization"] for r in recorder.requests] == [f"Bearer {TOKEN}", "Bearer rotated.token.value"]


async def test_missing_agent_id_is_rejected(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({})
    with pytest.raises(OperationRejected, match="agent_id"):
        await routine.routine_context({}, make_context(agent_id=None), config=config, client=recorder.client())
    assert recorder.requests == []


@pytest.mark.parametrize("change, match", [
    ({"telos_url": ""}, "TELOS_INTERNAL_URL"),
    ({"telos_token_file": ""}, "TELOS_INTERNAL_TOKEN_FILE"),
    ({"telos_token_file": "/nonexistent/telos-internal.jwt"}, "unreadable"),
])
async def test_missing_config_is_rejected(config: Config, make_context: MakeContext, change: dict[str, Any], match: str) -> None:
    recorder = Recorder({})
    with pytest.raises(OperationRejected, match=match):
        await ledger.list_({}, make_context(), config=replace(config, **change), client=recorder.client())
    assert recorder.requests == []


async def test_empty_token_file_is_rejected(config: Config, make_context: MakeContext, token_file: Path) -> None:
    token_file.write_text("\n")
    with pytest.raises(OperationRejected, match="empty"):
        await ledger.list_({}, make_context(), config=config, client=Recorder({}).client())


async def test_unmapped_scope_is_a_clear_rejection(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("GET", "/internal/cardea/v1/ledger"): httpx.Response(404, json={"detail": "not found"})})
    with pytest.raises(OperationRejected, match="no mirrored routine for namespace 'telos-owner' and agent 'telos-homey'"):
        await ledger.list_({}, make_context(), config=config, client=recorder.client())


@pytest.mark.parametrize("status", [401, 403])
async def test_refused_credential_is_rejected(config: Config, make_context: MakeContext, status: int) -> None:
    recorder = Recorder({("GET", "/internal/cardea/v1/ledger"): httpx.Response(status)})
    with pytest.raises(OperationRejected, match=str(status)):
        await ledger.list_({}, make_context(), config=config, client=recorder.client())


async def test_unreachable_listener_is_rejected(config: Config, make_context: MakeContext) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(OperationRejected, match="unreachable"):
        await ledger.list_({}, make_context(), config=config, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))


# ── reads ──────────────────────────────────────────────────────────────


async def test_list_pages_and_caps_limit(config: Config, make_context: MakeContext) -> None:
    page = {"items": [{"url": ASSESSMENT["url"], "disposition": "needs_verification"}], "next_offset": 50}
    recorder = Recorder({("GET", "/internal/cardea/v1/ledger"): httpx.Response(200, json=page)})
    assert_input("ledger.list", {"offset": 0, "limit": 50})
    output = await ledger.list_({"offset": 0, "limit": 500}, make_context(), config=config, client=recorder.client())
    assert output == page
    assert_output("ledger.list", output)
    assert recorder.requests[0].url.params["limit"] == "200"


async def test_history_passes_url(config: Config, make_context: MakeContext) -> None:
    body = {"history": [{"url": ASSESSMENT["url"], "recorded_at": "2026-10-19T08:00:00+00:00"}]}
    recorder = Recorder({("GET", "/internal/cardea/v1/ledger/history"): httpx.Response(200, json=body)})
    args = {"url": ASSESSMENT["url"]}
    assert_input("ledger.history", args)
    output = await ledger.history(args, make_context(), config=config, client=recorder.client())
    assert output == body
    assert_output("ledger.history", output)
    assert recorder.requests[0].url.params["url"] == ASSESSMENT["url"]


# ── ledger.record and its reconciler ───────────────────────────────────


async def test_record_sends_effect_key_as_idempotency_key(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("POST", "/internal/cardea/v1/ledger/assessments"):
                         httpx.Response(201, json={"recorded": True, "assessment_id": "as-1", "duplicate": False})})
    assert_input("ledger.record", ASSESSMENT)
    output = await ledger.record(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())
    assert output == {"recorded": True, "assessment_id": "as-1", "duplicate": False}
    assert_output("ledger.record", output)
    request = recorder.requests[0]
    assert request.headers["Idempotency-Key"] == EFFECT_KEY
    assert "dry_run" not in request.url.params
    assert json.loads(request.content) == ASSESSMENT


async def test_record_duplicate_returns_first_id(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("POST", "/internal/cardea/v1/ledger/assessments"):
                         httpx.Response(201, json={"recorded": True, "assessment_id": "as-1", "duplicate": True})})
    output = await ledger.record(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())
    assert output == {"recorded": True, "assessment_id": "as-1", "duplicate": True}


async def test_record_invalid_assessment_is_rejected(config: Config, make_context: MakeContext) -> None:
    detail = {"detail": "Rejected requires an evidenced failing criterion; missing evidence is unresolved"}
    recorder = Recorder({("POST", "/internal/cardea/v1/ledger/assessments"): httpx.Response(422, json=detail)})
    with pytest.raises(OperationRejected, match="evidenced failing criterion"):
        await ledger.record({**ASSESSMENT, "disposition": "rejected"}, make_context(), config=config, client=recorder.client())


@pytest.mark.parametrize("failure", ["timeout", "500"])
async def test_record_uncertain_failures_propagate(config: Config, make_context: MakeContext, failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPError) as caught:
        await ledger.record(dict(ASSESSMENT), make_context(), config=config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not isinstance(caught.value, OperationRejected)


async def test_reconcile_found_is_applied(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("GET", f"/internal/cardea/v1/ledger/assessments/by-key/{EFFECT_KEY}"):
                         httpx.Response(200, json={"assessment_id": "as-1"})})
    result = await ledger.reconcile_record(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())
    assert result.applied is True
    assert result.output == {"recorded": True, "assessment_id": "as-1", "duplicate": True}
    assert_output("ledger.record", result.output)
    request = recorder.requests[0]
    assert request.headers["X-Cardea-Agent"] == AGENT and request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_reconcile_missing_is_not_applied(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("GET", f"/internal/cardea/v1/ledger/assessments/by-key/{EFFECT_KEY}"): httpx.Response(404)})
    result = await ledger.reconcile_record(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())
    assert result.applied is False and result.output is None


async def test_reconcile_server_error_raises(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("GET", f"/internal/cardea/v1/ledger/assessments/by-key/{EFFECT_KEY}"): httpx.Response(503)})
    with pytest.raises(httpx.HTTPStatusError):
        await ledger.reconcile_record(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())


# ── ledger.record_preview (shadow mode) ────────────────────────────────


async def test_preview_is_a_dry_run(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("POST", "/internal/cardea/v1/ledger/assessments"): httpx.Response(200, json={"recorded": False, "valid": True})})
    assert_input("ledger.record_preview", ASSESSMENT)
    output = await ledger.record_preview(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())
    assert output == {"preview": True, "recorded": False, "valid": True, "url": ASSESSMENT["url"]}
    assert_output("ledger.record_preview", output)
    assert recorder.requests[0].url.params["dry_run"] == "1"


async def test_preview_invalid_fails_like_live(config: Config, make_context: MakeContext) -> None:
    recorder = Recorder({("POST", "/internal/cardea/v1/ledger/assessments"): httpx.Response(422, json={"detail": "bad"})})
    with pytest.raises(OperationRejected):
        await ledger.record_preview(dict(ASSESSMENT), make_context(), config=config, client=recorder.client())


# ── the descriptor schema mirrors Telos's PropertyAssessmentInput ──────


def test_schema_rejects_unknown_criterion_and_extra_fields() -> None:
    descriptor = load_descriptor("ledger.record")
    assert not vendored.valid_arguments(descriptor, {**ASSESSMENT, "criteria": {"garden": {"status": "unknown", "evidence": "n/a"}}})
    assert not vendored.valid_arguments(descriptor, {**ASSESSMENT, "user_id": "someone"})
    assert not vendored.valid_arguments(descriptor, {k: v for k, v in ASSESSMENT.items() if k != "reason"})


def _strip_titles(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_titles(v) for k, v in value.items() if k != "title"}
    if isinstance(value, list):
        return [_strip_titles(v) for v in value]
    return value


def test_assessment_schema_matches_telos_source() -> None:
    """Rebuild Telos's models from its source (without importing ``telos``) and compare JSON schemas."""
    source = PACKAGE_ROOT.parents[1] / "telos" / "domain" / "tools" / "property_candidates.py"
    if not source.exists():
        pytest.skip("not inside the telos repository")
    wanted = {"Criterion", "REQUIRED_CRITERIA", "CriterionEvidence", "PropertyAssessmentInput"}
    tree = ast.parse(source.read_text())
    body: list[ast.stmt] = [node for node in tree.body
            if (isinstance(node, ast.Import | ast.ImportFrom) and not (getattr(node, "module", None) or "").startswith("telos"))
            or (isinstance(node, ast.ClassDef) and node.name in wanted)
            or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets))]
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"), namespace)
    expected = _strip_titles(namespace["PropertyAssessmentInput"].model_json_schema())
    for name in ("ledger.record", "ledger.record_preview"):
        actual = {k: v for k, v in load_descriptor(name).input_schema.items() if k != "$schema"}
        assert actual == expected, f"regenerate descriptors/{name}.json inputSchema from PropertyAssessmentInput"
    assert tuple(expected["properties"]["criteria"]["propertyNames"]["enum"]) == CRITERIA
