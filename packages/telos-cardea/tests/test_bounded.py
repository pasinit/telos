"""Outputs stay under Cardea's per-result cap: descriptor caps and handler-side trimming."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from telos_cardea import browser, ledger, listings, memory, routine
from telos_cardea._cardea import OperationContext
from telos_cardea.bounded import MAX_OUTPUT_BYTES, TRUNCATION_MARK, fit, output_bytes
from telos_cardea.config import Config
from test_ledger import ROUTINE_CONTEXT, Recorder
from test_listings import FakeFetcher
from test_web_memory_browser import FakePage, mock_client

from conftest import DESCRIPTORS, assert_output

MakeContext = Callable[..., OperationContext]


@pytest.mark.parametrize("path", sorted(DESCRIPTORS.glob("**/*.json")), ids=lambda p: str(p.relative_to(DESCRIPTORS)))
def test_descriptor_caps_fit_a_cardea_receipt(path: Path) -> None:
    assert json.loads(path.read_text())["cardea"]["max_output_bytes"] <= MAX_OUTPUT_BYTES


def test_fit_trims_in_rule_order_without_mutating() -> None:
    output = {"items": ["x" * 1000] * 100, "note": "é" * 10000, "extra": {"blob": "y" * 5000}}
    fitted, trimmed = fit(output, ("list", "items"), ("text", "note"), ("drop", "extra.blob"))
    assert output_bytes(fitted) <= MAX_OUTPUT_BYTES and trimmed == ["items"]
    assert 0 < len(fitted["items"]) < 100 and fitted["note"] == output["note"] and len(output["items"]) == 100
    fitted, trimmed = fit({"note": "é" * 40000, "extra": {"blob": "y"}}, ("text", "note"))
    assert trimmed == ["note"] and fitted["note"].endswith(TRUNCATION_MARK) and output_bytes(fitted) <= MAX_OUTPUT_BYTES
    fitted, trimmed = fit({"small": "ok", "extra": {"blob": "y" * 70000}}, ("text", "small"), ("drop", "extra.blob"))
    assert trimmed == ["extra.blob"] and fitted == {"small": "ok", "extra": {}}  # an innocent field is left whole
    assert fit({"a": "b"}, ("text", "a")) == ({"a": "b"}, [])


def rightmove_page(n: int, summary: str) -> str:
    props = [{"id": 100 + i, "displayAddress": f"Flat {i}, Brixton", "price": {"displayPrices": [{"displayPrice": "£2,000 pcm"}]},
              "summary": summary, "bedrooms": 2, "propertyImages": {"mainImageSrc": "https://img/x.jpg"}} for i in range(n)]
    blob = {"props": {"pageProps": {"searchResults": {"properties": props, "resultCount": "420"}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(blob)}</script></html>'


async def test_oversize_listing_search_keeps_the_first_listings_and_says_so(make_context: MakeContext) -> None:
    page = rightmove_page(100, "Bright flat with a long description. " * 40)
    output = await listings.rightmove_search({"location": "London"}, make_context(), fetcher=FakeFetcher(default=page))  # type: ignore[arg-type]
    assert 0 < len(output["listings"]) < 100 and output["truncated"] is True
    assert output["listings"][0]["id"] in {100, "100"} and "narrow the search" in output["caveat"]
    assert_output("listings.rightmove_search", output)


async def test_oversize_browser_page_shortens_content_and_keeps_paging(make_context: MakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "".join(f"Paragraph {i} with some words. " for i in range(4000))
    page = FakePage("https://example.com/")
    page.html = f"<html><main><p>{text}</p></main></html>"

    async def with_page(url: str, action: Any) -> Any:
        return await action(page)

    monkeypatch.setattr(browser, "_with_page", with_page)
    monkeypatch.setattr(browser, "MAX_CONTENT_CHARS", 200000)
    first = await browser.open_page({"url": "https://example.com/"}, make_context())
    assert_output("browser.open", first)
    assert first["next_offset"] == len(first["content"]) < first["total_chars"]
    assert TRUNCATION_MARK not in first["content"]
    pages, offset = [first["content"]], first["next_offset"]
    while offset is not None:
        nxt = await browser.open_page({"url": "https://example.com/", "offset": offset}, make_context())
        assert_output("browser.open", nxt)
        pages.append(nxt["content"])
        offset = nxt["next_offset"]
    assert "".join(pages) == first["content"] + "".join(pages[1:]) and len("".join(pages)) == first["total_chars"]


async def test_oversize_ledger_page_moves_the_rest_to_the_next_page(config: Config, make_context: MakeContext) -> None:
    items = [{"url": f"https://www.example.com/properties/{i}", "reason": "r" * 900} for i in range(200)]
    recorder = Recorder({("GET", "/internal/cardea/v1/ledger"): httpx.Response(200, json={"items": items, "next_offset": None})})
    output = await ledger.list_({"offset": 40, "limit": 200}, make_context(), config=config, client=recorder.client())
    assert_output("ledger.list", output)
    kept = len(output["items"])
    assert 0 < kept < 200 and output["next_offset"] == 40 + kept and output["items"] == items[:kept]


async def test_oversize_routine_context_cuts_the_largest_block(config: Config, make_context: MakeContext) -> None:
    body = {**ROUTINE_CONTEXT, "delivery_history": "Sent: flat at 1 Example Road. " * 4000, "reserved_recall": "Keep me."}
    recorder = Recorder({("GET", "/internal/cardea/v1/routine-context"): httpx.Response(200, json=body)})
    output = await routine.routine_context({}, make_context(), config=config, client=recorder.client())
    assert_output("telos.routine_context", output)
    assert output["truncated"] == ["delivery_history"] and output["delivery_history"].endswith(TRUNCATION_MARK)
    assert output["reserved_recall"] == "Keep me." and output["ledger_summary"] == ROUTINE_CONTEXT["ledger_summary"]


async def test_oversize_briefing_and_recall_are_trimmed(config: Config, make_context: MakeContext) -> None:
    def briefing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"briefing": "Prefers south-facing flats. " * 4000, "units": 90})

    output = await memory.briefing({"query": "flats"}, make_context(), config=config, client=mock_client(briefing))
    assert_output("memory.briefing", output)
    assert output["briefing"].endswith(TRUNCATION_MARK) and output["units"] == 90

    def recall(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"text": f"Viewed {i} Foo St. " + "detail " * 200} for i in range(100)]})

    output = await memory.recall({"query": "Foo St"}, make_context(), config=config, client=mock_client(recall))
    assert_output("memory.recall", output)
    kept = len(output["recordatio"]["results"])
    assert 0 < kept < 100 and output["count"] == kept and output["recordatio"]["omitted"] == 100 - kept
