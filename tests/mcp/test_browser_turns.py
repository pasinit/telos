import asyncio

import pytest
from fastapi import FastAPI

from telos.domain.tools.web import BrowserOpenTool, BrowserReadTool
from telos.log import ConversationLog
from telos.mcp.browser_sessions import BrowserSessions
from telos.mcp.server import derive_agent_token, mount_mcp_host
from tests.mcp.conftest import mcp_client
from tests.mcp.test_host import _JSONRPC_HEADERS, _initialize, _parse_response


class FakeBrowser:
    def __init__(self):
        self.url = ""
        self.closed = False

    async def open(self, url):
        self.url = url
        return {"ok": True, "url": url, "content": "page", "images": ["data:image/png;base64,aW1hZ2U="]}

    async def read(self, offset=0):
        return {"ok": True, "url": self.url, "content": f"{self.url or 'empty'} offset={offset}"}

    async def close(self):
        self.closed = True


async def test_mcp_browser_turn_isolation_images_evidence_and_cleanup(tmp_path):
    app = FastAPI()
    host = mount_mcp_host(app, tools={"browser_open": BrowserOpenTool(lambda url: "127.0.0.1" in url),
                                     "browser_read": BrowserReadTool()}, storage_root=tmp_path, auth_token="secret")
    pool = BrowserSessions(FakeBrowser)
    host._browser_sessions = pool
    history = ConversationLog(tmp_path / "alice" / "agent" / "history.db")
    first = history.append("first", "", "sha")
    second = history.append("second", "", "sha")
    token = derive_agent_token("secret", "alice", "agent")
    async with mcp_client(app) as client:
        await _initialize(client, user_id="alice", slug="agent", token=token)
        async def call(turn, name, arguments):
            response = await client.post("/mcp/alice/agent", headers={**_JSONRPC_HEADERS, "Authorization": f"Bearer {token}",
                                        "X-Telos-Turn-Id": turn},
                                        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return _parse_response(response)["result"]
        opened = await call(first, "browser_open", {"url": "https://example.com/first"})
        assert not opened.get("isError")
        assert opened["content"][1] == {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"}
        assert "first" in (await call(first, "browser_read", {}))["content"][0]["text"]
        assert "offset=12000" in (await call(first, "browser_read", {"offset": 12000}))["content"][0]["text"]
        assert "empty" in (await call(second, "browser_read", {}))["content"][0]["text"]
        assert (await call(first, "browser_open", {"url": "http://127.0.0.1/private"}))["isError"]
        first_browser = pool._entries[("alice", "agent", first)].browser
        denied = await client.delete("/mcp/bob/agent", headers={"Authorization": f"Bearer {token}", "X-Telos-Turn-Id": first})
        assert denied.status_code == 401 and not first_browser.closed
        closed = await client.delete("/mcp/alice/agent", headers={"Authorization": f"Bearer {token}", "X-Telos-Turn-Id": first})
        assert closed.status_code == 200 and first_browser.closed
        evidence = history.execution_events.page(first, limit=20)
        assert evidence["total"] == 8
        assert evidence["events"][1]["payload"]["image_count"] == 1
        assert evidence["events"][-1]["payload"]["is_error"] is True
    assert not pool._entries


async def test_browser_pool_capacity_active_ttl_and_cancellation():
    pool = BrowserSessions(FakeBrowser, max_sessions=1, idle_seconds=0)
    key = ("u", "a", "t")
    with pytest.raises(asyncio.CancelledError):
        async with pool.session(key) as browser:
            await pool.reap()
            assert not browser.closed
            with pytest.raises(RuntimeError, match="capacity"):
                async with pool.session(("other", "a", "t")):
                    pass
            raise asyncio.CancelledError()
    await pool.reap()
    assert browser.closed and not pool._entries
