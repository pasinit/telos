"""Shared fixtures + helpers for the MCP-host tests.

We expose plain (sync) fixtures that build the FastAPI app, the tool
catalogue, and a few constants. The lifespan + httpx client are *not*
fixtures — they're an async context manager (:func:`mcp_client`)
because the FastMCP session manager's internal anyio task group must
be entered and exited inside the SAME asyncio task. pytest-asyncio's
async-generator fixtures hop tasks between setup, yield, and teardown,
which breaks anyio's cancel-scope invariant. Each test instead opens
``mcp_client(app)`` directly inside its own coroutine, so setup and
teardown stay on one task.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import FastAPI

from telos.domain.tools import (
    PythonExecInput,  # noqa: F401  (re-exported by tests as needed)
    PythonExecTool,
    ShellExecTool,
)
from telos.mcp.server import derive_agent_token, mount_mcp_host


@pytest.fixture
def sandbox_run():
    """An ``AsyncMock`` that captures every sandbox invocation.

    Tests assert against ``.call_args_list`` to verify per-user
    isolation (the workspace passed to the sandbox is per (user, slug)).
    """
    return AsyncMock(return_value=("ok", "", 0))


@pytest.fixture
def host_tools(sandbox_run):
    """The tool dict the host will register.

    Only sandbox-backed tools today — they exercise the request
    plumbing without pulling in the full production wiring (Pontifex,
    httpx for Google APIs, etc.).
    """
    return {
        "python_exec": PythonExecTool(sandbox_run),
        "shell_exec": ShellExecTool(sandbox_run),
    }


@pytest.fixture
def master_secret() -> str:
    """The MASTER secret the host is configured with.

    Never a valid bearer token by itself — per-request tokens are
    HMAC-derived from it per (user_id, slug); see ``token_for``.
    """
    return "test-master-secret-deadbeef"


@pytest.fixture
def token_for(master_secret):
    """Callable fixture: ``token_for(user_id, slug)`` → the bearer token
    valid for exactly that ``/mcp/{user_id}/{slug}`` surface.
    """
    def _derive(user_id: str, slug: str) -> str:
        return derive_agent_token(master_secret, user_id, slug)

    return _derive


@pytest.fixture
def app(tmp_path: Path, host_tools, master_secret) -> FastAPI:
    """A FastAPI app with the MCP host mounted at ``/mcp``."""
    fastapi_app = FastAPI()
    mount_mcp_host(
        fastapi_app,
        tools=host_tools,
        storage_root=tmp_path,
        auth_token=master_secret,
    )
    return fastapi_app


@asynccontextmanager
async def mcp_client(app) -> AsyncIterator[httpx.AsyncClient]:
    """Open the MCP host's lifespan + an ``httpx.AsyncClient`` in one
    task, so anyio cancel scopes inside the FastMCP session manager
    stay on the same asyncio task across setup and teardown.

    Usage in a test::

        @pytest.mark.asyncio
        async def test_something(app, auth_token):
            async with mcp_client(app) as client:
                resp = await client.post("/mcp/u/s", ...)
    """
    async with _run_asgi_lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c:
            yield c


@asynccontextmanager
async def _run_asgi_lifespan(app):
    """Minimal ASGI lifespan driver. Enters ``startup`` before the
    first request, runs ``shutdown`` after. Uses an anyio task group
    so the FastMCP session manager's internal task group has a parent
    to attach to.
    """
    startup_send, startup_recv = anyio.create_memory_object_stream[dict](10)
    out_send, out_recv = anyio.create_memory_object_stream[dict](10)

    async def _receive():
        return await startup_recv.receive()

    async def _send(msg):
        await out_send.send(msg)

    async with anyio.create_task_group() as tg:
        async def _run_app():
            await app({"type": "lifespan"}, _receive, _send)

        tg.start_soon(_run_app)
        await startup_send.send({"type": "lifespan.startup"})
        msg = await out_recv.receive()
        if msg["type"] != "lifespan.startup.complete":
            raise RuntimeError(f"lifespan startup failed: {msg}")
        try:
            yield
        finally:
            await startup_send.send({"type": "lifespan.shutdown"})
            msg = await out_recv.receive()
            if msg["type"] != "lifespan.shutdown.complete":
                raise RuntimeError(f"lifespan shutdown failed: {msg}")
            await startup_send.aclose()
            await out_send.aclose()
