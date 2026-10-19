"""Unit tests for the web-family tools.

Each tool is constructor-injected with its dependency
(``WebSearchTool(search)``, ``BrowserOpenTool(is_private_url)``); the
per-call ``BrowserSession`` still travels through ``ToolContext``.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.tools import ToolContext
from telos.domain.tools.web import (
    BrowserBackTool,
    BrowserClickTool,
    BrowserFillTool,
    BrowserOpenTool,
    BrowserReadTool,
    WebSearchTool,
    _shape_browser_result,
)
from telos.transport.browser.images import is_private_url as _is_private_url
from tests._helpers import dispatch


def _ctx(tmp_path: Path, *, browser_session=None) -> ToolContext:
    return ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="test-agent",
        workspace=tmp_path,
        storage_root=tmp_path,
        browser_session=browser_session,
    )


# ──────────────────────────────────────────────────────────────────────
# web_search
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_routes_query_and_returns_text(tmp_path):
    search = AsyncMock(return_value=("results body", False))
    result = await dispatch(WebSearchTool(search), {"query": "best espresso machines"}, _ctx(tmp_path))
    search.assert_awaited_once_with("best espresso machines")
    assert result.text == "results body"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_web_search_propagates_error_flag(tmp_path):
    search = AsyncMock(return_value=("Search failed: boom", True))
    result = await dispatch(WebSearchTool(search), {"query": "x"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "boom" in result.text


# ──────────────────────────────────────────────────────────────────────
# _shape_browser_result
# ──────────────────────────────────────────────────────────────────────


def test_shape_browser_result_renders_success():
    res = _shape_browser_result({
        "ok": True,
        "url": "https://example.com",
        "title": "Example",
        "interactive": [{"kind": "link", "text": "Next"}],
        "content": "Hello",
        "images": ["data:image/png;base64,abc"],
    })
    assert res.is_error is False
    assert "URL: https://example.com" in res.text
    assert "Title: Example" in res.text
    assert "[link] Next" in res.text
    assert "Hello" in res.text
    assert "1 extracted images" in res.text
    assert res.images == ["data:image/png;base64,abc"]


def test_shape_browser_result_handles_failure():
    res = _shape_browser_result({"ok": False, "error": "page crashed"})
    assert res.is_error is True
    assert "[browser error]" in res.text
    assert "page crashed" in res.text


def test_shape_browser_result_handles_empty_content():
    res = _shape_browser_result({"ok": True, "url": "u", "content": ""})
    assert res.is_error is False
    assert "(empty)" in res.text
    assert res.images is None


# ──────────────────────────────────────────────────────────────────────
# browser_open — privacy guard
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_open_returns_unavailable_when_no_session(tmp_path):
    tool = BrowserOpenTool(_is_private_url)
    result = await dispatch(tool, {"url": "https://example.com"}, _ctx(tmp_path, browser_session=None))
    assert result.is_error is True
    assert "not available" in result.text


@pytest.mark.asyncio
async def test_browser_open_blocks_private_urls(tmp_path):
    """The private-URL guard must run *before* the session is touched."""
    session = MagicMock()
    session.open = AsyncMock(side_effect=AssertionError("session.open must not be called for private URLs"))
    tool = BrowserOpenTool(_is_private_url)
    with pytest.raises(ValueError, match="private/internal URLs"):
        await dispatch(tool, {"url": "http://127.0.0.1/admin"}, _ctx(tmp_path, browser_session=session))


@pytest.mark.asyncio
async def test_browser_open_forwards_to_session(tmp_path):
    session = MagicMock()
    session.open = AsyncMock(return_value={
        "ok": True, "url": "https://example.com", "title": "Ex", "content": "hi",
    })
    tool = BrowserOpenTool(_is_private_url)
    result = await dispatch(tool, {"url": "https://example.com"}, _ctx(tmp_path, browser_session=session))
    session.open.assert_awaited_once_with("https://example.com")
    assert result.is_error is False


# ──────────────────────────────────────────────────────────────────────
# browser_click / fill / read / back
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_click_forwards_target(tmp_path):
    session = MagicMock()
    session.click = AsyncMock(return_value={"ok": True, "url": "u", "content": ""})
    await dispatch(BrowserClickTool(), {"target": "Sign in"}, _ctx(tmp_path, browser_session=session))
    session.click.assert_awaited_once_with("Sign in")


@pytest.mark.asyncio
async def test_browser_fill_forwards_target_and_value(tmp_path):
    session = MagicMock()
    session.fill = AsyncMock(return_value={"ok": True, "url": "u", "content": ""})
    await dispatch(BrowserFillTool(), {"target": "Email", "value": "a@b.c"}, _ctx(tmp_path, browser_session=session),
    )
    session.fill.assert_awaited_once_with("Email", "a@b.c")


@pytest.mark.asyncio
@pytest.mark.parametrize('args,offset', [({}, 0), ({'offset': 12000}, 12000)])
async def test_browser_read_forwards_offset(tmp_path, args, offset):
    session = MagicMock()
    session.read = AsyncMock(return_value={"ok": True, "url": "u", "content": "page"})
    result = await dispatch(BrowserReadTool(), args, _ctx(tmp_path, browser_session=session))
    session.read.assert_awaited_once_with(offset=offset)
    assert "page" in result.text


@pytest.mark.asyncio
async def test_browser_back_takes_no_args(tmp_path):
    session = MagicMock()
    session.back = AsyncMock(return_value={"ok": True, "url": "u", "content": ""})
    await dispatch(BrowserBackTool(), {}, _ctx(tmp_path, browser_session=session))
    session.back.assert_awaited_once_with()


@pytest.mark.parametrize("tool_cls", [
    BrowserClickTool, BrowserFillTool, BrowserReadTool, BrowserBackTool,
])
@pytest.mark.asyncio
async def test_browser_action_returns_unavailable_without_session(tmp_path, tool_cls):
    """All four ``browser_*`` interaction tools gate on ``ctx.browser_session``."""
    tool = tool_cls()
    args = {"target": "x", "value": "y"} if tool_cls in (BrowserClickTool, BrowserFillTool) else {}
    result = await tool(args, _ctx(tmp_path, browser_session=None))
    assert result.is_error is True
    assert "not available" in result.text
