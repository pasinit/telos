"""``rightmove_search`` tool — builds a Rightmove search URL and hands
it to the deployment-wide ``web_fetch`` callable.

HTML extraction (parsing listing pages / search pages into structured
data) happens in :mod:`telos.transport.browser.rightmove`, called from
inside ``web_fetch`` after the page is fetched. This module only
declares the LLM-facing tool.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

from pydantic import BaseModel, Field

from telos.domain.portal_locations import location_candidates
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for


class RightmoveSearchInput(BaseModel):
    location: str = Field(
        description="UK location (postcode, town, or area), e.g. 'Brixton', 'SW2', 'Manchester'.",
    )
    min_beds: int | None = Field(
        default=None,
        description="Minimum number of bedrooms.",
    )
    max_price: int | None = Field(
        default=None,
        description="Maximum price in GBP.",
    )
    max_beds: int | None = Field(default=None, description="Maximum number of bedrooms.")
    for_sale: bool = Field(
        default=True,
        description="True for properties for sale (default), False for to-rent.",
    )


class RightmoveSearchTool:
    """``rightmove_search`` — search Rightmove via ``web_fetch`` (which
    runs the page through :func:`telos.transport.browser.rightmove.enrich`
    to return structured listing data instead of raw HTML).
    """

    name = "rightmove_search"
    description = (
        "Search Rightmove for UK property listings. Builds a search URL from "
        "the location/filters and returns structured results."
    )

    def __init__(self, web_fetch: Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._web_fetch = web_fetch

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: RightmoveSearchInput, ctx: ToolContext) -> ToolResult:
        # "Marylebone W1" has no area page; "Marylebone" and "W1" do.
        text = ""
        for area in location_candidates(inp.location) or [inp.location]:
            url = _build_search_url(
                location=area,
                min_beds=inp.min_beds,
                max_price=inp.max_price,
                for_sale=inp.for_sale,
                max_beds=inp.max_beds,
            )
            result = await self._web_fetch(url)
            if result.get("structured", {}).get("kind") == "search_results":
                return ToolResult(text=result["text"], images=result.get("images") or None)
            text = result["text"]
        return ToolResult(
            text="Rightmove coverage unavailable: no recognised search results. "
                 "This is not evidence of zero inventory.\n" + text,
            is_error=True,
        )


def _build_search_url(
    location: str,
    min_beds: int | None = None,
    max_price: int | None = None,
    for_sale: bool = True,
    max_beds: int | None = None,
) -> str:
    """Area-specific search URL; unknown areas surface as unusable coverage."""
    base = (
        "https://www.rightmove.co.uk/property-for-sale/"
        if for_sale
        else "https://www.rightmove.co.uk/property-to-rent/"
    )
    # The generic landing page ignores searchLocation. Area pages contain
    # the region identity and expose real structured search results.
    area = quote(location.strip().replace(" ", "-"), safe="-")
    params = {"includeLetAgreed": "false"} if not for_sale else {}
    if min_beds is not None:
        params["minBedrooms"] = str(min_beds)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    if max_beds is not None:
        params["maxBedrooms"] = str(max_beds)
    return base + area + ".html" + ("?" + urlencode(params) if params else "")
