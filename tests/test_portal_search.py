"""Portal area fallbacks and the onthemarket_search tool."""

import json
from pathlib import Path

import pytest

from telos.core.delegation import DelegationGraph
from telos.core.run_context import RunContext
from telos.core.tool_context import ToolContext
from telos.domain.portal_locations import location_candidates
from telos.domain.tools.onthemarket import OnTheMarketSearchInput, OnTheMarketSearchTool, build_search_url
from telos.domain.tools.rightmove import RightmoveSearchInput, RightmoveSearchTool, _build_search_url, _with_index


def context(tmp_path: Path) -> ToolContext:
    return ToolContext(RunContext("u", DelegationGraph()), "agent", tmp_path, tmp_path)


@pytest.mark.parametrize(("location", "expected"), [
    ("Marylebone W1", ["Marylebone W1", "W1", "Marylebone"]),
    ("Bayswater, W2", ["Bayswater W2", "W2", "Bayswater"]),
    ("W2 4AB", ["W2 4AB", "W2"]),
    ("Notting Hill, London", ["Notting Hill London", "Notting Hill"]),
    ("London Bridge", ["London Bridge"]),
    ("Marylebone, London W1", ["Marylebone London W1", "W1", "Marylebone"]),
    ("SW11", ["SW11"]),
    ("Brixton", ["Brixton"]),
    ("  ", []),
])
def test_location_candidates(location: str, expected: list[str]) -> None:
    assert location_candidates(location) == expected


def search_result(ids: list[int], pagination: list[str] = ()) -> dict:
    data = {"results": [{"id": str(i), "url": f"https://www.onthemarket.com/details/{i}/"} for i in ids], "total": 50,
            "pagination": list(pagination)}
    return {"text": "page", "structured": {"kind": "search_results", "summary": "s", "data": data}}


class Pages:
    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages, self.urls = pages, []

    async def __call__(self, url: str) -> dict:
        self.urls.append(url)
        return self.pages.get(url, {"text": "Page not found", "status_code": 404})


def test_onthemarket_url() -> None:
    assert build_search_url("Bayswater", 2, 2, 3500, False) == \
        "https://www.onthemarket.com/to-rent/property/bayswater/?min-bedrooms=2&max-bedrooms=2&max-price=3500"
    assert build_search_url("St Albans") == "https://www.onthemarket.com/for-sale/property/st-albans/"


async def test_onthemarket_falls_back_to_district_and_follows_pages(tmp_path: Path) -> None:
    first = build_search_url("W2", 2, None, 3500, False)
    second = "https://www.onthemarket.com/to-rent/2-bed-property/w2/?max-price=3500&page=2"
    third = "https://www.onthemarket.com/to-rent/2-bed-property/w2/?max-price=3500&page=3"
    fetch = Pages({first: search_result([1, 2], [second]), second: search_result([2, 3], [first, third])})
    inp = OnTheMarketSearchInput(location="Bayswater W2", min_beds=2, max_price=3500, for_sale=False, max_pages=2)
    result = await OnTheMarketSearchTool(fetch)(inp, context(tmp_path))
    assert not result.is_error
    out = json.loads(result.text.split("\n", 1)[1])
    assert [card["id"] for card in out["listings"]] == ["1", "2", "3"]
    assert out["pages_visited"] == [first, second] and out["pending_urls"] == [third]
    assert fetch.urls == [build_search_url("Bayswater W2", 2, None, 3500, False), first, second]


async def test_onthemarket_unrecognised_everywhere_is_an_error(tmp_path: Path) -> None:
    result = await OnTheMarketSearchTool(Pages({}))(OnTheMarketSearchInput(location="Nowhere"), context(tmp_path))
    assert result.is_error and "not evidence of zero inventory" in result.text


def rm_result(n: int, text: str) -> dict:
    return {"text": text, "structured": {"kind": "search_results", "data": {"results": [{"id": str(i)} for i in range(n)]}}}


async def test_rightmove_tool_follows_index_pagination_and_flags_backlog(tmp_path: Path) -> None:
    base = _build_search_url("Brixton")
    page2, page3 = _with_index(base, 24), _with_index(base, 48)
    # Two full (24-card) pages read, a third still pending at the max_pages=2 cap.
    fetch = Pages({base: rm_result(24, "page1"), page2: rm_result(24, "page2"), page3: rm_result(3, "page3")})
    result = await RightmoveSearchTool(fetch)(RightmoveSearchInput(location="Brixton", max_pages=2), context(tmp_path))
    assert not result.is_error
    assert fetch.urls == [base, page2]  # the third page is left for a later, wider run
    assert "page1" in result.text and "page2" in result.text and "page3" not in result.text
    assert "More Rightmove pages remain unread" in result.text and page3 in result.text


async def test_rightmove_tool_short_first_page_has_no_backlog(tmp_path: Path) -> None:
    base = _build_search_url("Brixton")
    fetch = Pages({base: rm_result(10, "only page")})
    result = await RightmoveSearchTool(fetch)(RightmoveSearchInput(location="Brixton", max_pages=3), context(tmp_path))
    assert fetch.urls == [base] and result.text == "only page"


async def test_rightmove_tries_district_then_place(tmp_path: Path) -> None:
    good = _build_search_url("Marylebone", 2, 3500, False, 2)
    fetch = Pages({good: {"text": "results", "structured": {"kind": "search_results", "summary": "", "data": {}}}})
    inp = RightmoveSearchInput(location="Marylebone W1", min_beds=2, max_price=3500, max_beds=2, for_sale=False)
    result = await RightmoveSearchTool(fetch)(inp, context(tmp_path))
    assert not result.is_error and result.text == "results"
    assert fetch.urls == [_build_search_url("Marylebone W1", 2, 3500, False, 2), _build_search_url("W1", 2, 3500, False, 2), good]
