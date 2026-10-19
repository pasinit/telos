"""Unit tests for ``AgentRuntime``.

Exercises the cache + name-fallback resolution + Phase 2b setter-injection
removal in isolation. The real LLM client construction is fine — it's
``httpx.AsyncClient`` + ``Pontifex`` instances, no network until astream
is awaited.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from telos.api.registry import AgentRegistry
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.routines import RoutineStore
from telos.services import AgentRuntime


@pytest.fixture
def runtime(tmp_path: Path) -> AgentRuntime:
    registry = AgentRegistry(tmp_path / "registry.db")
    routine_store = RoutineStore(tmp_path / "routines.db")
    return AgentRuntime(
        registry=registry,
        storage_root=tmp_path,
        recordatio_url="http://recordatio.test:8787",
        model="qwen2.5:14b",
        chat_api_key=None,
        improver_api_key=None,
        utility_model="claude-haiku-4-5-20251001",
        max_history_turns=30,
        max_context_tokens=50_000,
        routine_store=routine_store,
        google_oauth_client_id="cid",
        google_oauth_client_secret="csec",
    )


@pytest.mark.asyncio
async def test_get_returns_cached_instance(runtime: AgentRuntime):
    """Two calls for the same ``(user, slug)`` return the same instance."""
    runtime._registry.create("alice", "Weather", "describe weather", "system prompt")
    a = await runtime.get("alice", "weather")
    b = await runtime.get("alice", "weather")
    assert a is b


@pytest.mark.asyncio
async def test_get_resolves_by_name_fallback(runtime: AgentRuntime):
    """``get`` falls back to matching by name when the slug doesn't hit."""
    runtime._registry.create("alice", "Weather", "describe weather", "system prompt")
    by_slug = await runtime.get("alice", "weather")
    by_name = await runtime.get("alice", "Weather")
    assert by_slug is by_name


@pytest.mark.asyncio
async def test_get_unknown_slug_raises_lookup_error(runtime: AgentRuntime):
    with pytest.raises(LookupError):
        await runtime.get("alice", "no-such-agent")


@pytest.mark.asyncio
async def test_get_housekeeper_caches(runtime: AgentRuntime):
    """Two calls for the housekeeper return the same instance."""
    hk1 = await runtime.get_housekeeper("alice")
    hk2 = await runtime.get_housekeeper("alice")
    assert hk1 is hk2
    # And the housekeeper is keyed under HOUSEKEEPER_SLUG.
    assert runtime.cached("alice", HOUSEKEEPER_SLUG) is hk1


@pytest.mark.asyncio
async def test_housekeepers_are_per_user(runtime: AgentRuntime):
    a = await runtime.get_housekeeper("alice")
    b = await runtime.get_housekeeper("bob")
    assert a is not b


@pytest.mark.asyncio
async def test_get_housekeeper_via_slug(runtime: AgentRuntime):
    """Asking ``get`` for the housekeeper slug also returns the housekeeper."""
    hk_direct = await runtime.get_housekeeper("alice")
    hk_via_get = await runtime.get("alice", HOUSEKEEPER_SLUG)
    assert hk_direct is hk_via_get


@pytest.mark.asyncio
async def test_evict_drops_cache_entry(runtime: AgentRuntime):
    runtime._registry.create("alice", "Weather", "describe", "prompt")
    first = await runtime.get("alice", "weather")
    runtime.evict("alice", "weather")
    assert runtime.cached("alice", "weather") is None
    second = await runtime.get("alice", "weather")
    assert first is not second  # rebuilt


@pytest.mark.asyncio
async def test_clear_drops_all_cached_agents(runtime: AgentRuntime):
    runtime._registry.create("alice", "Weather", "describe", "prompt")
    await runtime.get("alice", "weather")
    await runtime.get_housekeeper("alice")
    assert len(runtime.all_cached()) == 2
    runtime.clear()
    assert runtime.all_cached() == []


@pytest.mark.asyncio
async def test_constructor_propagates_to_agent(runtime: AgentRuntime):
    """The kwargs the runtime received are forwarded to the Agent constructor.

    Phase 2b's whole point: no more post-construction
    ``agent._registry = …`` setter injection.
    """
    runtime._registry.create("alice", "Weather", "describe", "prompt")
    agent = await runtime.get("alice", "weather")
    assert agent._registry is runtime._registry
    # `runtime.get` is a bound method — each attribute access yields a fresh
    # binding object, so compare the underlying function instead of identity.
    assert agent._get_agent_fn.__func__ is AgentRuntime.get
    assert agent._google_oauth_client_id == "cid"
    assert agent._google_oauth_client_secret == "csec"


@pytest.mark.asyncio
async def test_housekeeper_constructor_propagates(runtime: AgentRuntime):
    """Same setter-injection-removal check for the housekeeper path."""
    agent = await runtime.get_housekeeper("alice")
    assert agent._registry is runtime._registry
    # `runtime.get` is a bound method — each attribute access yields a fresh
    # binding object, so compare the underlying function instead of identity.
    assert agent._get_agent_fn.__func__ is AgentRuntime.get
    assert agent._google_oauth_client_id == "cid"
    assert agent._google_oauth_client_secret == "csec"


def test_make_context_carries_shared_graph(runtime: AgentRuntime):
    """Every context built by the runtime points at the same DelegationGraph
    so nested ``delegate_to`` calls all see one chain (cycle detection works).
    """
    a = runtime.make_context("alice")
    b = runtime.make_context("alice", is_routine=True)
    c = runtime.make_context("bob", image_url="data:image/png;base64,abc")
    assert a.delegation_graph is b.delegation_graph is c.delegation_graph
    assert a.is_routine is False
    assert b.is_routine is True
    assert c.image_url == "data:image/png;base64,abc"


def test_make_context_defaults(runtime: AgentRuntime):
    ctx = runtime.make_context("alice")
    assert ctx.user_id == "alice"
    assert ctx.image_url is None
    assert ctx.is_routine is False
    assert ctx.correlation_id is None
    assert ctx.request_id is None


# ──────────────────────────────────────────────────────────────────────
# Empty tool whitelist must mean "no tools", not "all tools" (#124).
# A legacy ``json.loads(... or "[]") or None`` coercion in
# ``_build_specialist`` used to collapse ``[]`` to ``None``, which the
# tool-resolution path treated as "use every available tool" —
# exposing tools the user had deselected in the UI.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_specialist_with_empty_tools_gets_no_tools(runtime: AgentRuntime):
    runtime._registry.create("alice", "Bare", "no tools selected", "prompt", tools=[])
    agent = await runtime.get("alice", "bare")
    assert agent._tools == []


@pytest.mark.asyncio
async def test_specialist_with_specific_tools_passes_them_through(runtime: AgentRuntime):
    runtime._registry.create(
        "alice", "Searcher", "uses search only", "prompt", tools=["web_search"],
    )
    agent = await runtime.get("alice", "searcher")
    assert agent._tools == ["web_search"]


@pytest.mark.asyncio
async def test_specialist_receives_injected_sandbox_run(tmp_path: Path):
    async def sandbox_run(command, *, timeout=30, cwd=None):
        return "ok\n", "", 0

    registry = AgentRegistry(tmp_path / "registry.db")
    runtime = AgentRuntime(
        registry=registry,
        storage_root=tmp_path,
        recordatio_url="http://recordatio.test:8787",
        model="qwen2.5:14b",
        chat_api_key=None,
        improver_api_key=None,
        utility_model="claude-haiku-4-5-20251001",
        max_history_turns=30,
        max_context_tokens=50_000,
        routine_store=RoutineStore(tmp_path / "routines.db"),
        sandbox_run=sandbox_run,
    )

    registry.create("alice", "Code", "runs code", "prompt", tools=["python_exec"])
    agent = await runtime.get("alice", "code")

    assert agent._sandbox_run is sandbox_run


@pytest.mark.asyncio
async def test_housekeeper_seeded_with_all_builtin_tools(runtime: AgentRuntime):
    """``ensure_housekeeper`` writes every built-in tool name so the
    housekeeper retains its full toolset after the empty-list-means-none
    fix. MCP tools stay out — users opt in explicitly."""
    from telos.tools import tool_repository

    hk = await runtime.get_housekeeper("alice")
    expected = {t.name for t in tool_repository.list_all() if t.type == "builtin"}
    assert set(hk._tools or []) == expected
    assert not any(name.startswith("mcp:") for name in (hk._tools or []))


# ──────────────────────────────────────────────────────────────────────
# Seed-on-recordatio-mode failure must NOT poison the cache (PR #159 review,
# P0b). Previously the runtime swallowed every seed exception and cached
# the (half-built) agent anyway, so a transient Recordatio outage on the
# very first build would pin a permanently-broken instance until process
# restart. After the fix: failure propagates out of ``get`` and the next
# call retries cleanly.
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_failure_does_not_cache_agent(tmp_path: Path):
    registry = AgentRegistry(tmp_path / "registry.db")
    routine_store = RoutineStore(tmp_path / "routines.db")
    registry.create("alice", "Weather", "describe", "prompt", tools=[])

    runtime = AgentRuntime(
        registry=registry,
        storage_root=tmp_path,
        recordatio_url="http://recordatio.test:8787",
        model="haiku",
        chat_api_key=None,
        improver_api_key=None,
        utility_model="haiku",
        max_history_turns=10,
        max_context_tokens=1000,
        routine_store=routine_store,
        telos_mcp_url="http://telos.test",
    )

    # First attempt: seed blows up — expect the exception to surface and
    # the cache to remain empty.
    boom = AsyncMock(side_effect=RuntimeError("recordatio unreachable"))
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=boom):
        with pytest.raises(RuntimeError, match="recordatio unreachable"):
            await runtime.get("alice", "weather")
    assert runtime.cached("alice", "weather") is None

    # Second attempt: seed succeeds — the agent is built, cached, and a
    # subsequent ``get`` returns the same instance (no leftover broken
    # entry from attempt #1).
    ok = AsyncMock(return_value=False)
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=ok):
        first = await runtime.get("alice", "weather")
        second = await runtime.get("alice", "weather")
    assert first is second
    assert runtime.cached("alice", "weather") is first


# ──────────────────────────────────────────────────────────────────────
# Seed client auth + failure classification.
# - The seed httpx client must carry the SAME Bearer credential the chat
#   path sends (Recordatio's config endpoints 401 without it).
# - Transport errors → RecordatioUnavailableError (transient, retryable);
#   HTTP rejections / RuntimeError → AgentConfigSeedError (fail closed).
#   Neither may cache the half-built agent.
# ──────────────────────────────────────────────────────────────────────


def _recordatio_runtime(tmp_path: Path, *, chat_api_key: str | None) -> AgentRuntime:
    registry = AgentRegistry(tmp_path / "registry.db")
    registry.create("alice", "Weather", "describe", "prompt", tools=[])
    return AgentRuntime(
        registry=registry,
        storage_root=tmp_path,
        recordatio_url="http://recordatio.test:8787",
        model="haiku",
        chat_api_key=chat_api_key,
        improver_api_key=None,
        utility_model="haiku",
        max_history_turns=10,
        max_context_tokens=1000,
        routine_store=RoutineStore(tmp_path / "routines.db"),
        telos_mcp_url="http://telos.test",
    )


@pytest.mark.asyncio
async def test_seed_client_sends_bearer_auth_header(tmp_path: Path):
    """The seed client must authenticate like the chat path does
    (``Authorization: Bearer {chat_api_key}``) — Recordatio's config
    endpoints 401 on anonymous requests, which would leave the config
    permanently unseeded on fresh deploys."""
    runtime = _recordatio_runtime(tmp_path, chat_api_key="sk-chat-key")

    captured: dict[str, str] = {}

    async def capture(client, **kwargs):
        captured.update(client.headers)
        return False

    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=capture):
        await runtime.get("alice", "weather")
    assert captured.get("authorization") == "Bearer sk-chat-key"


@pytest.mark.asyncio
async def test_seed_client_omits_auth_header_without_key(tmp_path: Path):
    """No chat_api_key (test/dev setups) must not produce a malformed
    ``Authorization: Bearer `` header."""
    runtime = _recordatio_runtime(tmp_path, chat_api_key=None)

    captured: dict[str, str] = {}

    async def capture(client, **kwargs):
        captured.update(client.headers)
        return False

    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=capture):
        await runtime.get("alice", "weather")
    assert "authorization" not in captured


@pytest.mark.asyncio
async def test_seed_connect_error_raises_unavailable_and_does_not_cache(tmp_path: Path):
    """Recordatio down at cold start is transient: surface
    ``RecordatioUnavailableError`` (mapped to a retryable 503 in the API
    layer), do NOT cache the agent, and let the next call retry."""
    import httpx

    from telos.services import RecordatioUnavailableError

    runtime = _recordatio_runtime(tmp_path, chat_api_key="sk")

    boom = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=boom):
        with pytest.raises(RecordatioUnavailableError):
            await runtime.get("alice", "weather")
    assert runtime.cached("alice", "weather") is None

    # Recordatio comes back: the next call retries the seed and caches.
    ok = AsyncMock(return_value=True)
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=ok):
        agent = await runtime.get("alice", "weather")
    assert runtime.cached("alice", "weather") is agent


@pytest.mark.asyncio
async def test_seed_http_error_raises_config_seed_error_and_does_not_cache(tmp_path: Path):
    """A 401/409-style HTTP rejection from Recordatio is a config error —
    classify as ``AgentConfigSeedError`` (fail closed) and don't cache."""
    import httpx

    from telos.services import AgentConfigSeedError

    runtime = _recordatio_runtime(tmp_path, chat_api_key="sk")

    resp = httpx.Response(401, request=httpx.Request("GET", "http://recordatio.test:8787"))
    boom = AsyncMock(
        side_effect=httpx.HTTPStatusError("401", request=resp.request, response=resp),
    )
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=boom):
        with pytest.raises(AgentConfigSeedError):
            await runtime.get("alice", "weather")
    assert runtime.cached("alice", "weather") is None


@pytest.mark.asyncio
async def test_seed_401_without_chat_api_key_has_actionable_hint(tmp_path: Path):
    """A 401 when telos sent *no* credential must tell the operator what
    to do — set CHAT_API_KEY / chat_api_key — not just say 'seed failed'."""
    import httpx

    from telos.services import AgentConfigSeedError

    runtime = _recordatio_runtime(tmp_path, chat_api_key=None)

    resp = httpx.Response(401, request=httpx.Request("GET", "http://recordatio.test:8787"))
    boom = AsyncMock(
        side_effect=httpx.HTTPStatusError("401", request=resp.request, response=resp),
    )
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=boom):
        with pytest.raises(AgentConfigSeedError, match="CHAT_API_KEY"):
            await runtime.get("alice", "weather")


@pytest.mark.asyncio
async def test_seed_401_with_chat_api_key_has_no_missing_credential_hint(tmp_path: Path):
    """When a credential WAS sent, a 401 means it's wrong — the
    'set CHAT_API_KEY' hint would mislead the operator."""
    import httpx

    from telos.services import AgentConfigSeedError

    runtime = _recordatio_runtime(tmp_path, chat_api_key="sk-wrong")

    resp = httpx.Response(401, request=httpx.Request("GET", "http://recordatio.test:8787"))
    boom = AsyncMock(
        side_effect=httpx.HTTPStatusError("401", request=resp.request, response=resp),
    )
    with patch("telos.services.agent_runtime.seed_agent_config_if_missing", new=boom):
        with pytest.raises(AgentConfigSeedError) as exc_info:
            await runtime.get("alice", "weather")
    assert "CHAT_API_KEY" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_agent_dep_maps_seed_failures_to_503(monkeypatch, tmp_path: Path):
    """``deps.get_agent`` must translate seed failures into clean 503s
    (transient → retryable with Retry-After; config → fail closed), not
    let them bubble as raw 500s."""
    from fastapi import HTTPException

    from telos.api import app as srv
    from telos.api import deps
    from telos.services import AgentConfigSeedError, RecordatioUnavailableError

    class _StubRuntime:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        async def get(self, uid: str, slug: str):
            raise self._exc

    monkeypatch.setattr(
        srv, "_agent_runtime", _StubRuntime(RecordatioUnavailableError("down")),
    )
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_agent("weather", uid="alice")
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "5"}

    monkeypatch.setattr(
        srv, "_agent_runtime", _StubRuntime(AgentConfigSeedError("bad creds")),
    )
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_agent("weather", uid="alice")
    assert exc_info.value.status_code == 503

    # LookupError mapping unchanged.
    monkeypatch.setattr(srv, "_agent_runtime", _StubRuntime(LookupError("nope")))
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_agent("weather", uid="alice")
    assert exc_info.value.status_code == 404


def test_app_helper_routes_map_seed_failures_to_503(monkeypatch, tmp_path: Path):
    """Routes that call ``_app._get_agent`` / ``_get_housekeeper`` directly
    (turns history, costs, settings page, routines) must get the same
    exception→HTTP mapping as the ``Depends()`` path — the SPA hits
    ``GET /api/agents/{slug}/turns`` *before* the first chat, so a raw
    500 here is the first thing a user sees on a cold-start failure."""
    from fastapi.testclient import TestClient

    from telos.api import app as srv
    from telos.housekeeper import HOUSEKEEPER_SLUG
    from telos.services import AgentConfigSeedError, RecordatioUnavailableError

    class _StubRuntime:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        async def get(self, uid: str, slug: str):
            raise self._exc

    monkeypatch.setattr(srv.settings, "storage_root", tmp_path)
    client = TestClient(srv.app, follow_redirects=False)
    cookie = srv._signer.dumps("alice")

    # Transient seed failure → 503 + Retry-After (was a raw 500).
    monkeypatch.setattr(
        srv, "_agent_runtime", _StubRuntime(RecordatioUnavailableError("down")),
    )
    r = client.get(
        f"/api/agents/{HOUSEKEEPER_SLUG}/turns", cookies={"user_id": cookie},
    )
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"

    # Config seed failure → 503 fail-closed, no Retry-After.
    monkeypatch.setattr(
        srv, "_agent_runtime", _StubRuntime(AgentConfigSeedError("bad creds")),
    )
    r = client.get(
        f"/api/agents/{HOUSEKEEPER_SLUG}/turns", cookies={"user_id": cookie},
    )
    assert r.status_code == 503
    assert "Retry-After" not in r.headers


def test_housekeeper_backfill_updates_legacy_empty_row(tmp_path: Path):
    """A row written before the fix (with ``tools='[]'``) gets backfilled
    on next ``ensure_housekeeper`` call."""
    import json
    from datetime import UTC, datetime

    from telos.api.registry import AgentRegistry
    from telos.housekeeper import HOUSEKEEPER_SLUG

    registry = AgentRegistry(tmp_path / "registry.db")
    with registry._db.connect() as conn:
        conn.execute(
            "INSERT INTO agents (user_id, slug, name, description, system_prompt, tools, "
            "tool_descriptions, created_at, last_active) VALUES (?,?,?,?,?,?,?,?,?)",
            ("alice", HOUSEKEEPER_SLUG, "ταμίας", "", "", "[]", "{}",
             datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )

    row = registry.ensure_housekeeper("alice")
    assert json.loads(row["tools"]) != []
