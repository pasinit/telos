"""Internal Cardea API: JWT-SVID auth, agent scoping, routine context and the property ledger."""

import asyncio
import dataclasses
import json
import os
import socket
import tempfile
import time
from concurrent import futures
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import grpc
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from telos.api.internal_cardea import DEFAULT_ALLOWED_SUB, InternalApiDeps, InternalApiServer, create_internal_app, internal_api_from_env
from telos.api.spiffe_auth import BundleUnavailableError, SvidVerifier, WorkloadApiKeys, decode_jwt_bundles
from telos.cardea_bridge import BridgeConfig
from telos.core.persistence import Database
from telos.core.repos.property_candidates import PropertyCandidateRepo
from telos.core.repos.routines import Routine
from telos.log import ConversationLog
from telos.services.routine_execution import RoutineExecutor

AUDIENCE = "telos-internal"
KERNEL = DEFAULT_ALLOWED_SUB
SCOPE = {"X-Cardea-Namespace": "telos-sam", "X-Cardea-Agent": "telos-scout"}
LISTING = "https://listings.example/property/42"
SIGNING_KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def public_jwk(private_key, kid: str) -> dict:
    return {**json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key())), "kid": kid, "use": "jwt-svid"}


class FakeKeys:
    """A trust domain bundle holding one EC signing key, standing in for the SPIRE agent."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.keys = {"k1": jwt.PyJWK(public_jwk(SIGNING_KEY, "k1"))}

    def signing_key(self, kid: str) -> jwt.PyJWK | None:
        if not self.available:
            raise BundleUnavailableError("agent down")
        return self.keys.get(kid)


def svid(*, sub: str = KERNEL, aud: str | list[str] = AUDIENCE, exp_in: int = 300, key=SIGNING_KEY, kid: str = "k1", **extra) -> str:
    claims = {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_in, "iat": int(time.time()), **extra}
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": kid, "typ": "JWT"})


def auth(token: str | None = None) -> dict:
    return {"Authorization": f"Bearer {token or svid()}", **SCOPE}


def routine(**changes) -> Routine:
    values = {"id": "r1", "user_id": "sam", "agent_slug": "scout", "name": "Daily search", "description": "Search listings.",
              "interval_seconds": 86400, "time_of_day": "08:30", "one_shot": False, "notify_email": True, "enabled": True,
              "last_run": None, "created_at": "2026-10-01T00:00:00"}
    return Routine(**{**values, **changes})


def assessment(disposition: str = "needs_verification", url: str = LISTING) -> dict:
    return {"url": url, "address": "1 Example Road", "disposition": disposition,
            "criteria": {"size": {"status": "unknown", "evidence": "Floorplan missing"}},
            "reason": "Size unconfirmed", "next_action": "Ask the agent for a floorplan"}


@pytest.fixture
def env(tmp_path):
    state = SimpleNamespace(routines=[routine()], tools=["rightmove_search", "send_email"], keys=FakeKeys(),
                            email="sam@example.com", recall=AsyncMock(return_value=None), root=tmp_path)
    deps = InternalApiDeps(
        routines=lambda: state.routines,
        agent_tools=lambda user, slug: state.tools,
        storage_root=tmp_path,
        get_user_email=lambda user: state.email,
        recall_fn=lambda user, slug: state.recall(user, slug),
        verifier=SvidVerifier(state.keys, audience=AUDIENCE, trust_domain="cardea.internal"),
        bridge=BridgeConfig(mode="shadow"),
    )
    state.deps = deps
    state.client = TestClient(create_internal_app(deps))
    state.history = tmp_path / "sam" / "scout" / "history.db"
    return state


# ── auth ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("headers", "status"), [
    ({}, 401),
    ({"Authorization": "Basic abc"}, 401),
    ({"Authorization": "Bearer not-a-jwt"}, 401),
    ({"Authorization": f"Bearer {svid(exp_in=-60)}"}, 401),
    ({"Authorization": f"Bearer {svid(aud='cardea-kernel')}"}, 401),
    ({"Authorization": f"Bearer {svid(key=OTHER_KEY)}"}, 401),
    ({"Authorization": f"Bearer {svid(kid='rotated-away')}"}, 401),
    ({"Authorization": f"Bearer {svid(sub='spiffe://elsewhere.internal/runtime/kernel')}"}, 401),
    ({"Authorization": f"Bearer {svid(sub='spiffe://cardea.internal/service/telos')}"}, 403),
])
def test_rejects_bad_tokens(env, headers, status):
    response = env.client.get("/internal/cardea/v1/ledger", headers={**headers, **SCOPE})
    assert response.status_code == status


def test_token_without_expiry_is_rejected(env):
    token = jwt.encode({"sub": KERNEL, "aud": AUDIENCE}, SIGNING_KEY, algorithm="ES256", headers={"kid": "k1"})
    assert env.client.get("/internal/cardea/v1/ledger", headers=auth(token)).status_code == 401


def test_symmetric_algorithms_are_rejected(env):
    token = jwt.encode({"sub": KERNEL, "aud": AUDIENCE, "exp": int(time.time()) + 60}, "secret" * 8, algorithm="HS256",
                       headers={"kid": "k1"})
    assert env.client.get("/internal/cardea/v1/ledger", headers=auth(token)).status_code == 401


def test_audience_list_containing_ours_is_accepted(env):
    assert env.client.get("/internal/cardea/v1/ledger", headers=auth(svid(aud=["other", AUDIENCE]))).status_code == 200


def test_unavailable_bundle_fails_closed_with_503(env):
    env.keys.available = False
    response = env.client.get("/internal/cardea/v1/ledger", headers=auth())
    assert response.status_code == 503


def test_auth_runs_before_scope_lookup(env):
    response = env.client.get("/internal/cardea/v1/ledger", headers={"X-Cardea-Namespace": "telos-nobody", "X-Cardea-Agent": "telos-x"})
    assert response.status_code == 401


def test_only_internal_paths_are_served(env):
    for path in ("/healthz", "/api/agents", "/docs", "/openapi.json"):
        assert env.client.get(path, headers=auth()).status_code == 404


# ── scoping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [
    {"X-Cardea-Namespace": "telos-alex", "X-Cardea-Agent": "telos-scout"},
    {"X-Cardea-Namespace": "telos-sam", "X-Cardea-Agent": "telos-helper"},
    {"X-Cardea-Namespace": "sam", "X-Cardea-Agent": "scout"},
])
def test_unknown_agents_are_404(env, headers):
    response = env.client.get("/internal/cardea/v1/ledger", headers={"Authorization": f"Bearer {svid()}", **headers})
    assert response.status_code == 404


def test_missing_scope_headers_are_400(env):
    response = env.client.get("/internal/cardea/v1/ledger", headers={"Authorization": f"Bearer {svid()}"})
    assert response.status_code == 400


@pytest.mark.parametrize("change", [
    {"routines": []},
    {"routines": [routine(one_shot=True)]},  # Telos keeps one-shot routines
    {"tools": ["create_routine"]},  # no tool maps to a Cardea operation
])
def test_agents_without_a_mirrorable_routine_are_404(env, change):
    for name, value in change.items():
        setattr(env, name, value)
    assert env.client.get("/internal/cardea/v1/ledger", headers=auth()).status_code == 404


def test_routines_kept_local_by_mode_are_404(env):
    for bridge in (BridgeConfig(mode="off"), BridgeConfig(mode="shadow", routines={"r1": "off"})):
        client = TestClient(create_internal_app(dataclasses.replace(env.deps, bridge=bridge)))
        assert client.get("/internal/cardea/v1/ledger", headers=auth()).status_code == 404
    live = TestClient(create_internal_app(dataclasses.replace(env.deps, bridge=BridgeConfig(mode="off", routines={"r1": "live"}))))
    assert live.get("/internal/cardea/v1/ledger", headers=auth()).status_code == 200


def test_scope_matches_bridge_identifiers_for_unsafe_slugs(env):
    env.routines = [routine(user_id="sam@example", agent_slug="scout two")]
    headers = {"Authorization": f"Bearer {svid()}", "X-Cardea-Namespace": "telos-sam-example", "X-Cardea-Agent": "telos-scout-two"}
    assert env.client.get("/internal/cardea/v1/ledger", headers=headers).status_code == 200


# ── routine context ────────────────────────────────────────────────────


def record_email(history: ConversationLog, body: str) -> None:
    turn = history.append("daily", "Email sent", "prompt", is_routine=True)
    history.execution_events.append(turn, "tool_call", {
        "name": "send_email", "call_id": "send", "input": {"to": "sam@example.com", "subject": "Recap", "body": body}})
    history.execution_events.append(turn, "tool_result", {"call_id": "send", "is_error": False})


async def test_routine_context_matches_what_telos_injects(env):
    record_email(ConversationLog(env.history), f"Already sent {LISTING}")
    PropertyCandidateRepo(Database(env.history)).record(LISTING, assessment())
    env.recall.return_value = {"records": [{"id": "m1", "content": "Applied to 3 Sample Lane"}], "errors": []}

    response = env.client.get("/internal/cardea/v1/routine-context", headers={**auth(), "X-Cardea-Slot-Time": "2026-10-19T23:30:00+00:00"})
    assert response.status_code == 200
    body = response.json()
    assert body["invocation"] == {"reporting_date": "2026-10-20", "timezone": "Europe/London", "slot_time": "2026-10-19T23:30:00+00:00"}
    assert LISTING in body["delivery_history"] and "not necessarily recommended" in body["delivery_history"]
    assert body["ledger_summary"].startswith("Historical property assessments") and LISTING in body["ledger_summary"]
    assert body["reserved_recall"].startswith("Reserved reference records") and "3 Sample Lane" in body["reserved_recall"]

    messages = []

    async def stream(message, ctx):
        messages.append(message)
        yield "text", "Done"

    runtime = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(astream=stream)), make_context=MagicMock())
    executor = RoutineExecutor(runtime, MagicMock(), lambda _: "sam@example.com", lambda *_: "Scout", "",
                               storage_root=env.root, recall_fn=env.recall)
    await executor.execute("sam", "scout", "search")
    assert messages[0] == "search\n\n" + "\n\n".join([body["delivery_history"], body["ledger_summary"], body["reserved_recall"]])


def test_routine_context_for_a_fresh_agent_is_empty_text(env):
    env.recall.return_value = None
    body = env.client.get("/internal/cardea/v1/routine-context", headers=auth()).json()
    assert body["delivery_history"] == body["ledger_summary"] == body["reserved_recall"] == ""
    assert body["invocation"]["slot_time"] is None
    assert body["invocation"]["reporting_date"] == datetime.now(ZoneInfo("Europe/London")).date().isoformat()
    assert not env.history.exists()


def test_routine_context_discloses_recall_failure(env):
    env.recall.side_effect = RuntimeError("memory offline")
    body = env.client.get("/internal/cardea/v1/routine-context", headers=auth()).json()
    assert "RuntimeError: memory offline" in body["reserved_recall"]


def test_invalid_slot_time_is_422(env):
    for value in ("yesterday", "2026-10-20T08:30:00"):
        response = env.client.get("/internal/cardea/v1/routine-context", headers={**auth(), "X-Cardea-Slot-Time": value})
        assert response.status_code == 422


# ── ledger reads ───────────────────────────────────────────────────────


def test_ledger_pages_latest_records_up_to_200(env):
    repo = PropertyCandidateRepo(Database(env.history))
    for index in range(205):
        repo.record(f"https://listings.example/property/{index}", assessment(url=f"https://listings.example/property/{index}"))
    first = env.client.get("/internal/cardea/v1/ledger?limit=200", headers=auth()).json()
    assert len(first["items"]) == 200 and first["next_offset"] == 200
    assert first["items"][0]["url"] == "https://listings.example/property/204"
    rest = env.client.get("/internal/cardea/v1/ledger?offset=200&limit=200", headers=auth()).json()
    assert len(rest["items"]) == 5 and rest["next_offset"] is None
    assert env.client.get("/internal/cardea/v1/ledger?limit=201", headers=auth()).status_code == 422


def test_ledger_item_shape_matches_the_tool(env):
    repo = PropertyCandidateRepo(Database(env.history))
    repo.record(LISTING, assessment())
    items = env.client.get("/internal/cardea/v1/ledger", headers=auth()).json()["items"]
    assert items == repo.page()["records"]


def test_ledger_reads_never_create_a_ledger(env):
    assert env.client.get("/internal/cardea/v1/ledger", headers=auth()).json() == {"items": [], "next_offset": None}
    assert env.client.get("/internal/cardea/v1/ledger/history", params={"url": LISTING}, headers=auth()).json() == {"history": []}
    assert env.client.get("/internal/cardea/v1/ledger/assessments/by-key/k", headers=auth()).status_code == 404
    assert not env.history.exists()


def test_history_canonicalises_the_url(env):
    repo = PropertyCandidateRepo(Database(env.history))
    repo.record(LISTING, assessment())
    repo.record(LISTING, assessment())
    response = env.client.get("/internal/cardea/v1/ledger/history", params={"url": "https://LISTINGS.example/property/42/"}, headers=auth())
    assert response.status_code == 200
    assert len(response.json()["history"]) == 2
    assert env.client.get("/internal/cardea/v1/ledger/history", params={"url": "not a url"}, headers=auth()).status_code == 422


# ── ledger writes ──────────────────────────────────────────────────────


def post(env, body, *, key: str | None = "effect-1", dry_run: bool = False):
    headers = {**auth(), **({"Idempotency-Key": key} if key is not None else {})}
    return env.client.post("/internal/cardea/v1/ledger/assessments" + ("?dry_run=1" if dry_run else ""), json=body, headers=headers)


def test_record_is_idempotent_per_key(env):
    first = post(env, assessment())
    assert first.status_code == 201
    assert first.json() == {"recorded": True, "assessment_id": first.json()["assessment_id"], "duplicate": False}
    again = post(env, assessment())
    assert again.status_code == 201
    assert again.json() == {**first.json(), "duplicate": True}
    other = post(env, assessment(), key="effect-2")
    assert other.json()["assessment_id"] != first.json()["assessment_id"]
    assert len(PropertyCandidateRepo(Database(env.history)).history(LISTING)) == 2


def test_replay_returns_original_even_if_body_no_longer_validates(env):
    first = post(env, assessment()).json()
    replay = post(env, {"url": LISTING})
    assert replay.status_code == 201 and replay.json() == {**first, "duplicate": True}


def test_by_key_lookup(env):
    recorded = post(env, assessment(), key="effect-9").json()
    found = env.client.get("/internal/cardea/v1/ledger/assessments/by-key/effect-9", headers=auth())
    assert found.status_code == 200 and found.json() == {"assessment_id": recorded["assessment_id"]}
    assert env.client.get("/internal/cardea/v1/ledger/assessments/by-key/missing", headers=auth()).status_code == 404


def test_recorded_payload_matches_the_tool(env):
    post(env, {**assessment(), "url": "https://LISTINGS.example/property/42/"})
    saved = PropertyCandidateRepo(Database(env.history)).history(LISTING)[0]
    assert saved["url"] == LISTING and saved["disposition"] == "needs_verification" and saved["first_recorded_at"]


@pytest.mark.parametrize(("body", "key"), [
    (assessment("verified"), "k"),  # verified without passing evidence
    ({**assessment(), "unexpected": 1}, "k"),
    ({"url": LISTING}, "k"),
    (assessment(), None),
    (assessment(), ""),
    (assessment(), "x" * 129),
])
def test_invalid_records_are_422(env, body, key):
    assert post(env, body, key=key).status_code == 422
    assert not env.history.exists() or PropertyCandidateRepo(Database(env.history)).page()["total"] == 0


def test_ledger_rules_are_422(env):
    post(env, assessment("applied"), key="a")
    response = post(env, assessment(), key="b")
    assert response.status_code == 422
    assert "remain excluded" in response.text


def test_dry_run_validates_without_writing(env):
    response = post(env, assessment(), key=None, dry_run=True)
    assert response.status_code == 200 and response.json() == {"recorded": False, "valid": True}
    assert not env.history.exists()
    assert post(env, assessment("verified"), key=None, dry_run=True).status_code == 422
    post(env, assessment("applied"), key="a")
    assert post(env, assessment(), key="b", dry_run=True).status_code == 422
    assert post(env, assessment("applied"), key="a2", dry_run=True).status_code == 200
    assert len(PropertyCandidateRepo(Database(env.history)).history(LISTING)) == 1
    assert env.client.get("/internal/cardea/v1/ledger/assessments/by-key/a2", headers=auth()).status_code == 404


def test_existing_ledgers_gain_the_key_column(tmp_path):
    db = Database(tmp_path / "history.db")
    with db.connect() as conn:
        conn.execute("CREATE TABLE property_assessments (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, "
                     "recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL)")
        conn.execute("INSERT INTO property_assessments(url,recorded_at,payload_json) VALUES ('u','t','{\"disposition\":\"dropped\"}')")
    repo = PropertyCandidateRepo(db)
    assert repo.page()["total"] == 1
    first = repo.record_keyed("u", assessment(), idempotency_key="k")
    assert repo.record_keyed("u", assessment(), idempotency_key="k") == (first[0], first[1], True)
    repo.record("u", assessment())
    repo.record("u", assessment())  # unkeyed rows never collide
    assert len(repo.history("u")) == 4


# ── SPIRE Workload API client ──────────────────────────────────────────


def encode_bundles(bundles: dict[str, bytes]) -> bytes:
    """Protobuf encoding of JWTBundlesResponse built with the real protobuf runtime."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory  # noqa: PLC0415

    file = descriptor_pb2.FileDescriptorProto(name="workload_test.proto", package="", syntax="proto3")
    response = file.message_type.add(name="JWTBundlesResponse")
    entry = response.nested_type.add(name="BundlesEntry")
    entry.options.map_entry = True
    entry.field.add(name="key", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING, label=1)
    entry.field.add(name="value", number=2, type=descriptor_pb2.FieldDescriptorProto.TYPE_BYTES, label=1)
    response.field.add(name="bundles", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, label=3,
                       type_name=".JWTBundlesResponse.BundlesEntry")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    message = message_factory.GetMessageClass(pool.FindMessageTypeByName("JWTBundlesResponse"))()
    for domain, value in bundles.items():
        message.bundles[domain] = value
    return message.SerializeToString()


def test_decode_jwt_bundles():
    bundles = {"cardea.internal": b'{"keys":[]}', "other.internal": b"\x00\xff"}
    assert decode_jwt_bundles(encode_bundles(bundles)) == bundles
    assert decode_jwt_bundles(b"") == {}


@pytest.fixture
def spire_agent():
    """A fake SPIRE agent serving FetchJWTBundles on a unix socket."""
    state = SimpleNamespace(calls=0, metadata=[], bundles={"cardea.internal": json.dumps({"keys": [public_jwk(SIGNING_KEY, "k1")]}).encode()})

    def fetch(request, context):
        state.calls += 1
        state.metadata.append(dict(context.invocation_metadata()))
        if dict(context.invocation_metadata()).get("workload.spiffe.io") != "true":
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "missing security header")
        yield encode_bundles(state.bundles)

    handler = grpc.method_handlers_generic_handler("SpiffeWorkloadAPI", {"FetchJWTBundles": grpc.unary_stream_rpc_method_handler(
        fetch, request_deserializer=lambda data: data, response_serializer=lambda data: data)})
    directory = tempfile.mkdtemp(prefix="spire")
    socket = f"unix://{os.path.join(directory, 'agent.sock')}"
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((handler,))
    server.add_insecure_port(socket)
    server.start()
    state.socket = socket
    yield state
    server.stop(None)


def test_workload_api_keys_validate_real_tokens(spire_agent):
    keys = WorkloadApiKeys(spire_agent.socket, "cardea.internal", min_refresh=0)
    verifier = SvidVerifier(keys, audience=AUDIENCE, trust_domain="cardea.internal")
    assert verifier.verify(svid()) == KERNEL
    assert verifier.verify(svid()) == KERNEL
    assert spire_agent.calls == 1  # cached
    assert spire_agent.metadata[0]["workload.spiffe.io"] == "true"


def test_workload_api_keys_refresh_on_rotation(spire_agent):
    keys = WorkloadApiKeys(spire_agent.socket, "cardea.internal", min_refresh=0)
    assert keys.signing_key("k1") is not None
    spire_agent.bundles = {"cardea.internal": json.dumps({"keys": [public_jwk(OTHER_KEY, "k2")]}).encode()}
    assert keys.signing_key("k2") is not None
    assert spire_agent.calls == 2


def test_workload_api_keys_missing_trust_domain(spire_agent):
    spire_agent.bundles = {"elsewhere.internal": b'{"keys":[]}'}
    with pytest.raises(BundleUnavailableError, match="no JWT bundle"):
        WorkloadApiKeys(spire_agent.socket, "cardea.internal").signing_key("k1")


def test_workload_api_keys_agent_down():
    keys = WorkloadApiKeys(f"unix://{tempfile.mkdtemp(prefix='spire')}/absent.sock", "cardea.internal", timeout=0.5)
    with pytest.raises(BundleUnavailableError):
        keys.signing_key("k1")


# ── embedded server ────────────────────────────────────────────────────


def test_off_unless_enabled(monkeypatch, tmp_path):
    kwargs = {"routines": list, "agent_tools": lambda u, s: [], "storage_root": tmp_path, "get_user_email": lambda u: None, "recall_fn": None}
    monkeypatch.delenv("TELOS_INTERNAL_API", raising=False)
    assert internal_api_from_env(**kwargs) is None
    monkeypatch.setenv("TELOS_INTERNAL_API", "1")
    assert isinstance(internal_api_from_env(**kwargs), InternalApiServer)


async def test_embedded_server_serves_and_stops(env):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = InternalApiServer(env.client.app, host="127.0.0.1", port=port)
    server.start()
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.02)
    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{port}/internal/cardea/v1/ledger", headers=auth())
    assert response.status_code == 200
    await server.stop()
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/internal/cardea/v1/ledger")


async def test_bind_failure_does_not_exit_the_process(env):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        server = InternalApiServer(env.client.app, host="127.0.0.1", port=taken.getsockname()[1])
        server.start()
        await server.stop()
    assert not server.started


# ── stable error codes ─────────────────────────────────────────────────


def test_error_bodies_carry_stable_codes(env):
    get = env.client.get
    bearer = {"Authorization": f"Bearer {svid()}"}
    post(env, assessment("applied"), key="a")
    cases = [
        (get("/internal/cardea/v1/ledger", headers=SCOPE), 401, "missing_token"),
        (get("/internal/cardea/v1/ledger", headers={"Authorization": "Bearer nope", **SCOPE}), 401, "invalid_token"),
        (get("/internal/cardea/v1/ledger", headers=auth(svid(sub="spiffe://cardea.internal/x"))), 403, "subject_not_allowed"),
        (get("/internal/cardea/v1/ledger", headers=bearer), 400, "missing_scope"),
        (get("/internal/cardea/v1/ledger", headers={**bearer, "X-Cardea-Namespace": "telos-alex", "X-Cardea-Agent": "telos-x"}),
         404, "no_mirrored_routine"),
        (get("/internal/cardea/v1/ledger/assessments/by-key/missing", headers=auth()), 404, "assessment_not_found"),
        (get("/internal/cardea/v1/nowhere", headers=auth()), 404, "not_found"),
        (get("/internal/cardea/v1/ledger?limit=500", headers=auth()), 422, "invalid_request"),
        (get("/internal/cardea/v1/routine-context", headers={**auth(), "X-Cardea-Slot-Time": "soon"}), 422, "invalid_slot_time"),
        (get("/internal/cardea/v1/ledger/history", params={"url": "x"}, headers=auth()), 422, "invalid_url"),
        (post(env, assessment(), key=None), 422, "invalid_idempotency_key"),
        (post(env, {"url": LISTING}, key="b"), 422, "invalid_assessment"),
        (post(env, assessment(), key="c"), 422, "ledger_rule"),
    ]
    for response, status, code in cases:
        assert (response.status_code, response.json()["code"]) == (status, code), response.text
        assert response.json()["detail"]
    env.keys.available = False
    down = get("/internal/cardea/v1/ledger", headers=auth())
    assert (down.status_code, down.json()["code"], down.headers["Retry-After"]) == (503, "bundle_unavailable", "10")
    assert get("/internal/cardea/v1/ledger", headers=SCOPE).headers["WWW-Authenticate"] == "Bearer"


def test_404_detail_text_is_unchanged(env):
    headers = {"Authorization": f"Bearer {svid()}", "X-Cardea-Namespace": "telos-alex", "X-Cardea-Agent": "telos-x"}
    assert env.client.get("/internal/cardea/v1/ledger", headers=headers).json()["detail"] == "No mirrored routine for this agent"
    missing = env.client.get("/internal/cardea/v1/ledger/assessments/by-key/k", headers=auth()).json()
    assert missing["detail"] == "No assessment recorded under this key"
