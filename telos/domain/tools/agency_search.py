"""``agency_search`` tool — search a named London estate agent's own site.

Delegates to the injected ``search`` dispatch (built from the per-agency
:class:`~telos.transport.browser.agencies.AgencySearch` implementations),
so each agency fetches its own way — scrape HTML or call its internal JSON
API — without ``domain`` importing ``transport``.

Searching agents directly surfaces agent-direct inventory that may not
appear on the aggregators.
"""
import asyncio
import json
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from telos.domain.agencies import AGENCY_LABELS, AgencySearchUnavailable, SearchFilters, SearchReport
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for

SearchDispatch = Callable[[str, str, bool, SearchFilters, int], Awaitable[SearchReport]]

# Hard per-tool wall-clock cap on one agency search. The fetcher already bounds
# each browser phase, but this is the coarse backstop: a single stalled agency
# must surface as a normal error and let the routine continue — never hang the
# turn (and, since routines share the process, the API + scheduler). One
# unbounded agency fetch froze telos for 13 days (2026-08-26).
_AGENCY_SEARCH_TIMEOUT_S = 120.0

_LABELS = ", ".join(f"{k} ({v})" for k, v in AGENCY_LABELS.items())


class AgencySearchInput(BaseModel):
    agency: str = Field(description=f"Which estate agent to search. One of: {_LABELS}.")
    location: str = Field(
        description="London area or postcode district, e.g. 'Marylebone', 'W2', 'Notting Hill', 'NW1'.",
    )
    for_sale: bool = Field(
        default=False,
        description="True for properties for sale, False for to-rent (default). OpenRent is rentals-only.",
    )
    max_price: int | None = Field(
        default=None,
        description="Max price: monthly rent (£ pcm) for lettings, or sale price (£) for sales. "
                    "Applied at search time where the agency exposes it — Foxtons (structured API, exact), "
                    "and the HTML agencies best-effort (price read from the listing card where shown). "
                    "Listings with no detectable price are surfaced, not dropped.",
    )
    min_beds: int | None = Field(
        default=None,
        description="Minimum bedrooms. Applied to card labels, descriptions and listing URLs; "
                    "unknowns are surfaced, not dropped.",
    )
    max_beds: int | None = Field(default=None, description="Maximum bedrooms (same behaviour as min_beds).")
    max_pages: int = Field(default=5, ge=1, le=10, description="Maximum observed search pages to visit. Coverage and remaining URLs are returned.")
    postcode_districts: list[str] = Field(default_factory=list, max_length=20,
        description="Optional allowed postcode districts, e.g. W1 and W2. Unknown locations still require detail verification.")


class AgencySearchTool:
    """``agency_search`` — search one London estate agent's own site."""

    name = "agency_search"
    description = (
        "Search a specific London estate agent's OWN website for property listings by area — "
        "surfaces agent-direct inventory that may not be on the aggregators (Rightmove/Zoopla). "
        f"Supported: {_LABELS}. Optional max_price/min_beds/max_beds filter results at search "
        "time — Foxtons exactly (structured API returning price+beds); the HTML agencies "
        "best-effort (card fields and listing URLs; unknowns kept). "
        "Follows observed pagination links within max_pages and a time budget; coverage is never exhaustive. "
        "Use alongside rightmove_search/zoopla_search."
    )

    def __init__(self, search: SearchDispatch) -> None:
        self._search = search

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: AgencySearchInput, ctx: ToolContext) -> ToolResult:
        agency = inp.agency.strip().lower()
        if agency not in AGENCY_LABELS:
            return ToolResult(text=f"Unknown agency '{inp.agency}'. Supported: {', '.join(AGENCY_LABELS)}.", is_error=True)
        filters = SearchFilters(max_price=inp.max_price, min_beds=inp.min_beds, max_beds=inp.max_beds,
                                postcode_districts=tuple(inp.postcode_districts))
        try:
            report = await asyncio.wait_for(
                self._search(agency, inp.location, inp.for_sale, filters, inp.max_pages),
                timeout=_AGENCY_SEARCH_TIMEOUT_S,
            )
        except TimeoutError:
            return ToolResult(
                text=f"{AGENCY_LABELS[agency]} search timed out after "
                     f"{int(_AGENCY_SEARCH_TIMEOUT_S)}s for '{inp.location}' — skipping this agency.",
                is_error=True,
            )
        except AgencySearchUnavailable as exc:
            return ToolResult(
                text=json.dumps({"agency": agency, "location": inp.location, "status": "unusable",
                                 "reason": str(exc), "coverage": "unknown", "listings": []}),
                is_error=True,
            )
        hits = [hit.to_dict() for hit in report.hits]
        payload = {"agency": agency, "location": inp.location, "count": len(hits), "listings": hits,
                   "status": "partial" if report.warnings or report.pending_urls else ("results" if hits else "no_matches_on_observed_pages"),
                   "coverage": "observed_pages_only", "pages_visited": report.pages_visited,
                   "pending_urls": report.pending_urls, "warnings": report.warnings, "stop_reason": report.stop_reason,
                   "caveat": "Not an exhaustive inventory check. Verify location, criteria and availability on detail pages."}
        return ToolResult(text=json.dumps(payload, ensure_ascii=False, indent=2))
