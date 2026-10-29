import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from telos_cardea import memory_reconcile
from telos_cardea._cardea import OperationContext, OperationRejected
from telos_cardea.config import Config

from conftest import EFFECT_KEY, assert_input, assert_output, load_descriptor

MakeContext = Callable[..., OperationContext]
BASE = "http://recordatio.test:8787/v1/users/owner/memory-reconcile"
TOKEN = "u1~u2~-~0~" + "0" * 32
VERDICT = {"relation": "duplicate_subsumed", "keep": "A", "drop": "B",
           "facts": [{"dropped_fact": "lives in Springfield", "kept_span": "lives in Springfield", "status": "same"}],
           "evidence": "B repeats A"}
UNIT = {"id": "u1", "scope": "owner/_user", "type": "semantic", "content": "Owner lives in Springfield",
        "valid_from": "2026-09-01T09:00:00+00:00", "asserted_at": "2026-09-01T09:00:00+00:00", "valid_until": None}


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def reconcile_config(config: Config) -> Config:
    return Config(**{**config.__dict__, "recordatio_reconcile_key": "reconcile-key", "reconcile_batch_size": 5})


def test_run_id_is_stable_per_slot(make_context: MakeContext) -> None:
    a = memory_reconcile.run_id(make_context(idempotency_context="slot-1"))
    assert a == memory_reconcile.run_id(make_context(idempotency_context="slot-1"))
    assert a != memory_reconcile.run_id(make_context(idempotency_context="slot-2"))
    assert a != memory_reconcile.run_id(make_context(namespace="telos-other", idempotency_context="slot-1"))
    assert a.startswith("cardea-")


async def test_candidates_uses_scope_key_and_caps_batch(reconcile_config: Config, make_context: MakeContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        pair = {"pair_token": TOKEN, "kind": "candidate", "signal": "fts", "op_id": None, "may_drop_only": None,
                "a": UNIT, "b": {**UNIT, "id": "u2"}}
        return httpx.Response(200, json={"pairs": [pair], "budget_left": 3, "more": False, "finished": False})

    context = make_context(idempotency_context="slot-1")
    assert_input("memory.reconcile_candidates", {"limit": 25})
    out = await memory_reconcile.candidates({"limit": 25}, context, config=reconcile_config, client=mock_client(handler))

    assert_output("memory.reconcile_candidates", out)
    request = seen[0]
    assert str(request.url).startswith(f"{BASE}/candidates")
    assert request.url.params["limit"] == "5"  # capped at the configured batch size
    assert request.url.params["run_id"] == memory_reconcile.run_id(context)
    assert request.headers["authorization"] == "Bearer reconcile-key"
    assert out["pairs"][0]["pair_token"] == TOKEN


async def test_apply_sends_verdict_with_idempotency_key(reconcile_config: Config, make_context: MakeContext) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append({"url": str(request.url), "key": request.headers.get("idempotency-key"), **json.loads(request.content)})
        return httpx.Response(200, json={"pair_token": TOKEN, "outcome": "deleted", "reason": "verified", "replayed": False,
                                         "kept_unit_id": "u1", "dropped_unit_id": "u2", "relation": "duplicate_subsumed"})

    arguments = {"pair_token": TOKEN, "verdict": VERDICT}
    assert_input("memory.reconcile_apply", arguments)
    out = await memory_reconcile.apply(arguments, make_context(), config=reconcile_config, client=mock_client(handler))

    assert_output("memory.reconcile_apply", out)
    assert out == {"outcome": "deleted", "reason": "verified", "dry_run": False, "replayed": False,
                   "kept_unit_id": "u1", "dropped_unit_id": "u2"}
    assert bodies[0]["url"] == f"{BASE}/verdicts"
    assert bodies[0]["key"] == EFFECT_KEY and bodies[0]["dry_run"] is False and bodies[0]["verdict"] == VERDICT


async def test_apply_preview_is_a_dry_run(reconcile_config: Config, make_context: MakeContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["dry_run"] is True and "idempotency-key" not in request.headers
        return httpx.Response(200, json={"pair_token": TOKEN, "outcome": "would_delete", "reason": "verified", "replayed": False})

    out = await memory_reconcile.apply_preview({"pair_token": TOKEN, "verdict": VERDICT}, make_context(),
                                               config=reconcile_config, client=mock_client(handler))
    assert_output("memory.reconcile_apply_preview", out)
    assert out["outcome"] == "would_delete" and out["dry_run"] is True


@pytest.mark.parametrize("status", [401, 403, 409, 422])
async def test_refusals_are_rejected(reconcile_config: Config, make_context: MakeContext, status: int) -> None:
    client = mock_client(lambda _r: httpx.Response(status, json={"error": "no", "code": "invalid_request"}))
    with pytest.raises(OperationRejected):
        await memory_reconcile.apply({"pair_token": TOKEN, "verdict": VERDICT}, make_context(), config=reconcile_config, client=client)


async def test_server_error_propagates(reconcile_config: Config, make_context: MakeContext) -> None:
    client = mock_client(lambda _r: httpx.Response(502))
    with pytest.raises(httpx.HTTPStatusError):
        await memory_reconcile.apply({"pair_token": TOKEN, "verdict": VERDICT}, make_context(), config=reconcile_config, client=client)


async def test_unmapped_scope_is_rejected(reconcile_config: Config, make_context: MakeContext) -> None:
    with pytest.raises(OperationRejected):
        await memory_reconcile.candidates({}, make_context(namespace="telos-stranger"), config=reconcile_config,
                                          client=mock_client(lambda _r: httpx.Response(200, json={})))


async def test_apply_reconciler(reconcile_config: Config, make_context: MakeContext) -> None:
    found = mock_client(lambda r: httpx.Response(200, json={"pair_token": TOKEN, "outcome": "kept", "reason": "compatible"}))
    rec = await memory_reconcile.reconcile_apply({"pair_token": TOKEN}, make_context(), config=reconcile_config, client=found)
    assert rec.applied and rec.output is not None and rec.output["outcome"] == "kept" and rec.output["replayed"] is True
    assert_output("memory.reconcile_apply", rec.output)

    missing = mock_client(lambda r: httpx.Response(404, json={"code": "verdict_not_found"}))
    rec = await memory_reconcile.reconcile_apply({"pair_token": TOKEN}, make_context(), config=reconcile_config, client=missing)
    assert not rec.applied

    odd = mock_client(lambda r: httpx.Response(404, json={"detail": "Not Found"}))
    with pytest.raises(RuntimeError):
        await memory_reconcile.reconcile_apply({"pair_token": TOKEN}, make_context(), config=reconcile_config, client=odd)


async def test_finish_and_reconciler(reconcile_config: Config, make_context: MakeContext) -> None:
    summary = {"user_id": "owner", "run_id": "x", "deleted": 2, "by_relation": {"supersedes": 2}, "by_source": {"judge": 2},
               "verdicts": {"deleted": 2}, "kept_reasons": {}, "ops_applied": 1, "ops_dismissed": 0, "ops_pending": 0,
               "legacy_unclassified_kept": 0, "open_pairs": 0, "snapshot": "/private/path", "vacuumed": True, "replayed": False}
    context = make_context(idempotency_context="slot-1")
    run = memory_reconcile.run_id(context)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE}/runs/{run}/finish"
        return httpx.Response(200, json=summary)

    out = await memory_reconcile.finish({}, context, config=reconcile_config, client=mock_client(handler))
    assert_output("memory.reconcile_finish", out)
    assert out["deleted"] == 2 and "snapshot" not in out  # host paths stay out of the receipt

    done = mock_client(lambda r: httpx.Response(200, json={"finished_at": "2026-09-02T01:00:00+00:00", "summary": summary}))
    rec = await memory_reconcile.reconcile_finish({}, context, config=reconcile_config, client=done)
    assert rec.applied and rec.output is not None
    assert_output("memory.reconcile_finish", rec.output)
    unfinished = mock_client(lambda r: httpx.Response(200, json={"finished_at": None, "summary": None}))
    assert not (await memory_reconcile.reconcile_finish({}, context, config=reconcile_config, client=unfinished)).applied
    never = mock_client(lambda r: httpx.Response(404, json={"code": "run_not_found"}))
    assert not (await memory_reconcile.reconcile_finish({}, context, config=reconcile_config, client=never)).applied


async def test_finish_preview(reconcile_config: Config, make_context: MakeContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["dry_run"] == "1"
        return httpx.Response(200, json={"would_delete": [{"unit_id": "u3"}], "ops": [{"op_id": 1}], "ops_pending": 2, "open_pairs": 0})

    out = await memory_reconcile.finish_preview({}, make_context(), config=reconcile_config, client=mock_client(handler))
    assert_output("memory.reconcile_finish_preview", out)
    assert out["would_delete"] == 1 and out["ops_settled"] == 1


def test_apply_scope_is_one_effect_per_pair_token() -> None:
    binding = load_descriptor("memory.reconcile_apply").cardea
    assert binding.idempotency_scope == ("/pair_token",)
    assert binding.reconciler == "memory.reconcile_apply.reconcile"
    finish = load_descriptor("memory.reconcile_finish").cardea
    assert finish.idempotency_scope == () and finish.reconciler == "memory.reconcile_finish.reconcile"


def test_previews_share_inputs() -> None:
    assert load_descriptor("memory.reconcile_apply_preview").input_schema == load_descriptor("memory.reconcile_apply").input_schema


def test_verdict_schema_refuses_memory_text() -> None:
    import _cardea_operations as vendored
    from conftest import CARDEA  # noqa: F401  (descriptor validation uses the same module)

    descriptor = load_descriptor("memory.reconcile_apply")
    assert not vendored.valid_arguments(descriptor, {"pair_token": TOKEN, "verdict": {**VERDICT, "new_content": "rewritten"}})
    assert not vendored.valid_arguments(descriptor, {"pair_token": TOKEN, "verdict": VERDICT, "content": "x"})
