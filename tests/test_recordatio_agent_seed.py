"""Tests for the Phase 4 Recordatio AgentConfig seeding step.

Covers ``telos.services.recordatio_agent_seed`` — the bridge that, on
first build of a recordatio-mode Agent, populates Recordatio's AgentConfig
from telos's local registry rows.
"""
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.services.recordatio_agent_seed import (
    _registry_row_to_subagent,
    build_agent_config_payload,
    seed_agent_config_if_missing,
)

# ---------------------------------------------------------------------------
# _registry_row_to_subagent
# ---------------------------------------------------------------------------


def test_registry_row_to_subagent_translates_all_fields() -> None:
    row = {
        "user_id": "alice",
        "slug": "researcher",
        "description": "Looks up facts.",
        "system_prompt": "You are a researcher.",
        "tools": json.dumps(["web_search", "mcp__other__do_thing"]),
    }
    sub = _registry_row_to_subagent(row)
    # Bare names are rebadged ``mcp__telos__<name>`` (same rule as the
    # top-level allowed_tools); already-prefixed names pass through.
    assert sub == {
        "name": "researcher",
        "description": "Looks up facts.",
        "prompt": "You are a researcher.",
        "tools": ["mcp__telos__web_search", "mcp__other__do_thing"],
    }


def test_registry_row_to_subagent_rebadges_bare_tool_names() -> None:
    """Bare registry tool names would match nothing in Recordatio's
    AgentDefinition — they must carry the ``mcp__telos__`` prefix so the
    SDK resolves them against the telos MCP server."""
    row = {
        "slug": "researcher", "description": "", "system_prompt": "",
        "tools": json.dumps(["web_search", "create_routine"]),
    }
    sub = _registry_row_to_subagent(row)
    assert sub["tools"] == ["mcp__telos__web_search", "mcp__telos__create_routine"]


def test_registry_row_to_subagent_handles_empty_tools() -> None:
    row = {"slug": "minimal", "description": "", "system_prompt": "", "tools": ""}
    sub = _registry_row_to_subagent(row)
    assert sub["tools"] is None


def test_registry_row_to_subagent_handles_missing_optional_fields() -> None:
    row = {"slug": "bare"}
    sub = _registry_row_to_subagent(row)
    assert sub == {"name": "bare", "description": "", "prompt": "", "tools": None}


def test_registry_row_to_subagent_handles_invalid_tools_json() -> None:
    row = {"slug": "broken", "description": "", "system_prompt": "", "tools": "{not json"}
    sub = _registry_row_to_subagent(row)
    assert sub["tools"] is None


# ---------------------------------------------------------------------------
# build_agent_config_payload
# ---------------------------------------------------------------------------


def _make_registry(rows_by_pair: dict[tuple[str, str], dict[str, Any]],
                   specialists_by_user: dict[str, list[dict[str, Any]]]) -> MagicMock:
    registry = MagicMock()
    registry.get.side_effect = lambda uid, slug: rows_by_pair.get((uid, slug))
    registry.list_specialists.side_effect = lambda uid: specialists_by_user.get(uid, [])
    return registry


# Default kwargs every ``build_agent_config_payload`` call needs since the
# Phase-4-Phase-6 split. Spelt out once here so the original tests below stay
# focused on what they were testing (model, subagents, allowed-tools shape).
_MCP_KWARGS: dict[str, Any] = {
    "telos_mcp_url": "https://telos.example",
    "telos_mcp_auth_token": "tok-abc",
}


def test_build_payload_returns_none_when_no_registry_row() -> None:
    registry = _make_registry({}, {})
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="missing", model="claude-opus-4-7",
        **_MCP_KWARGS,
    )
    assert payload is None


def test_build_payload_for_specialist_has_empty_subagents() -> None:
    registry = _make_registry(
        {("alice", "researcher"): {
            "user_id": "alice", "slug": "researcher",
            "name": "Researcher", "description": "Finds facts.",
            "system_prompt": "You research.", "tools": json.dumps(["web_search"]),
        }},
        specialists_by_user={},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="researcher", model="claude-opus-4-7",
        **_MCP_KWARGS,
    )
    assert payload is not None
    assert payload["system_prompt"] == "You research."
    assert payload["model"] == "claude-opus-4-7"
    # Top-level allowed_tools are MCP-prefixed (see ``_telos_mcp_tool_name``).
    assert payload["allowed_tools"] == ["mcp__telos__web_search"]
    assert payload["subagents"] == []
    assert payload["version"] == 1


def test_build_payload_for_housekeeper_includes_specialists_as_subagents() -> None:
    registry = _make_registry(
        rows_by_pair={
            ("alice", HOUSEKEEPER_SLUG): {
                "user_id": "alice", "slug": HOUSEKEEPER_SLUG,
                "name": "Housekeeper", "description": "Meta-agent.",
                "system_prompt": "Coordinate specialists.",
                "tools": json.dumps(["delegate_to", "coordinate"]),
            },
        },
        specialists_by_user={"alice": [
            {"slug": "researcher", "description": "Finds facts.",
             "system_prompt": "Research.", "tools": json.dumps(["web_search"])},
            {"slug": "judge", "description": "Scores candidates.",
             "system_prompt": "Judge.", "tools": "[]"},
        ]},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug=HOUSEKEEPER_SLUG, model="claude-opus-4-7",
        **_MCP_KWARGS,
    )
    assert payload is not None
    assert len(payload["subagents"]) == 2
    by_name = {s["name"]: s for s in payload["subagents"]}
    # Subagent tools carry the same ``mcp__telos__`` rebadging as the
    # top-level allowed_tools.
    assert by_name["researcher"]["tools"] == ["mcp__telos__web_search"]
    assert by_name["judge"]["tools"] == []  # empty list != None — parsed JSON [] stays []


def test_build_payload_handles_missing_model() -> None:
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="x", model=None,
        **_MCP_KWARGS,
    )
    assert payload is not None
    assert payload["model"] == ""


# ---------------------------------------------------------------------------
# New: mcp_servers + allowed_tools wiring (P0a from PR #159 review)
# ---------------------------------------------------------------------------


def test_build_payload_populates_mcp_servers_with_telos_endpoint() -> None:
    """Without a non-empty ``mcp_servers``, ``allowed_tools`` would name
    tools that no server serves and the agent ends up with zero callable
    tools. The seed must point Recordatio at the telos MCP host."""
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="x", model="m",
        telos_mcp_url="https://telos.example",
        telos_mcp_auth_token="tok-abc",
    )
    assert payload is not None
    servers = payload["mcp_servers"]
    assert len(servers) == 1
    srv = servers[0]
    assert srv["name"] == "telos"
    assert srv["type"] == "http"
    assert srv["url"] == "https://telos.example/mcp/alice/x"
    # The baked header carries the per-(user, slug) token HMAC-derived
    # from the master secret — never the master secret itself — so this
    # config can only reach its own /mcp/alice/x surface.
    from telos.mcp.server import derive_agent_token

    expected = derive_agent_token("tok-abc", "alice", "x")
    assert srv["headers"]["Authorization"] == f"Bearer {expected}"
    assert "tok-abc" not in srv["headers"]["Authorization"]


def test_build_payload_omits_auth_header_when_no_token() -> None:
    """When telos hasn't generated/configured a token (only legal in
    test setups), don't emit a malformed ``Authorization: Bearer ``
    header."""
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="x", model="m",
        telos_mcp_url="http://telos.test",
        telos_mcp_auth_token=None,
    )
    assert payload is not None
    srv = payload["mcp_servers"][0]
    assert "headers" not in srv


def test_build_payload_rebadges_allowed_tools_with_mcp_prefix() -> None:
    """SDK matches tool names as ``mcp__<server>__<tool>`` — bare telos
    names would never resolve."""
    registry = _make_registry(
        {("alice", "hk"): {
            "slug": "hk", "system_prompt": "p",
            "tools": json.dumps(["web_search", "create_routine", "delegate_to"]),
        }},
        {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="hk", model="m",
        **_MCP_KWARGS,
    )
    assert payload is not None
    assert payload["allowed_tools"] == [
        "mcp__telos__web_search",
        "mcp__telos__create_routine",
        "mcp__telos__delegate_to",
    ]


def test_build_payload_preserves_already_prefixed_tool_names() -> None:
    """If a tool name is already MCP-prefixed (newer registry rows, or
    a tool from a different server), pass it through verbatim."""
    registry = _make_registry(
        {("alice", "x"): {
            "slug": "x", "system_prompt": "p",
            "tools": json.dumps(["web_search", "mcp__other__do_thing"]),
        }},
        {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="x", model="m",
        **_MCP_KWARGS,
    )
    assert payload is not None
    assert payload["allowed_tools"] == [
        "mcp__telos__web_search",
        "mcp__other__do_thing",
    ]


def test_build_payload_passes_native_sdk_tools_through_unrebadged() -> None:
    """SDK-native tools (Bash, Read, …) are the SDK's own, not telos MCP
    tools — they must pass through verbatim, while bare telos names still get
    the ``mcp__telos__`` prefix. This is what lets telos grant native tools."""
    registry = _make_registry(
        {("alice", "coder"): {
            "slug": "coder", "system_prompt": "p",
            "tools": json.dumps(["web_search", "Bash", "Read", "WebFetch"]),
        }},
        {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="coder", model="m",
        **_MCP_KWARGS,
    )
    assert payload is not None
    assert payload["allowed_tools"] == [
        "mcp__telos__web_search",
        "Bash",
        "Read",
        "WebFetch",
    ]


def test_build_payload_raises_when_telos_mcp_url_unknown() -> None:
    """Silently writing ``mcp_servers=[]`` would yield an agent with
    allowed_tools but no server to back them — caught at build time
    rather than discovered as 'tool calls silently do nothing' later."""
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    with pytest.raises(RuntimeError, match="telos MCP URL unknown"):
        build_agent_config_payload(
            registry=registry, user_id="alice", slug="x", model="m",
            telos_mcp_url="", telos_mcp_auth_token="t",
        )


def test_build_payload_strips_trailing_slash_from_telos_url() -> None:
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    payload = build_agent_config_payload(
        registry=registry, user_id="alice", slug="x", model="m",
        telos_mcp_url="https://telos.example/",
        telos_mcp_auth_token="t",
    )
    assert payload is not None
    assert payload["mcp_servers"][0]["url"] == "https://telos.example/mcp/alice/x"


# ---------------------------------------------------------------------------
# seed_agent_config_if_missing
# ---------------------------------------------------------------------------


def _make_resp(status: int, body: dict[str, Any] | None = None) -> MagicMock:
    """One canned httpx.Response: ``raise_for_status`` raises on >=400."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body or {}
    if status >= 400:
        err = httpx.HTTPStatusError(
            "boom",
            request=MagicMock(spec=httpx.Request),
            response=resp,
        )
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def _client_with(get_response: tuple[int, dict[str, Any] | None],
                 put_response: tuple[int, dict[str, Any]] | None = None) -> MagicMock:
    """Build a mock httpx.AsyncClient where .get() and .put() return
    pre-canned responses. ``get_response`` is ``(status_code, json_body)``;
    ``put_response`` is ``(status_code, json_body)`` or None (asserts no PUT).
    """
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(return_value=_make_resp(*get_response))
    if put_response is not None:
        client.put = AsyncMock(return_value=_make_resp(*put_response))
    else:
        client.put = AsyncMock(side_effect=AssertionError("PUT must not be called"))
    return client


@pytest.mark.asyncio
async def test_seed_creates_on_404() -> None:
    client = _client_with(get_response=(404, None), put_response=(200, {"version": 1}))
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, user_id="alice", slug="x", model="m",
        **_MCP_KWARGS,
    )
    assert created is True
    client.put.assert_awaited_once()
    # The PUT body should contain the seeded fields.
    put_kwargs = client.put.await_args.kwargs
    assert put_kwargs["json"]["system_prompt"] == "p"


@pytest.mark.asyncio
async def test_seed_skips_when_config_already_exists() -> None:
    client = _client_with(
        get_response=(200, {"system_prompt": "existing", "version": 5}),
        # put_response=None means PUT is asserted-not-called
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, user_id="alice", slug="x", model="m",
        **_MCP_KWARGS,
    )
    assert created is False
    client.put.assert_not_called()


@pytest.mark.asyncio
async def test_seed_skips_when_no_registry_row() -> None:
    client = _client_with(get_response=(404, None))
    registry = _make_registry({}, {})
    created = await seed_agent_config_if_missing(
        client, registry=registry, user_id="alice", slug="ghost", model="m",
        **_MCP_KWARGS,
    )
    assert created is False
    client.put.assert_not_called()


@pytest.mark.asyncio
async def test_seed_surfaces_non_404_get_errors() -> None:
    client = _client_with(get_response=(500, None))
    registry = _make_registry({}, {})
    with pytest.raises(httpx.HTTPStatusError):
        await seed_agent_config_if_missing(
            client, registry=registry, user_id="alice", slug="x", model="m",
            **_MCP_KWARGS,
        )


# ---------------------------------------------------------------------------
# Stale-auth-header refresh on existing configs (PR #162 review, P0:
# migration path for configs seeded before per-(user, slug) token binding).
# ---------------------------------------------------------------------------


def _existing_config(auth_header: str | None, *, version: int = 5) -> dict[str, Any]:
    """An already-seeded AgentConfig as Recordatio's GET would return it."""
    server: dict[str, Any] = {
        "name": "telos",
        "type": "http",
        "url": "https://telos.example/mcp/alice/x",
    }
    if auth_header is not None:
        server["headers"] = {"Authorization": auth_header, "X-Custom": "keep-me"}
    return {
        "system_prompt": "tuned by the improver — do not clobber",
        "mcp_servers": [server],
        "subagents": [{"name": "researcher", "description": "d", "prompt": "p", "tools": None}],
        "model": "claude-opus-4-7",
        "allowed_tools": ["mcp__telos__web_search"],
        "version": version,
    }


def _seq_client(get_responses: list[tuple[int, dict[str, Any] | None]],
                put_responses: list[tuple[int, dict[str, Any]]]) -> MagicMock:
    """Mock client where successive .get()/.put() calls walk the lists."""
    client = MagicMock(spec=httpx.AsyncClient)

    def make_resp(status: int, body: dict[str, Any] | None) -> MagicMock:
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status
        resp.json.return_value = body or {}
        if status >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "boom", request=MagicMock(spec=httpx.Request), response=resp,
            )
        else:
            resp.raise_for_status.return_value = None
        return resp

    client.get = AsyncMock(side_effect=[make_resp(*r) for r in get_responses])
    client.put = AsyncMock(side_effect=[make_resp(*r) for r in put_responses])
    return client


_SEED_REGISTRY_KW: dict[str, Any] = {
    "user_id": "alice", "slug": "x", "model": "m", **_MCP_KWARGS,
}


def _fresh_header() -> str:
    from telos.mcp.server import derive_agent_token

    return f"Bearer {derive_agent_token('tok-abc', 'alice', 'x')}"


@pytest.mark.asyncio
async def test_seed_refreshes_stale_auth_header_preserving_other_fields() -> None:
    """A config seeded under the OLD global-token scheme carries the
    master secret as its Authorization header; without a refresh it
    401s on every MCP call forever after deploy. The seed must PUT a
    header-only fix: every other field (improver-tuned prompt, model,
    subagents, extra headers, version) goes back verbatim."""
    stale = _existing_config("Bearer tok-abc", version=7)  # old global token
    client = _seq_client(
        get_responses=[(200, stale)],
        put_responses=[(200, {"version": 8})],
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, **_SEED_REGISTRY_KW,
    )
    assert created is False  # refresh, not a create
    client.put.assert_awaited_once()
    sent = client.put.await_args.kwargs["json"]
    # Header fixed to the derived per-(user, slug) token …
    srv = sent["mcp_servers"][0]
    assert srv["headers"]["Authorization"] == _fresh_header()
    # … sibling header survives …
    assert srv["headers"]["X-Custom"] == "keep-me"
    # … and EVERYTHING else is byte-identical, version carried for the
    # optimistic-concurrency check.
    assert sent["system_prompt"] == "tuned by the improver — do not clobber"
    assert sent["model"] == "claude-opus-4-7"
    assert sent["subagents"] == stale["subagents"]
    assert sent["allowed_tools"] == ["mcp__telos__web_search"]
    assert sent["version"] == 7
    # The original GET result was not mutated in place.
    assert stale["mcp_servers"][0]["headers"]["Authorization"] == "Bearer tok-abc"


@pytest.mark.asyncio
async def test_seed_no_put_when_auth_header_already_fresh() -> None:
    client = _client_with(
        get_response=(200, _existing_config(_fresh_header())),
        # put_response=None → PUT is asserted-not-called
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, **_SEED_REGISTRY_KW,
    )
    assert created is False
    client.put.assert_not_called()


@pytest.mark.asyncio
async def test_seed_refresh_retries_once_on_409() -> None:
    """409 = someone (the improver) bumped the version between our GET
    and PUT. Re-GET the current config and retry exactly once with the
    new version."""
    stale_v7 = _existing_config("Bearer tok-abc", version=7)
    current_v9 = _existing_config("Bearer tok-abc", version=9)
    client = _seq_client(
        get_responses=[(200, stale_v7), (200, current_v9)],
        put_responses=[(409, {"detail": "version conflict"}), (200, {"version": 10})],
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, **_SEED_REGISTRY_KW,
    )
    assert created is False
    assert client.get.await_count == 2
    assert client.put.await_count == 2
    retry_body = client.put.await_args_list[1].kwargs["json"]
    assert retry_body["version"] == 9
    assert retry_body["mcp_servers"][0]["headers"]["Authorization"] == _fresh_header()


@pytest.mark.asyncio
async def test_seed_refresh_surfaces_second_409() -> None:
    """One retry only — a second 409 propagates (something is fighting
    over the config; the next build will try again)."""
    stale = _existing_config("Bearer tok-abc", version=7)
    client = _seq_client(
        get_responses=[(200, stale), (200, _existing_config("Bearer tok-abc", version=9))],
        put_responses=[(409, {}), (409, {})],
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    with pytest.raises(httpx.HTTPStatusError):
        await seed_agent_config_if_missing(
            client, registry=registry, **_SEED_REGISTRY_KW,
        )
    assert client.put.await_count == 2


@pytest.mark.asyncio
async def test_seed_refresh_skips_without_master_secret() -> None:
    """No master secret → nothing to derive a fresh header from."""
    client = _client_with(get_response=(200, _existing_config("Bearer whatever")))
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, user_id="alice", slug="x", model="m",
        telos_mcp_url="https://telos.example", telos_mcp_auth_token=None,
    )
    assert created is False
    client.put.assert_not_called()


@pytest.mark.asyncio
async def test_seed_refresh_skips_config_without_telos_server_entry() -> None:
    """No telos server entry → no stale telos token to fix; the seed
    must not invent one on a config the improver owns."""
    client = _client_with(
        get_response=(200, {"system_prompt": "x", "mcp_servers": [
            {"name": "other", "type": "http", "url": "https://other.example"},
        ], "version": 3}),
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, **_SEED_REGISTRY_KW,
    )
    assert created is False
    client.put.assert_not_called()


@pytest.mark.asyncio
async def test_seed_refreshes_config_missing_headers_entirely() -> None:
    """A telos server entry with no headers at all (seeded when no
    token was configured) gets the derived header added."""
    client = _seq_client(
        get_responses=[(200, _existing_config(None, version=2))],
        put_responses=[(200, {"version": 3})],
    )
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, **_SEED_REGISTRY_KW,
    )
    assert created is False
    sent = client.put.await_args.kwargs["json"]
    assert sent["mcp_servers"][0]["headers"]["Authorization"] == _fresh_header()
    assert sent["version"] == 2


@pytest.mark.asyncio
async def test_seed_409_race_treated_as_already_seeded() -> None:
    """Two workers GET-404 then both PUT: the loser's 409 means the
    winner seeded successfully — re-GET and continue (return ``False``,
    'already existed'), don't fail closed with 'contact administrator'."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=[
        _make_resp(404),                              # our GET: missing
        _make_resp(200, {"system_prompt": "p", "version": 1}),  # re-GET after 409
    ])
    client.put = AsyncMock(return_value=_make_resp(409, {"error": "conflict"}))
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    created = await seed_agent_config_if_missing(
        client, registry=registry, user_id="alice", slug="x", model="m",
        **_MCP_KWARGS,
    )
    assert created is False
    assert client.get.await_count == 2  # confirmed the winner's config exists


@pytest.mark.asyncio
async def test_seed_409_without_existing_config_still_raises() -> None:
    """A 409 whose re-GET finds nothing is a genuine anomaly (version
    conflict on an empty key) — surface it rather than masking it."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(side_effect=[_make_resp(404), _make_resp(404)])
    client.put = AsyncMock(return_value=_make_resp(409, {"error": "conflict"}))
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    with pytest.raises(httpx.HTTPStatusError):
        await seed_agent_config_if_missing(
            client, registry=registry, user_id="alice", slug="x", model="m",
            **_MCP_KWARGS,
        )


@pytest.mark.asyncio
async def test_seed_surfaces_put_errors() -> None:
    client = _client_with(get_response=(404, None), put_response=(500, {"error": "boom"}))
    registry = _make_registry(
        {("alice", "x"): {"slug": "x", "system_prompt": "p", "tools": "[]"}}, {},
    )
    with pytest.raises(httpx.HTTPStatusError):
        await seed_agent_config_if_missing(
            client, registry=registry, user_id="alice", slug="x", model="m",
            **_MCP_KWARGS,
        )
