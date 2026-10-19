"""Per-agent scoping of the MCP host's ``tools/list``.

The host advertises, on each ``/mcp/{user_id}/{slug}`` surface, only
the tools that agent owns in its registry row (``agents.tools`` JSON)
— defence-in-depth on top of the SDK-side ``allowed_tools`` whitelist,
which remains the invocation gate. Covers:

- a specialist surface lists exactly its registry subset;
- the housekeeper surface is unchanged (full registered set);
- an unknown agent advertises nothing (fail closed, no new HTTP
  semantics);
- invoking a registered-but-unadvertised tool still SUCCEEDS — the
  only MCP-side invocation gate is the existing housekeeper-only
  check, and listing scope must not tighten it (belt and braces live
  in the SDK whitelist).
"""

import pytest
from fastapi import FastAPI

from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.mcp.server import mount_mcp_host
from telos.mcp.tool_adapters import scoped_tool_names
from tests.mcp.conftest import mcp_client
from tests.mcp.test_host import _JSONRPC_HEADERS, _initialize, _parse_response


class _FakeRegistry:
    """Duck-typed stand-in for ``telos.api.registry.AgentRegistry``.

    Only ``get`` is consulted by the listing scope; rows mirror the
    ``registry.db`` shape — ``tools`` is a JSON-encoded list, exactly
    what :class:`telos.core.repos.registry.RegistryRepo` returns.
    """

    def __init__(self, rows: dict[tuple[str, str], dict]) -> None:
        self._rows = rows

    def get(self, user_id: str, slug: str) -> dict | None:
        return self._rows.get((user_id, slug))


@pytest.fixture
def registry() -> _FakeRegistry:
    return _FakeRegistry({
        # homey owns only python_exec, although the host registers
        # python_exec AND shell_exec.
        ("alice", "homey"): {"tools": '["python_exec"]'},
    })


@pytest.fixture
def scoped_app(tmp_path, host_tools, master_secret, registry) -> FastAPI:
    """Like the conftest ``app`` fixture, but with a registry wired so
    tool listing is scoped per agent."""
    fastapi_app = FastAPI()
    mount_mcp_host(
        fastapi_app,
        tools=host_tools,
        storage_root=tmp_path,
        auth_token=master_secret,
        registry=registry,
    )
    return fastapi_app


async def _list_tool_names(client, *, user_id: str, slug: str, token: str) -> list[str]:
    await _initialize(client, user_id=user_id, slug=slug, token=token)
    resp = await client.post(
        f"/mcp/{user_id}/{slug}",
        headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert resp.status_code == 200, resp.text
    body = _parse_response(resp)
    return sorted(t["name"] for t in body["result"]["tools"])


@pytest.mark.asyncio
async def test_specialist_surface_lists_only_its_registry_subset(scoped_app, token_for):
    """homey's registry row whitelists python_exec only — its surface
    must advertise exactly that, not the host's full registered set."""
    async with mcp_client(scoped_app) as client:
        names = await _list_tool_names(
            client, user_id="alice", slug="homey", token=token_for("alice", "homey"),
        )
    assert names == ["python_exec"]


@pytest.mark.asyncio
async def test_housekeeper_surface_still_lists_full_registered_set(scoped_app, token_for):
    """The housekeeper keeps its full set (incl. delegation tools when
    registered) — its surface is deliberately NOT registry-scoped."""
    token = token_for("alice", HOUSEKEEPER_SLUG)
    async with mcp_client(scoped_app) as client:
        names = await _list_tool_names(
            client, user_id="alice", slug=HOUSEKEEPER_SLUG, token=token,
        )
    assert names == ["python_exec", "shell_exec"]


@pytest.mark.asyncio
async def test_unknown_agent_surface_lists_no_tools(scoped_app, token_for):
    """No registry row → fail closed: the surface advertises nothing
    (same 200 ``tools/list`` shape — no new 404 semantics)."""
    async with mcp_client(scoped_app) as client:
        names = await _list_tool_names(
            client, user_id="alice", slug="ghost", token=token_for("alice", "ghost"),
        )
    assert names == []


@pytest.mark.asyncio
async def test_calling_tool_outside_allowlist_is_denied_at_invocation(
    scoped_app, token_for, sandbox_run,
):
    """shell_exec is registered but NOT in homey's registry allowlist.

    telos enforces per-agent tool ownership at its OWN MCP boundary now
    (not only at listing), so the call is denied here — without relying on
    Recordatio's SDK-side ``allowed_tools``. The tool never runs.
    """
    sandbox_run.return_value = ("ok", "", 0)
    token = token_for("alice", "homey")
    async with mcp_client(scoped_app) as client:
        await _initialize(client, user_id="alice", slug="homey", token=token)
        resp = await client.post(
            "/mcp/alice/homey",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "shell_exec", "arguments": {"command": "pwd"}},
            },
        )
    assert resp.status_code == 200, resp.text
    result = _parse_response(resp)["result"]
    assert result.get("isError") is True, result
    assert "allowlist" in str(result).lower(), result
    sandbox_run.assert_not_called()


@pytest.mark.asyncio
async def test_calling_tool_in_allowlist_succeeds(scoped_app, token_for, sandbox_run):
    """python_exec IS in homey's registry allowlist — the invocation gate
    lets it through and the tool runs."""
    sandbox_run.return_value = ("ok", "", 0)
    token = token_for("alice", "homey")
    async with mcp_client(scoped_app) as client:
        await _initialize(client, user_id="alice", slug="homey", token=token)
        resp = await client.post(
            "/mcp/alice/homey",
            headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "python_exec", "arguments": {"code": "print(1)"}},
            },
        )
    assert resp.status_code == 200, resp.text
    result = _parse_response(resp)["result"]
    assert result.get("isError") is not True, result
    sandbox_run.assert_called_once()


# ──────────────────────────────────────────────────────────────────────
# Unit-level: the scoping policy itself
# ──────────────────────────────────────────────────────────────────────


def test_scoped_tool_names_drops_housekeeper_only_even_if_whitelisted():
    """A specialist row that (mis)whitelists delegate_to must not get it
    advertised — the existing invocation gate would deny it anyway."""
    reg = _FakeRegistry({
        ("alice", "homey"): {"tools": '["python_exec", "delegate_to"]'},
    })
    names = scoped_tool_names(
        user_id="alice",
        slug="homey",
        registered={"python_exec", "shell_exec", "delegate_to"},
        registry=reg,
    )
    assert names == {"python_exec"}


def test_scoped_tool_names_housekeeper_keeps_delegation_tools():
    names = scoped_tool_names(
        user_id="alice",
        slug=HOUSEKEEPER_SLUG,
        registered={"python_exec", "delegate_to", "create_agent"},
        registry=_FakeRegistry({}),
    )
    assert names == {"python_exec", "delegate_to", "create_agent"}


def test_scoped_tool_names_without_registry_keeps_legacy_listing_minus_hk_only():
    """No registry wired (embedders/tests) → previous behaviour, except
    housekeeper-only tools disappear from non-housekeeper surfaces."""
    names = scoped_tool_names(
        user_id="alice",
        slug="homey",
        registered={"python_exec", "shell_exec", "delegate_to"},
        registry=None,
    )
    assert names == {"python_exec", "shell_exec"}


def test_scoped_tool_names_unparseable_tools_column_fails_closed():
    reg = _FakeRegistry({("alice", "homey"): {"tools": "not json"}})
    names = scoped_tool_names(
        user_id="alice", slug="homey", registered={"python_exec"}, registry=reg,
    )
    assert names == set()
