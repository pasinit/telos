"""Web-family tools — ``web_search`` and the ``browser_*`` group.

``WebSearchTool`` takes the search callable via ``__init__``;
``BrowserOpenTool`` takes a ``(url) -> bool`` private-URL predicate so it
can refuse private/internal URLs without reaching for module globals.
The per-turn :class:`~telos.browser.BrowserSession` is per-call state
and lives on :class:`~telos.domain.tools.ToolContext`.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# web_search
# ──────────────────────────────────────────────────────────────────────


class WebSearchInput(BaseModel):
    query: str = Field(description="The search query.")


class WebSearchTool:
    """``web_search`` — query the configured search engine."""

    name = "web_search"
    description = "Search the web for current information."

    def __init__(self, search: Callable[[str], Awaitable[tuple[str, bool]]]) -> None:
        self._search = search

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: WebSearchInput, ctx: ToolContext) -> ToolResult:
        text, is_error = await self._search(inp.query)
        return ToolResult(text=text, is_error=is_error)


# ──────────────────────────────────────────────────────────────────────
# browser_* — shared rendering + open / click / fill / read / back
# ──────────────────────────────────────────────────────────────────────


def _shape_browser_result(res: dict[str, Any]) -> ToolResult:
    """Render a :class:`BrowserSession` response into a ``ToolResult``."""
    if not res.get("ok"):
        return ToolResult(
            text=f"[browser error] {res.get('error', 'unknown error')}",
            is_error=True,
        )
    parts = [f"URL: {res['url']}"]
    if res.get("title"):
        parts.append(f"Title: {res['title']}")
    if res.get("interactive"):
        lines = [f"  - [{i['kind']}] {i['text']}" + (f" -> {i['href']}" if i.get('href') else '') for i in res["interactive"]]
        parts.append("Interactive elements:\n" + "\n".join(lines))
    parts.append("Content:\n" + (res.get("content") or "(empty)"))
    if pagination := res.get("pagination"):
        parts.append(f"Content characters: {pagination['offset']}..{pagination['offset'] + len(res.get('content', ''))} of {pagination['total_chars']}.")
        if pagination.get("next_offset") is not None:
            parts.append(f"More content available: call browser_read with offset={pagination['next_offset']}.")
        if pagination.get("source_truncated"):
            parts.append("Source exceeded the extraction limit; remaining content is not available and is not evidence of absence.")
    images = res.get("images") or []
    if images:
        parts.append(f"[This page contains {len(images)} extracted images attached below.]")
    return ToolResult(
        text="\n\n".join(parts),
        is_error=False,
        images=list(images) if images else None,
    )


_BROWSER_UNAVAILABLE = ToolResult(
    text="browser session not available — agent has no browser tools enabled",
    is_error=True,
)


class BrowserOpenInput(BaseModel):
    url: str = Field(description="Absolute URL to navigate to.")


class BrowserOpenTool:
    """``browser_open`` — navigate the stateful session to a URL.

    Refuses private / internal URLs (link-local, RFC1918, loopback) to
    prevent the LLM from probing the host's internal network. The
    predicate is constructor-injected so the security gate is bound at
    wiring time, never reached for at call time.
    """

    name = "browser_open"
    description = (
        "Open a URL or PDF attachment in the browser session. Returns the title, "
        "main content, and a list of interactive elements (links, buttons, "
        "form fields) the model can target with browser_click / browser_fill. "
        "Use browser_read with the reported next_offset to retrieve remaining text."
    )

    def __init__(self, is_private_url: Callable[[str], bool]) -> None:
        self._is_private_url = is_private_url

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: BrowserOpenInput, ctx: ToolContext) -> ToolResult:
        if ctx.browser_session is None:
            return _BROWSER_UNAVAILABLE
        if self._is_private_url(inp.url):
            raise ValueError("Blocked: cannot open private/internal URLs")
        return _shape_browser_result(await ctx.browser_session.open(inp.url))


class BrowserClickInput(BaseModel):
    target: str = Field(description="Visible text or CSS selector identifying the element to click.")


class BrowserClickTool:
    """``browser_click`` — click a link/button by visible text or selector."""

    name = "browser_click"
    description = "Click a link or button on the currently open page."

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: BrowserClickInput, ctx: ToolContext) -> ToolResult:
        if ctx.browser_session is None:
            return _BROWSER_UNAVAILABLE
        return _shape_browser_result(await ctx.browser_session.click(inp.target))


class BrowserFillInput(BaseModel):
    target: str = Field(description="Visible label or CSS selector of the input field.")
    value: str = Field(description="Text to type into the field.")


class BrowserFillTool:
    """``browser_fill`` — fill an input field by visible text or selector."""

    name = "browser_fill"
    description = "Fill an input field on the currently open page."

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: BrowserFillInput, ctx: ToolContext) -> ToolResult:
        if ctx.browser_session is None:
            return _BROWSER_UNAVAILABLE
        return _shape_browser_result(await ctx.browser_session.fill(inp.target, inp.value))


class BrowserReadInput(BaseModel):
    offset: int = Field(default=0, ge=0, description="Character offset from the previous result. Use next_offset to read the remainder of a page or PDF.")


class BrowserReadTool:
    """``browser_read`` — re-read the currently open page (post-interaction)."""

    name = "browser_read"
    description = "Read the current page or PDF. Use the reported next_offset to retrieve remaining content; offset=0 refreshes an HTML page."

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: BrowserReadInput, ctx: ToolContext) -> ToolResult:
        if ctx.browser_session is None:
            return _BROWSER_UNAVAILABLE
        return _shape_browser_result(await ctx.browser_session.read(offset=inp.offset))


class BrowserBackInput(BaseModel):
    """No arguments — navigate one step back in history."""


class BrowserBackTool:
    """``browser_back`` — navigate back to the previous page in history."""

    name = "browser_back"
    description = "Navigate back to the previously visited page."

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: BrowserBackInput, ctx: ToolContext) -> ToolResult:
        if ctx.browser_session is None:
            return _BROWSER_UNAVAILABLE
        return _shape_browser_result(await ctx.browser_session.back())
