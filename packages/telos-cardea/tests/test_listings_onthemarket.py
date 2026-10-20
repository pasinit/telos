"""listings.onthemarket_search and the JavaScript-rendering fallback for js_search agencies."""

import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from telos_cardea import listings
from telos_cardea._cardea import OperationContext
from telos_cardea.config import Config
from telos_cardea.fetch import Fetcher, FetchError, render_js_template

from conftest import assert_input, assert_output

MakeContext = Callable[..., OperationContext]
SEARCH = "https://www.onthemarket.com/to-rent/property/brixton/?min-bedrooms=2&max-price=2500"


def otm_page(ids: list[int], total: int, pages: tuple[int, ...] = (), nested: bool = True) -> str:
    cards = [{"id": str(i), "details-url": f"/details/{i}/", "address": f"Example Road {i}, London SW2", "price": "£2,100 pcm",
              "bedrooms": 2, "bathrooms": 1, "features": ["Garden"], "main-label": "Added today"} for i in ids]
    state = {"initialReduxState": {"results": {"list": cards, "totalResults": total}}}
    blob = {"props": {"pageProps": state} if nested else {"pageProps": {}, **state}}
    links = "".join(f'<a href="/to-rent/property/brixton/?max-price=2500&min-bedrooms=2&page={n}">{n}</a>' for n in pages)
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script>{links}</html>'


def page_url(n: int) -> str:
    return f"https://www.onthemarket.com/to-rent/property/brixton/?max-price=2500&min-bedrooms=2&page={n}"


class Pages:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    async def fetch_html(self, url: str) -> str:
        self.urls.append(url)
        if url not in self.pages:
            raise FetchError("curl HTTP 403")
        return self.pages[url]


def test_onthemarket_url() -> None:
    assert listings.onthemarket_url("Brixton", 2, None, 2500, False) == SEARCH
    assert listings.onthemarket_url("SW2", max_beds=3) == "https://www.onthemarket.com/for-sale/property/sw2/?max-bedrooms=3"
    assert listings.onthemarket_url("St Albans") == "https://www.onthemarket.com/for-sale/property/st-albans/"


async def test_single_page_results(make_context: MakeContext) -> None:
    args = {"location": "Brixton", "min_beds": 2, "max_price": 2500, "for_sale": False}
    assert_input("listings.onthemarket_search", args)
    fetcher = Pages({SEARCH: otm_page([101, 102], total=40, pages=(2, 3))})
    output = await listings.onthemarket_search(args, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert output["status"] == "results" and [hit["id"] for hit in output["listings"]] == ["101", "102"]
    assert output["listings"][0]["url"] == "https://www.onthemarket.com/details/101/"
    assert output["total"] == 40 and output["truncated"] is True
    assert output["stop_reason"] == "max_pages" and output["pending_urls"] == [page_url(2)]
    assert fetcher.urls == [SEARCH]
    assert_output("listings.onthemarket_search", output)


async def test_follows_observed_next_page_only(make_context: MakeContext) -> None:
    fetcher = Pages({SEARCH: otm_page([101], 3, pages=(3, 2)), page_url(2): otm_page([102, 101], 3, pages=(1, 3)),
                     page_url(3): otm_page([103], 3, pages=(1, 2))})
    output = await listings.onthemarket_search({"location": "Brixton", "min_beds": 2, "max_price": 2500, "for_sale": False,
                                                "max_pages": 5}, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert fetcher.urls == [SEARCH, page_url(2), page_url(3)]
    assert [hit["id"] for hit in output["listings"]] == ["101", "102", "103"] and output["truncated"] is False
    assert output["stop_reason"] == "no_observed_next_link" and output["pending_urls"] == []
    assert_output("listings.onthemarket_search", output)


async def test_later_page_failure_is_partial(make_context: MakeContext) -> None:
    fetcher = Pages({SEARCH: otm_page([101], 60, pages=(2,))})
    output = await listings.onthemarket_search({"location": "Brixton", "min_beds": 2, "max_price": 2500, "for_sale": False,
                                                "max_pages": 3}, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert output["status"] == "partial" and output["stop_reason"] == "page_failed" and "HTTP 403" in output["warnings"][0]
    assert output["pending_urls"] == [page_url(2)]
    assert_output("listings.onthemarket_search", output)


async def test_blocked_first_page_is_unavailable_not_empty(make_context: MakeContext) -> None:
    output = await listings.onthemarket_search({"location": "Brixton"}, make_context(), fetcher=Pages({}))  # type: ignore[arg-type]
    assert output["status"] == "unavailable" and output["listings"] == [] and "not evidence of zero inventory" in output["caveat"]
    assert_output("listings.onthemarket_search", output)


async def test_unrecognised_page_is_unavailable(make_context: MakeContext) -> None:
    url = listings.onthemarket_url("Brixton")
    output = await listings.onthemarket_search({"location": "Brixton"}, make_context(),
                                               fetcher=Pages({url: "<html><p>Something else</p></html>"}))  # type: ignore[arg-type]
    assert output["status"] == "unavailable" and output["reason"] == "no recognised search results"


async def test_empty_search_is_no_matches_and_legacy_state_shape_parses(make_context: MakeContext) -> None:
    url = listings.onthemarket_url("Brixton")
    output = await listings.onthemarket_search({"location": "Brixton"}, make_context(),
                                               fetcher=Pages({url: otm_page([], 0, nested=False)}))  # type: ignore[arg-type]
    assert output["status"] == "no_matches" and output["total"] == 0
    assert_output("listings.onthemarket_search", output)


# ── js_search agencies (Dexters, KFH): rendered HTML or unavailable ────

DEXTERS = "https://www.dexters.co.uk/property-lettings/properties-to-rent-in-bayswater"
CARDS = ('<article><a href="/property-for-rent/flat-london-w2/1111">Example W2</a><p>£3,000 pcm</p><p>2 Bedrooms</p></article>'
         + "<p>" + "Flats to rent in Bayswater. " * 20 + "</p>")


@pytest.fixture
def curl(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serves server HTML with cards (without JS-filled data) and records every URL fetched."""
    urls: list[str] = []

    def fake(url: str, timeout: int) -> tuple[str, int]:
        urls.append(url)
        return CARDS, 200

    monkeypatch.setattr(Fetcher, "_curl", staticmethod(fake))
    return urls


async def test_js_agency_without_browser_or_unblocker_is_unavailable(config: Config, make_context: MakeContext,
                                                                      curl: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    config = replace(config, browser_fallback=True, unblocker_url="")
    monkeypatch.setattr("telos_cardea.fetch.browser_available", lambda: False)
    output = await listings.agency_search({"agency": "dexters", "location": "Bayswater"}, make_context(), config=config)
    assert output["status"] == "unavailable" and output["listings"] == []
    assert "needs JavaScript rendering" in output["reason"] and "browser extra not installed" in output["reason"]
    assert curl == []  # never the server HTML that lacks the JS-filled prices
    assert_output("listings.agency_search", output)


async def test_js_agency_falls_back_to_rendering_unblocker(config: Config, make_context: MakeContext, curl: list[str],
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    config = replace(config, browser_fallback=True, unblocker_url="https://unblocker.example/v1/?api_key=K&render_js=false&url={url}")
    monkeypatch.setattr("telos_cardea.fetch.browser_available", lambda: False)
    output = await listings.agency_search({"agency": "dexters", "location": "Bayswater", "max_pages": 1}, make_context(), config=config)
    assert output["status"] == "results" and [hit["id"] for hit in output["listings"]] == ["1111"]
    assert curl and all("render_js=true&url=" in url for url in curl)
    assert_output("listings.agency_search", output)


async def test_js_agency_uses_browser_when_installed(config: Config, make_context: MakeContext, curl: list[str],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[str] = []

    async def render(url: str) -> str:
        rendered.append(url)
        return CARDS

    monkeypatch.setattr("telos_cardea.fetch.browser_available", lambda: True)
    monkeypatch.setattr(Fetcher, "_render", staticmethod(render))
    output = await listings.agency_search({"agency": "dexters", "location": "Bayswater", "max_pages": 1}, make_context(),
                                          config=replace(config, browser_fallback=True))
    assert output["status"] == "results" and rendered == [DEXTERS] and curl == []


async def test_failed_browser_render_falls_back_to_unblocker(config: Config, curl: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(url: str) -> str:
        raise RuntimeError("chromium crashed")

    monkeypatch.setattr("telos_cardea.fetch.browser_available", lambda: True)
    monkeypatch.setattr(Fetcher, "_render", staticmethod(broken))
    fetcher = Fetcher(replace(config, browser_fallback=True, unblocker_url="https://unblocker.example/?url={url}"))
    assert await fetcher.fetch_html_js(DEXTERS) == CARDS
    assert curl == ["https://unblocker.example/?url=" + DEXTERS.replace(":", "%3A").replace("/", "%2F")]


def test_render_js_template() -> None:
    assert render_js_template("https://u/?render_js=false&url={url}") == "https://u/?render_js=true&url={url}"
    assert render_js_template("https://u/?a=1&render_js=0") == "https://u/?a=1&render_js=true"
    assert render_js_template("https://u/?url={url}") == "https://u/?url={url}"
    assert render_js_template("https://u/?prerender_js=false") == "https://u/?prerender_js=false"
