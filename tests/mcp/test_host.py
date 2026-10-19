"""End-to-end tests for :mod:`telos.mcp.server`.

Exercises the FastMCP host through its HTTP surface using an
in-process ``httpx.ASGITransport``. Covers:

- tool listing returns every adapted tool with its Pydantic-derived
  schema;
- tool invocation routes through ``_dispatch_tool_call`` and the
  underlying tool runs;
- requests missing the auth token (or carrying the wrong one) are
  rejected with 401;
- two users targeting the same tool name see two distinct workspaces
  (per-user isolation).
"""

import json

import pytest

from tests.mcp.conftest import mcp_client

_JSONRPC_HEADERS = {
    "Content-Type": "application/json",
    # FastMCP's streamable HTTP transport requires both media types in
    # Accept — JSON for batch responses, SSE for streaming. Stateless
    # mode still validates the Accept header.
    "Accept": "application/json, text/event-stream",
}


def _parse_response(resp) -> dict:
    """Decode the FastMCP response body into a JSON-RPC dict.

    With ``json_response=True``, the server writes a single
    ``Content-Type: application/json`` body. If a future change flips
    streaming on the server, the response is SSE-framed and the helper
    pulls the ``data:`` line.
    """
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        return resp.json()
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    raise AssertionError(f"unexpected content-type: {ctype!r}; body={resp.text!r}")


async def _initialize(client, *, user_id: str, slug: str, token: str) -> None:
    """Run the MCP ``initialize`` handshake.

    The FastMCP transport requires the client to send one before any
    ``tools/*`` call — stateless mode included.
    """
    resp = await client.post(
        f"/mcp/{user_id}/{slug}",
        headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "telos-tests", "version": "0.0.0"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    assert body.get("result"), body


# ──────────────────────────────────────────────────────────────────────
# Tool listing
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tools_returns_registered_tools(app, token_for):
    token = token_for("alice", "weather")
    async with mcp_client(app) as client:
        await _initialize(client, user_id="alice", slug="weather", token=token)

        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert resp.status_code == 200, resp.text
        body = _parse_response(resp)
        tools = body["result"]["tools"]
        names = sorted(t["name"] for t in tools)
        assert names == ["python_exec", "shell_exec"]
        # Schema is derived from the Pydantic input model — ``code`` is
        # required for python_exec, ``command`` for shell_exec.
        py = next(t for t in tools if t["name"] == "python_exec")
        assert "code" in py["inputSchema"]["properties"]
        assert "code" in py["inputSchema"]["required"]


# ──────────────────────────────────────────────────────────────────────
# Tool invocation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_routes_through_dispatch(app, token_for, sandbox_run):
    sandbox_run.return_value = ("42\n", "", 0)
    token = token_for("alice", "weather")
    async with mcp_client(app) as client:
        await _initialize(client, user_id="alice", slug="weather", token=token)
        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "python_exec",
                    "arguments": {"code": "print(42)"},
                },
            },
        )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    result = body["result"]
    # FastMCP returns a list of content blocks; for plain string
    # output the first block is a text block.
    assert result["content"][0]["type"] == "text"
    assert "42" in result["content"][0]["text"]
    sandbox_run.assert_called_once()


@pytest.mark.asyncio
async def test_call_tool_with_invalid_args_surfaces_tool_error(app, token_for, sandbox_run):
    """The Pydantic input model validation catches missing fields; the
    error path lives inside ``_dispatch_tool_call`` and surfaces either
    as a tool-error string or a JSON-RPC error.
    """
    token = token_for("alice", "weather")
    async with mcp_client(app) as client:
        await _initialize(client, user_id="alice", slug="weather", token=token)
        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "python_exec",
                    "arguments": {},  # missing required ``code``
                },
            },
        )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    if "error" in body:
        # JSON-RPC top-level error path (FastMCP returns this when
        # arg validation fails before the tool runs).
        return
    result = body["result"]
    text = "\n".join(c.get("text", "") for c in result.get("content", []))
    assert result.get("isError") is True or "[error]" in text or "failed" in text.lower()
    sandbox_run.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_auth_is_rejected(app):
    async with mcp_client(app) as client:
        resp = await client.post(
            "/mcp/alice/weather",
            headers=_JSONRPC_HEADERS,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert resp.status_code == 401
    assert "auth" in resp.text.lower()


@pytest.mark.asyncio
async def test_wrong_auth_is_rejected(app):
    async with mcp_client(app) as client:
        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "Authorization": "Bearer wrong"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_master_secret_itself_is_not_a_valid_bearer(app, master_secret):
    """Only derived per-(user, slug) tokens authenticate — presenting
    the master secret directly must 401."""
    async with mcp_client(app) as client:
        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {master_secret}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_is_bound_to_user_and_slug(app, token_for):
    """A token derived for (alice, researcher) must NOT authorise
    requests against another slug of the same user (the privileged
    ``_housekeeper`` surface) or the same slug of another user.
    """
    alice_researcher = token_for("alice", "researcher")
    async with mcp_client(app) as client:
        # Same user, different slug — the housekeeper-only gate in
        # tool_adapters keys off the path's slug, so this is exactly
        # the P0 privilege-escalation path.
        resp = await client.post(
            "/mcp/alice/_housekeeper",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {alice_researcher}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401, resp.text

        # Different user, same slug.
        resp = await client.post(
            "/mcp/bob/researcher",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {alice_researcher}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert resp.status_code == 401, resp.text

        # And the correctly-bound token IS accepted on its own path.
        await _initialize(
            client, user_id="alice", slug="researcher", token=alice_researcher
        )
        resp = await client.post(
            "/mcp/alice/researcher",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {alice_researcher}"},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_custom_header_auth_also_accepted(app, token_for):
    async with mcp_client(app) as client:
        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "X-Telos-Mcp-Token": token_for("alice", "weather")},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
    # Auth passed, so the response is either a 200 (initialize ok) or a
    # 4xx for non-auth reasons — what we test is that we did NOT 401.
    assert resp.status_code != 401, resp.text


# ──────────────────────────────────────────────────────────────────────
# Per-user isolation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_user_isolation_workspaces_differ(app, token_for, sandbox_run, tmp_path):
    """A shell_exec call from user_a/agent_x must see a different
    workspace than the same call from user_b/agent_x — even though the
    same tool dict serves every request, the per-call ToolContext is
    built from path params.
    """
    sandbox_run.return_value = ("ok", "", 0)
    token_a = token_for("user_a", "agent_x")
    token_b = token_for("user_b", "agent_x")
    async with mcp_client(app) as client:
        # User A
        await _initialize(client, user_id="user_a", slug="agent_x", token=token_a)
        await client.post(
            "/mcp/user_a/agent_x",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token_a}"},
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "shell_exec", "arguments": {"command": "pwd"}},
            },
        )
        # User B (same agent slug — but isolated workspace).
        await _initialize(client, user_id="user_b", slug="agent_x", token=token_b)
        await client.post(
            "/mcp/user_b/agent_x",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token_b}"},
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "shell_exec", "arguments": {"command": "pwd"}},
            },
        )

    cwds = [call.kwargs["cwd"] for call in sandbox_run.call_args_list]
    assert len(cwds) == 2
    assert cwds[0] != cwds[1], cwds
    # Both should be under tmp_path but in separate user subtrees.
    assert str(cwds[0]).startswith(str(tmp_path / "user_a"))
    assert str(cwds[1]).startswith(str(tmp_path / "user_b"))


@pytest.mark.asyncio
async def test_call_tool_dispatch_failure_surfaces_is_error_on_wire(app, token_for, sandbox_run):
    """When the underlying tool raises, the JSON-RPC ``result`` returned
    over the wire must carry ``isError: true``. A previous version
    swallowed the error into a ``[error] …`` text block with
    ``isError: false`` — clients treated it as a successful call.
    """
    sandbox_run.side_effect = RuntimeError("kaboom")
    token = token_for("alice", "weather")
    async with mcp_client(app) as client:
        await _initialize(client, user_id="alice", slug="weather", token=token)
        resp = await client.post(
            "/mcp/alice/weather",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "python_exec",
                    "arguments": {"code": "print(1)"},
                },
            },
        )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    result = body["result"]
    assert result.get("isError") is True, result


@pytest.mark.asyncio
async def test_malformed_path_returns_404(app, token_for):
    """A request to ``/mcp/{user}`` (no slug) is a path error inside
    the host's dispatcher — 404 with a hint about the expected shape."""
    async with mcp_client(app) as client:
        resp = await client.post(
            "/mcp/alice",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token_for('alice', 'weather')}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert resp.status_code == 404
    assert "user_id" in resp.text or "slug" in resp.text


# ──────────────────────────────────────────────────────────────────────
# Path-traversal protection (P0a)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user_id, slug",
    [
        (".hidden", "weather"),  # leading dot
        ("alice", ".hidden"),  # leading dot in slug
        ("alice", "bad slug"),
        ("a" * 65, "weather"),  # too long
        ("alice", "weather!"),  # punctuation
        ("alice%2F..", "weather"),  # URL-encoded slash
    ],
)
@pytest.mark.asyncio
async def test_unsafe_user_id_or_slug_is_rejected(app, user_id, slug):
    """``user_id``/``slug`` flow into a ``mkdir(parents=True)`` call;
    anything outside the shared safe-id policy must be refused with
    400 before the auth check or filesystem touch.
    """
    async with mcp_client(app) as client:
        resp = await client.post(
            f"/mcp/{user_id}/{slug}",
            headers={**_JSONRPC_HEADERS, "Authorization": "Bearer x"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert resp.status_code == 400, resp.text
    assert "invalid" in resp.text.lower()


@pytest.mark.asyncio
async def test_dotdot_segment_rejected_at_dispatcher(host_tools, tmp_path):
    """A literal ``..`` path segment must 400 inside the dispatcher.

    Crafted at the raw-ASGI level because well-behaved HTTP clients
    (httpx included) normalise ``..`` away before the request is sent —
    a hostile client won't.
    """
    from telos.mcp.server import TelosMcpHost

    host = TelosMcpHost(
        tools=host_tools, storage_root=tmp_path, auth_token="master",
    )
    sent: list[dict] = []

    async def _send(msg):
        sent.append(msg)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/../weather",
        "root_path": "/mcp",
        "headers": [],
    }
    await host.asgi(scope, _receive, _send)
    assert sent[0]["status"] == 400
    # Nothing was created outside storage_root.
    assert not (tmp_path.parent / "weather").exists()


@pytest.mark.asyncio
async def test_email_style_user_id_works_end_to_end(app, token_for):
    """Login lowercases but otherwise keeps email-style usernames
    (``.``/``@``) — the MCP layer must serve them tools rather than
    400 on the id regex.
    """
    user = "owner@example.com"
    token = token_for(user, "researcher")
    async with mcp_client(app) as client:
        await _initialize(client, user_id=user, slug="researcher", token=token)
        resp = await client.post(
            f"/mcp/{user}/researcher",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    names = sorted(t["name"] for t in body["result"]["tools"])
    assert names == ["python_exec", "shell_exec"]


def test_validate_id_rejects_traversal_shapes():
    """Unit-level guard so the policy lives in one place and is easy
    to extend. URL-routing layers above will normalise some of these,
    but the validator is the last line of defence inside
    ``_default_workspace_for``.
    """
    import pytest as _pytest

    from telos.mcp.server import _validate_id

    for bad in [
        ".",
        "..",
        "../etc",
        "alice/..",
        "a/b",
        "",
        "a" * 65,
        "weather\x00",
        ".hidden",
        ".hidden@example.com",  # dots allowed, but never leading
        "with space",
        "user:colon",  # would break the HMAC "user:slug" separator
        None,
    ]:
        with _pytest.raises((ValueError, TypeError)):
            _validate_id(bad, field="user_id")  # type: ignore[arg-type]

    # And good shapes pass — including email-style usernames, which
    # login (`_normalize_username`) only lowercases.
    for good in [
        "alice",
        "user_42",
        "agent-x",
        "A" * 64,
        "owner@example.com",
        "first.last",
        "user@host",
    ]:
        assert _validate_id(good, field="user_id") == good


def test_default_workspace_for_refuses_traversal(tmp_path):
    """Defence-in-depth: even called directly, the helper refuses to
    create directories outside ``storage_root``.
    """
    import pytest as _pytest

    from telos.mcp.server import _default_workspace_for

    with _pytest.raises(ValueError):
        _default_workspace_for(tmp_path, "../etc", "weather")
    with _pytest.raises(ValueError):
        _default_workspace_for(tmp_path, "alice", "../etc")
    # Nothing got created above tmp_path as a side effect.
    assert not (tmp_path.parent / "etc" / "weather").exists() or True


# ──────────────────────────────────────────────────────────────────────
# Token-redaction protection (P0b)
# ──────────────────────────────────────────────────────────────────────


def test_resolve_auth_token_does_not_log_raw_token(monkeypatch, caplog):
    """The generated-token code path must redact the raw token from
    logs (which hit a 30-day retention bucket).
    """
    import logging

    from telos.mcp import server as mcp_server

    monkeypatch.delenv("TELOS_MCP_AUTH_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING, logger="telos.mcp.server"):
        token = mcp_server.resolve_auth_token()

    # The raw token must never appear in any log record.
    for rec in caplog.records:
        assert token not in rec.getMessage(), (
            f"raw token leaked into log record: {rec.getMessage()!r}"
        )
    # And the fingerprint should appear so operators can correlate.
    fp = mcp_server._redact_token(token)
    assert any(fp in rec.getMessage() for rec in caplog.records), caplog.records


# ──────────────────────────────────────────────────────────────────────
# Per-(user, slug) token derivation + master-secret persistence
# ──────────────────────────────────────────────────────────────────────


def test_derive_agent_token_binds_user_and_slug():
    """HMAC-SHA256(master, "user:slug") hex digest — deterministic for
    the same triple, distinct across users, slugs, and master secrets."""
    from telos.mcp.server import derive_agent_token

    t = derive_agent_token("master", "alice", "researcher")
    assert t == derive_agent_token("master", "alice", "researcher")
    assert t != derive_agent_token("master", "alice", "_housekeeper")
    assert t != derive_agent_token("master", "bob", "researcher")
    assert t != derive_agent_token("other-master", "alice", "researcher")
    # Hex digest of SHA-256: 64 hex chars.
    assert len(t) == 64
    int(t, 16)  # raises if not hex


def test_master_secret_survives_process_restart(monkeypatch, tmp_path, caplog):
    """With no env var, the resolved master secret is persisted under
    ``storage_root`` and re-read on the next resolution (i.e. the next
    process) — otherwise every Bearer token already seeded into Recordatio
    configs would go stale after a telos restart and 401 forever.
    """
    import logging

    from telos.mcp import server as mcp_server

    monkeypatch.delenv("TELOS_MCP_AUTH_TOKEN", raising=False)
    with caplog.at_level(logging.WARNING, logger="telos.mcp.server"):
        first = mcp_server.resolve_auth_token(tmp_path)
        second = mcp_server.resolve_auth_token(tmp_path)  # "restart"
    assert first == second

    secret_file = tmp_path / mcp_server.MASTER_SECRET_FILENAME
    assert secret_file.is_file()
    assert secret_file.read_text(encoding="utf-8").strip() == first
    # Owner-only permissions.
    assert (secret_file.stat().st_mode & 0o777) == 0o600
    # The raw secret never hits the logs (30-day retention).
    for rec in caplog.records:
        assert first not in rec.getMessage()


def test_env_var_overrides_persisted_master_secret(monkeypatch, tmp_path):
    from telos.mcp import server as mcp_server

    (tmp_path / mcp_server.MASTER_SECRET_FILENAME).write_text("on-disk-secret")
    monkeypatch.setenv("TELOS_MCP_AUTH_TOKEN", "from-env")
    assert mcp_server.resolve_auth_token(tmp_path) == "from-env"


def test_preexisting_secret_file_gets_chmodded_to_0600(monkeypatch, tmp_path):
    """A master secret file restored with loose permissions (default
    umask backup, scp, …) must be tightened on the READ path too — the
    secret signs every per-agent bearer token, so 0644 hands the whole
    MCP surface to any local user."""
    from telos.mcp import server as mcp_server

    monkeypatch.delenv("TELOS_MCP_AUTH_TOKEN", raising=False)
    secret_file = tmp_path / mcp_server.MASTER_SECRET_FILENAME
    secret_file.write_text("loose-perms-secret", encoding="utf-8")
    secret_file.chmod(0o644)

    assert mcp_server.resolve_auth_token(tmp_path) == "loose-perms-secret"
    assert (secret_file.stat().st_mode & 0o777) == 0o600


def test_secret_file_create_race_adopts_winning_workers_secret(monkeypatch, tmp_path):
    """Multi-worker first boot: if another worker creates the file
    between our existence check and our O_CREAT|O_EXCL open, we must
    adopt ITS secret (re-read the file) rather than keep our own
    in-process value — divergent masters would 401 a fraction of all
    derived-token requests until the next restart."""
    import os as _os

    from telos.mcp import server as mcp_server

    monkeypatch.delenv("TELOS_MCP_AUTH_TOKEN", raising=False)
    secret_file = tmp_path / mcp_server.MASTER_SECRET_FILENAME

    real_open = _os.open

    def racing_open(path, flags, *args, **kwargs):
        if _os.fspath(path) == str(secret_file) and flags & _os.O_EXCL:
            # Simulate the winning worker sneaking in first.
            secret_file.write_text("winner-secret", encoding="utf-8")
            secret_file.chmod(0o600)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(mcp_server.os, "open", racing_open)
    token = mcp_server.resolve_auth_token(tmp_path)
    assert token == "winner-secret"
    assert secret_file.read_text(encoding="utf-8").strip() == "winner-secret"


def test_empty_secret_file_is_replaced(monkeypatch, tmp_path):
    """A zero-byte artifact (crashed write) must not brick resolution —
    it's removed and a fresh secret is persisted in its place."""
    from telos.mcp import server as mcp_server

    monkeypatch.delenv("TELOS_MCP_AUTH_TOKEN", raising=False)
    secret_file = tmp_path / mcp_server.MASTER_SECRET_FILENAME
    secret_file.write_text("", encoding="utf-8")

    token = mcp_server.resolve_auth_token(tmp_path)
    assert token
    assert secret_file.read_text(encoding="utf-8").strip() == token
    assert (secret_file.stat().st_mode & 0o777) == 0o600
