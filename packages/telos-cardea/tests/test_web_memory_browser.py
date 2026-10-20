from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from telos_cardea import browser, memory, web
from telos_cardea._cardea import OperationContext, OperationRejected
from telos_cardea.config import Config

from conftest import assert_input, assert_output

MakeContext = Callable[..., OperationContext]


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── web.search ─────────────────────────────────────────────────────────


async def test_serper_results(config: Config, make_context: MakeContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "serper-key"
        return httpx.Response(200, json={"organic": [{"title": "Flat", "link": "https://example.com/1", "snippet": "2 bed"}]})

    assert_input("web.search", {"query": "flats in W2"})
    output = await web.search({"query": "flats in W2"}, make_context(), config=config, client=mock_client(handler))
    assert output == {"engine": "serper", "status": "results", "results": [{"title": "Flat", "url": "https://example.com/1", "snippet": "2 bed"}]}
    assert_output("web.search", output)


async def test_serper_failure_falls_back_to_free_search(config: Config, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [{"title": "Ad", "href": "https://www.bing.com/aclick?x", "body": ""},
            {"title": "Real", "href": "https://rightmove.co.uk/p/1", "body": "snip"}]
    monkeypatch.setattr(web, "_ddgs", lambda query, backend: hits)
    client = mock_client(lambda request: httpx.Response(400, json={"message": "Not enough credits"}))
    output = await web.search({"query": "flat"}, make_context(), config=config, client=client)
    assert output["engine"] == "duckduckgo"
    assert output["results"] == [{"title": "Real", "url": "https://rightmove.co.uk/p/1", "snippet": "snip"}]
    assert_output("web.search", output)


async def test_free_search_unavailable_is_reported(config: Config, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(query: str, backend: str) -> list[dict[str, Any]]:
        raise RuntimeError("blocked")

    monkeypatch.setattr(web, "_ddgs", broken)
    output = await web.search({"query": "flat"}, make_context(), config=replace(config, search_engine="bing"))
    assert output["engine"] == "bing" and output["status"] == "unavailable"
    assert_output("web.search", output)


def test_site_restrictions_are_enforced() -> None:
    results = [{"href": "https://www.foxtons.co.uk/a"}, {"href": "https://other.com/b"}]
    assert web.filter_results("flats site:foxtons.co.uk", results) == [results[0]]
    assert web.filter_results("flats -site:foxtons.co.uk", results) == [results[1]]


# ── memory.recall ──────────────────────────────────────────────────────


async def test_recall_uses_namespace_scope(config: Config, make_context: MakeContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": [{"text": "Viewed 12 Foo St"}]})

    args = {"query": "12 Foo Street", "k": 5}
    assert_input("memory.recall", args)
    output = await memory.recall(args, make_context(), config=config, client=mock_client(handler))
    assert output == {"query": "12 Foo Street", "count": 1, "recordatio": {"results": [{"text": "Viewed 12 Foo St"}]}}
    assert_output("memory.recall", output)
    request = seen[0]
    assert str(request.url).startswith("http://recordatio.test:8787/v1/users/owner/memory-search?")
    assert request.url.params["slug"] == "homey" and request.url.params["k"] == "5" and request.url.params["mode"] == "production"
    assert request.headers["Authorization"] == "Bearer chat-key"


async def test_recall_refuses_unconfigured_namespace(config: Config, make_context: MakeContext) -> None:
    with pytest.raises(OperationRejected):
        await memory.recall({"query": "x"}, make_context(namespace="someone-else"), config=config,
                            client=mock_client(lambda r: httpx.Response(200, json={})))


async def test_recall_http_error_raises(config: Config, make_context: MakeContext) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        await memory.recall({"query": "x"}, make_context(), config=config, client=mock_client(lambda r: httpx.Response(503)))


# ── browser.open / browser.click (fake page; no Chromium) ─────────────


class FakeLocator:
    def __init__(self, page: "FakePage", matches: bool) -> None:
        self.page, self.matches = page, matches

    async def all(self) -> list["FakeLocator"]:
        return [self] if self.matches else []

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def evaluate(self, script: str) -> dict[str, str | None]:
        return {"tag": "a", "type": "", "form_method": None, "override": ""}

    async def click(self, timeout: int) -> None:
        self.page.url = "https://example.com/next"
        self.page.html = "<html><main><p>" + "Page two. " * 50 + "</p></main></html>"


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.html = "<html><main><p>" + "Hello world. " * 2000 + "</p></main></html>"

    async def content(self) -> str:
        return self.html

    async def title(self) -> str:
        return "Example"

    async def evaluate(self, script: str) -> list[dict[str, str]]:
        return [{"kind": "link", "text": "Next", "href": "https://example.com/next"}]

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def get_by_role(self, role: str, name: str, exact: bool) -> FakeLocator:
        return FakeLocator(self, role == "link" and name == "Next")

    def get_by_text(self, text: str, exact: bool) -> FakeLocator:
        return FakeLocator(self, False)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, False)


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    async def with_page(url: str, action: Any) -> Any:
        return await action(FakePage(url))

    monkeypatch.setattr(browser, "_with_page", with_page)
    monkeypatch.setattr(browser, "is_private_url", lambda url: "internal" in url)


@pytest.mark.usefixtures("fake_browser")
async def test_browser_open_pages_content(make_context: MakeContext) -> None:
    args = {"url": "https://example.com/", "offset": 0}
    assert_input("browser.open", args)
    first = await browser.open_page(args, make_context())
    assert first["title"] == "Example" and len(first["content"]) == browser.MAX_CONTENT_CHARS
    assert first["next_offset"] == browser.MAX_CONTENT_CHARS
    assert_output("browser.open", first)
    page, offset = first, first["next_offset"]
    while offset is not None:
        page = await browser.open_page({**args, "offset": offset}, make_context())
        offset = page["next_offset"]
    assert page["total_chars"] == first["total_chars"] and page["content"].strip().endswith("Hello world.")
    assert_output("browser.open", page)


@pytest.mark.usefixtures("fake_browser")
async def test_browser_click_follows_target(make_context: MakeContext) -> None:
    args = {"url": "https://example.com/", "target": "Next"}
    assert_input("browser.click", args)
    output = await browser.click(args, make_context())
    assert output["url"] == "https://example.com/next" and "Page two." in output["content"]
    assert_output("browser.click", output)
    with pytest.raises(OperationRejected, match="could not find"):
        await browser.click({**args, "target": "Missing"}, make_context())


@pytest.mark.usefixtures("fake_browser")
async def test_browser_refuses_private_urls(make_context: MakeContext) -> None:
    with pytest.raises(OperationRejected, match="private"):
        await browser.open_page({"url": "http://internal/"}, make_context())


def test_private_url_check() -> None:
    assert browser.is_private_url("http://127.0.0.1/")
    assert browser.is_private_url("file:///etc/passwd")
    assert browser.is_private_url("http://169.254.169.254/latest/meta-data")
