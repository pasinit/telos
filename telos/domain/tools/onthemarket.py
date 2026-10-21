"""``onthemarket_search`` tool — builds an OnTheMarket search URL and hands it to
the deployment-wide ``web_fetch`` callable, following observed pagination.

HTML extraction happens in :mod:`telos.transport.browser.onthemarket`, called
from inside ``web_fetch``. The Cardea operation ``listings.onthemarket_search``
(packages/telos-cardea) builds the same URLs.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel, Field

from telos.domain.portal_locations import location_candidates
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for


class OnTheMarketSearchInput(BaseModel):
    location: str = Field(description="UK area or postcode district, e.g. 'Bayswater', 'W2', 'Manchester'.")
    min_beds: int | None = Field(default=None, description="Minimum number of bedrooms.")
    max_beds: int | None = Field(default=None, description="Maximum number of bedrooms.")
    max_price: int | None = Field(default=None, description="Maximum price in GBP. For rentals this is monthly rent.")
    for_sale: bool = Field(default=True, description="True for properties for sale (default), False for to-rent.")
    max_pages: int = Field(default=1, ge=1, le=5, description="Result pages to read (about 30 listings each).")


class OnTheMarketSearchTool:
    """``onthemarket_search`` — search OnTheMarket via ``web_fetch``."""

    name = "onthemarket_search"
    description = (
        "Search OnTheMarket for UK property listings. Builds a search URL from the location/filters, "
        "reads up to max_pages result pages and returns structured results."
    )

    def __init__(self, web_fetch: Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._web_fetch = web_fetch

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: OnTheMarketSearchInput, ctx: ToolContext) -> ToolResult:
        reason = "no recognised search results"
        for area in location_candidates(inp.location) or [inp.location]:
            url = build_search_url(area, inp.min_beds, inp.max_beds, inp.max_price, inp.for_sale)
            try:
                first = await self._web_fetch(url)
            except Exception as exc:
                reason = f"fetch failed ({type(exc).__name__})"
                continue
            data = _search_data(first)
            if data is not None:
                return ToolResult(text=await self._collect(url, data, inp.max_pages))
        return ToolResult(text=f"OnTheMarket search unavailable: {reason}. This is not evidence of zero inventory.", is_error=True)

    async def _collect(self, url: str, data: dict, max_pages: int) -> str:
        listings = {str(card.get("id")): card for card in data.get("results", []) if isinstance(card, dict)}
        visited, warnings = [url], []
        pending = _next_page(url, data)
        while pending is not None and len(visited) < max_pages:
            try:
                page = _search_data(await self._web_fetch(pending))
            except Exception as exc:
                page, failure = None, f"fetch failed ({type(exc).__name__})"
            else:
                failure = "no recognised search results"
            if page is None:
                warnings.append(f"Failed to read {pending} ({failure}); retained earlier results")
                break
            visited.append(pending)
            for card in page.get("results", []):
                if isinstance(card, dict):
                    listings.setdefault(str(card.get("id")), card)
            pending = _next_page(pending, page)
        out = {
            "site": "onthemarket", "search_url": url, "listings": list(listings.values()), "pages_visited": visited,
            "pending_urls": [pending] if pending else [], "warnings": warnings, "total": data.get("total"),
            "caveat": "Search cards and recency labels do not confirm current availability; verify on detail pages.",
        }
        return f"[search_results] OnTheMarket search: {len(listings)} listings from {len(visited)} page(s)\n" + json.dumps(out, ensure_ascii=False)


def build_search_url(location: str, min_beds: int | None = None, max_beds: int | None = None, max_price: int | None = None,
                     for_sale: bool = True) -> str:
    slug = "-".join(part.lower() for part in location.split()) or "uk"
    params = {k: v for k, v in (("min-bedrooms", min_beds), ("max-bedrooms", max_beds), ("max-price", max_price)) if v is not None}
    kind = "for-sale" if for_sale else "to-rent"
    return f"https://www.onthemarket.com/{kind}/property/{slug}/" + (f"?{urlencode(params)}" if params else "")


def _search_data(result: dict[str, Any]) -> dict | None:
    structured = result.get("structured")
    if isinstance(structured, dict) and structured.get("kind") == "search_results":
        return structured.get("data") or {}
    return None


def _page_number(url: str) -> int:
    try:
        return int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
    except ValueError:
        return 0


def _next_page(url: str, data: dict) -> str | None:
    """The observed link to the following page; OTM's own links keep the filters."""
    current = _page_number(url)
    return next((link for link in data.get("pagination", []) if _page_number(link) == current + 1), None)
