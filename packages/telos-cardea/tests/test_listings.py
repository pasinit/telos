import json
from collections.abc import Callable
from typing import Any

import pytest

from telos_cardea import listings
from telos_cardea._cardea import OperationContext
from telos_cardea.config import Config
from telos_cardea.fetch import FetchError, usable

from conftest import assert_input, assert_output

MakeContext = Callable[..., OperationContext]


class FakeFetcher:
    def __init__(self, pages: dict[str, str] | None = None, default: str | None = None) -> None:
        self.pages = pages or {}
        self.default = default
        self.urls: list[str] = []

    async def fetch_html(self, url: str) -> str:
        self.urls.append(url)
        if url in self.pages:
            return self.pages[url]
        if self.default is not None:
            return self.default
        raise FetchError("curl returned a bot page or JS shell")

    async def fetch_json(self, url: str) -> object:
        return json.loads(await self.fetch_html(url))

    async def fetch_html_js(self, url: str) -> str:
        return await self.fetch_html(url)


def rightmove_page(n: int, start: int = 100, result_count: object = "42") -> str:
    props = [{"id": start + i, "displayAddress": f"Flat {i}, Brixton", "price": {"displayPrices": [{"displayPrice": "£2,000 pcm"}]},
              "summary": "Bright flat", "bedrooms": 2, "propertyImages": {"mainImageSrc": "https://img/x.jpg"}} for i in range(n)]
    blob = {"props": {"pageProps": {"searchResults": {"properties": props, "resultCount": result_count}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script></html>'


def zoopla_page() -> str:
    data = {"props": {"pageProps": {"listings": [
        {"listingId": "1", "listingUrl": "/to-rent/details/1/", "displayAddress": "Foo Street, SW2",
         "price": {"displayPrice": "£2,000 pcm"}, "bedrooms": 2},
    ]}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_rightmove_url_matches_telos() -> None:
    url = listings.rightmove_url("Brixton Hill", min_beds=2, max_price=600000, for_sale=False)
    assert url == "https://www.rightmove.co.uk/property-to-rent/Brixton-Hill.html?includeLetAgreed=false&minBedrooms=2&maxPrice=600000"


@pytest.mark.parametrize(("location", "paths"), [
    ("SW2", ["sw2"]),
    ("Notting Hill, London", ["london/notting-hill"]),
    ("Marylebone", ["marylebone", "london/marylebone"]),
])
def test_zoopla_urls_match_telos(location: str, paths: list[str]) -> None:
    urls = listings.zoopla_urls(location, for_sale=False)
    assert urls == [f"https://www.zoopla.co.uk/to-rent/property/{p}/?price_frequency=per_month" for p in paths]


async def test_rightmove_search_returns_structured_results(config: Config, make_context: MakeContext) -> None:
    args = {"location": "Brixton", "min_beds": 2, "max_price": 2500, "for_sale": False}
    assert_input("listings.rightmove_search", args)
    output = await listings.rightmove_search(args, make_context(), fetcher=FakeFetcher(default=rightmove_page(3)))  # type: ignore[arg-type]
    assert output["status"] == "results"
    assert [item["id"] for item in output["listings"]] == ["100", "101", "102"]
    assert_output("listings.rightmove_search", output)


async def test_rightmove_large_page_fits_output_limit(make_context: MakeContext) -> None:
    output = await listings.rightmove_search({"location": "London"}, make_context(), fetcher=FakeFetcher(default=rightmove_page(150)))  # type: ignore[arg-type]
    assert len(output["listings"]) == 100 and output["truncated"] is True
    assert_output("listings.rightmove_search", output)


async def test_rightmove_unusable_page_is_unavailable_not_empty(make_context: MakeContext) -> None:
    for fetcher in (FakeFetcher(), FakeFetcher(default="<html>nothing</html>")):
        output = await listings.rightmove_search({"location": "Nowhere"}, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
        assert output["status"] == "unavailable" and output["listings"] == []
        assert "not evidence of zero inventory" in output["caveat"]
        assert_output("listings.rightmove_search", output)


async def test_zoopla_falls_back_to_london_path(make_context: MakeContext) -> None:
    good = "https://www.zoopla.co.uk/to-rent/property/london/marylebone/?price_frequency=per_month"
    fetcher = FakeFetcher({good: zoopla_page()})
    args = {"location": "Marylebone", "for_sale": False}
    assert_input("listings.zoopla_search", args)
    output = await listings.zoopla_search(args, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert fetcher.urls[-1] == good
    assert output["status"] == "results" and output["listings"][0]["id"] == "1"
    assert_output("listings.zoopla_search", output)


async def test_zoopla_unavailable(make_context: MakeContext) -> None:
    output = await listings.zoopla_search({"location": "SW2"}, make_context(), fetcher=FakeFetcher())  # type: ignore[arg-type]
    assert output["status"] == "unavailable" and "fetch failed" in output["reason"]
    assert_output("listings.zoopla_search", output)


def card(id: str, price: str = "£3,000 pcm") -> str:
    return f'<article><a href="/property-for-rent/flat-london-w2/{id}">Example W2</a><p>{price}</p><p>2 Bedrooms</p></article>'


async def test_agency_search_filters_and_reports_coverage(config: Config, make_context: MakeContext) -> None:
    args: dict[str, Any] = {"agency": "dexters", "location": "Bayswater", "max_price": 3500, "min_beds": 2, "postcode_districts": ["W2"]}
    assert_input("listings.agency_search", args)
    fetcher = FakeFetcher(default=card("1111", "£8,000 pcm") + card("2222"))
    output = await listings.agency_search(args, make_context(), config=config, fetcher=fetcher)  # type: ignore[arg-type]
    assert output["agency"] == "Dexters" and output["status"] == "results"
    assert [hit["id"] for hit in output["listings"]] == ["2222"]
    assert_output("listings.agency_search", output)


async def test_agency_search_first_page_failure_is_unavailable(config: Config, make_context: MakeContext) -> None:
    output = await listings.agency_search({"agency": "dexters", "location": "Bayswater"}, make_context(), config=config,
                                          fetcher=FakeFetcher())  # type: ignore[arg-type]
    assert output["status"] == "unavailable" and output["listings"] == []
    assert_output("listings.agency_search", output)


def test_unknown_agency_is_refused_by_schema() -> None:
    import _cardea_operations as vendored
    from conftest import load_descriptor

    assert not vendored.valid_arguments(load_descriptor("listings.agency_search"), {"agency": "nope", "location": "W2"})


def test_fetch_heuristics_reject_bot_pages_and_accept_hydration_blobs() -> None:
    assert not usable("<html><body>Just a moment... Checking your browser. Ray ID 123</body></html>")
    shell = '<html><div id="__next"></div><script>window.__NEXT_DATA__</script></html>'
    assert not usable(shell)
    prose = "<p>" + "Two bedroom flat to rent in Brixton, close to the tube. " * 10 + "</p>"
    assert usable("<html><body>" + prose + "</body></html>")
    # A JS shell is accepted when the wire HTML carries the hydration data.
    assert usable('<html><div id="__next"></div>' + prose + rightmove_page(30) + "</html>")


async def test_rightmove_follows_index_pagination_and_dedupes(make_context: MakeContext) -> None:
    base = listings.rightmove_url("Brixton")
    page2 = listings._rightmove_page_url(base, 24)
    # A full first page (24 cards) implies another; the second page is short (5), so it is the last.
    fetcher = FakeFetcher({base: rightmove_page(24, start=100), page2: rightmove_page(5, start=200)})
    args = {"location": "Brixton", "max_pages": 2}
    assert_input("listings.rightmove_search", args)
    output = await listings.rightmove_search(args, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert fetcher.urls == [base, page2]
    assert output["status"] == "results" and len(output["listings"]) == 29
    assert output["pages_visited"] == [base, page2] and output["pending_urls"] == []
    assert output["stop_reason"] == "last_page"
    assert_output("listings.rightmove_search", output)


async def test_rightmove_reports_unread_pages_when_capped_at_max_pages(make_context: MakeContext) -> None:
    base = listings.rightmove_url("Brixton")
    page2 = listings._rightmove_page_url(base, 24)
    fetcher = FakeFetcher({base: rightmove_page(24, start=100), page2: rightmove_page(24, start=200)})
    output = await listings.rightmove_search({"location": "Brixton", "max_pages": 1}, make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert fetcher.urls == [base]  # page 2 is left for a later, wider run
    assert len(output["listings"]) == 24 and output["pages_visited"] == [base]
    assert output["pending_urls"] == [page2] and output["stop_reason"] == "max_pages"
    assert output["truncated"] is True
    assert_output("listings.rightmove_search", output)


async def test_rightmove_tries_place_and_district_separately(make_context: MakeContext) -> None:
    good = listings.rightmove_url("W1", 2, 3500, False)
    fetcher = FakeFetcher({good: rightmove_page(2)}, default="<html>This isn't the place you were looking for!</html>")
    output = await listings.rightmove_search({"location": "Marylebone W1", "min_beds": 2, "max_price": 3500, "for_sale": False},
                                             make_context(), fetcher=fetcher)  # type: ignore[arg-type]
    assert output["status"] == "results" and output["search_url"] == good
    assert fetcher.urls == [listings.rightmove_url("Marylebone W1", 2, 3500, False), good]
