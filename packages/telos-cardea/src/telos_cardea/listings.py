"""``listings.*`` read operations: Rightmove, Zoopla, OnTheMarket and estate-agent searches.

URL building follows ``telos.domain.tools.rightmove`` / ``zoopla``; parsing is the
vendored Telos code. An unusable fetch is returned as ``status: "unavailable"``
with a reason rather than raised, so the agent sees why coverage is missing and
never mistakes it for zero inventory.
"""

import asyncio
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

from telos_cardea._cardea import Arguments, OperationContext, Output
from telos_cardea._vendor import agencies, onthemarket, rightmove, zoopla
from telos_cardea._vendor.agency_types import AGENCY_LABELS, SearchFilters
from telos_cardea.config import Config, load_config
from telos_cardea.fetch import Fetcher

UNAVAILABLE_CAVEAT = "Coverage unavailable; this is not evidence of zero inventory."
AGENCY_SEARCH_TIMEOUT_S = 120.0
# Inside the descriptors' 180 s: curl, unblocker and a browser render can each take a while.
PORTAL_SEARCH_TIMEOUT_S = 150.0


def _unavailable(site: str, url: str, reason: str) -> Output:
    return {"site": site, "status": "unavailable", "search_url": url, "reason": reason, "listings": [], "caveat": UNAVAILABLE_CAVEAT}


def rightmove_url(location: str, min_beds: int | None = None, max_price: int | None = None, for_sale: bool = True,
                  max_beds: int | None = None) -> str:
    base = "https://www.rightmove.co.uk/property-for-sale/" if for_sale else "https://www.rightmove.co.uk/property-to-rent/"
    area = quote(location.strip().replace(" ", "-"), safe="-")
    params = {"includeLetAgreed": "false"} if not for_sale else {}
    if min_beds is not None:
        params["minBedrooms"] = str(min_beds)
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    if max_beds is not None:
        params["maxBedrooms"] = str(max_beds)
    return base + area + ".html" + ("?" + urlencode(params) if params else "")


_POSTCODE_RE = re.compile(r"^[a-z]{1,2}\d[a-z\d]?(-?\d[a-z]{2})?$")


def _location_slug(location: str) -> str:
    parts = re.findall(r"[^\W_]+", location.strip().lower())
    return "-".join(parts) or "uk"


def _zoopla_path(location: str) -> str:
    slug = _location_slug(location)
    parts = slug.split("-")
    if _POSTCODE_RE.fullmatch(slug):
        return slug
    if "london" in parts:
        area = "-".join(p for p in parts if p not in {"london", "uk", "england"})
        return f"london/{area}" if area else "london"
    return slug


def zoopla_urls(location: str, min_beds: int | None = None, max_price: int | None = None, for_sale: bool = True) -> list[str]:
    primary = _zoopla_path(location)
    paths = [primary]
    slug = _location_slug(location)
    if "/" not in primary and "-" not in slug and slug not in {"uk", "london"} and not _POSTCODE_RE.fullmatch(slug):
        paths.append(f"london/{slug}")
    params: dict[str, str | int] = {}
    if min_beds is not None:
        params["beds_min"] = min_beds
    if max_price is not None:
        params["price_max"] = max_price
    if not for_sale:
        params["price_frequency"] = "per_month"
    kind = "for-sale" if for_sale else "to-rent"
    return [f"https://www.zoopla.co.uk/{kind}/property/{p}/" + (f"?{urlencode(params)}" if params else "") for p in paths]


def onthemarket_url(location: str, min_beds: int | None = None, max_beds: int | None = None, max_price: int | None = None,
                    for_sale: bool = True) -> str:
    kind = "for-sale" if for_sale else "to-rent"
    params: dict[str, int] = {}
    if min_beds is not None:
        params["min-bedrooms"] = min_beds
    if max_beds is not None:
        params["max-bedrooms"] = max_beds
    if max_price is not None:
        params["max-price"] = max_price
    return f"https://www.onthemarket.com/{kind}/property/{_location_slug(location)}/" + (f"?{urlencode(params)}" if params else "")


def _page_number(url: str) -> int:
    try:
        return int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
    except ValueError:
        return 0


def _results(site: str, url: str, structured: dict[str, Any] | None) -> Output | None:
    if not structured or structured.get("kind") != "search_results":
        return None
    data = structured.get("data") or {}
    listings = [r for r in data.get("results", []) if isinstance(r, dict)]
    out: Output = {"site": site, "status": "results" if listings else "no_matches", "search_url": url, "listings": listings,
                   "caveat": "Search cards are not confirmation of current availability; verify on detail pages."}
    if isinstance(data.get("total"), int):
        out["total"] = data["total"]
    if isinstance(data.get("truncated"), bool):
        out["truncated"] = data["truncated"]
    return out


async def rightmove_search(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                           fetcher: Fetcher | None = None) -> Output:
    """Entry point ``listings.rightmove_search``."""
    fetcher = fetcher or Fetcher(config or load_config())
    url = rightmove_url(arguments["location"], arguments.get("min_beds"), arguments.get("max_price"),
                        arguments.get("for_sale", True), arguments.get("max_beds"))
    try:
        html = await asyncio.wait_for(fetcher.fetch_html(url), timeout=PORTAL_SEARCH_TIMEOUT_S)
    except TimeoutError:
        return _unavailable("rightmove", url, f"timed out after {int(PORTAL_SEARCH_TIMEOUT_S)}s")
    except Exception as error:
        return _unavailable("rightmove", url, f"fetch failed ({error})")
    return _results("rightmove", url, rightmove.enrich(url, html)) or _unavailable("rightmove", url, "no recognised search results")


async def zoopla_search(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                        fetcher: Fetcher | None = None) -> Output:
    """Entry point ``listings.zoopla_search``."""
    fetcher = fetcher or Fetcher(config or load_config())
    urls = zoopla_urls(arguments["location"], arguments.get("min_beds"), arguments.get("max_price"), arguments.get("for_sale", True))
    reason = "unrecognised search response"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PORTAL_SEARCH_TIMEOUT_S
    for url in urls:
        try:
            html = await asyncio.wait_for(fetcher.fetch_html(url), timeout=max(0.0, deadline - loop.time()))
        except TimeoutError:
            reason = f"timed out after {int(PORTAL_SEARCH_TIMEOUT_S)}s"
            break
        except Exception as error:
            reason = f"fetch failed ({error})"
            continue
        found = _results("zoopla", url, zoopla.enrich(url, html))
        if found is not None:
            return found
    return _unavailable("zoopla", urls[-1], reason)


async def onthemarket_search(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                             fetcher: Fetcher | None = None) -> Output:
    """Entry point ``listings.onthemarket_search``: server HTML, following only observed pagination links."""
    fetcher = fetcher or Fetcher(config or load_config())
    url = onthemarket_url(arguments["location"], arguments.get("min_beds"), arguments.get("max_beds"), arguments.get("max_price"),
                          arguments.get("for_sale", True))
    max_pages = int(arguments.get("max_pages", 1))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PORTAL_SEARCH_TIMEOUT_S
    listings: dict[str, dict[str, Any]] = {}
    visited: list[str] = []
    warnings: list[str] = []
    total: int | None = None
    pending: str | None = url
    stop_reason = "no_observed_next_link"
    while pending is not None:
        if len(visited) >= max_pages:
            stop_reason = "max_pages"
            break
        page_url = pending
        structured: dict[str, Any] | None = None
        try:
            html = await asyncio.wait_for(fetcher.fetch_html(page_url), timeout=max(0.0, deadline - loop.time()))
        except TimeoutError:
            failure = f"timed out after {int(PORTAL_SEARCH_TIMEOUT_S)}s"
        except Exception as error:
            failure = f"fetch failed ({error})"
        else:
            structured = onthemarket.enrich(page_url, html)
            failure = "no recognised search results"
        if not structured or structured.get("kind") != "search_results":
            if not visited:
                return _unavailable("onthemarket", url, failure)
            warnings.append(f"Failed to read {page_url} ({failure}); retained earlier results")
            stop_reason = "page_failed"
            break
        data = structured["data"]
        visited.append(page_url)
        if total is None and isinstance(data.get("total"), int):
            total = data["total"]
        for card in data.get("results", []):
            if isinstance(card, dict):
                listings.setdefault(str(card.get("id")), card)
        current = _page_number(page_url)
        pending = next((link for link in data.get("pagination", []) if _page_number(link) == current + 1 and link not in visited), None)
    out: Output = {"site": "onthemarket", "search_url": url, "listings": list(listings.values()), "pages_visited": visited,
                   "pending_urls": [pending] if pending is not None else [], "warnings": warnings, "stop_reason": stop_reason,
                   "status": "partial" if warnings else ("results" if listings else "no_matches"),
                   "caveat": "Search cards and recency labels do not confirm current availability; verify on detail pages."}
    if total is not None:
        out["total"] = total
        out["truncated"] = len(listings) < total
    return out


async def agency_search(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                        fetcher: Fetcher | None = None) -> Output:
    """Entry point ``listings.agency_search``."""
    config = config or load_config()
    fetcher = fetcher or Fetcher(config)
    agency = str(arguments["agency"]).strip().lower()
    location = str(arguments["location"])
    impl = agencies.REGISTRY[agency]  # the input schema enumerates the agencies
    for_sale = bool(arguments.get("for_sale", False))
    filters = SearchFilters(max_price=arguments.get("max_price"), min_beds=arguments.get("min_beds"),
                            max_beds=arguments.get("max_beds"), postcode_districts=tuple(arguments.get("postcode_districts", ())))
    base: Output = {"site": agency, "agency": AGENCY_LABELS[agency], "search_url": impl.search_url(location, for_sale)}
    try:
        report = await asyncio.wait_for(
            impl.search_report(location, for_sale, filters, max_pages=int(arguments.get("max_pages", 5)),
                               fetch_html=fetcher.fetch_html, fetch_json=fetcher.fetch_json,
                               # Used only by agencies whose cards JavaScript fills in (js_search):
                               # browser, else rendering unblocker, else unavailable.
                               fetch_html_js=fetcher.fetch_html_js),
            timeout=AGENCY_SEARCH_TIMEOUT_S,
        )
    except TimeoutError:
        return {**base, "status": "unavailable", "reason": f"timed out after {int(AGENCY_SEARCH_TIMEOUT_S)}s",
                "listings": [], "caveat": UNAVAILABLE_CAVEAT}
    except Exception as error:  # AgencySearchUnavailable, fetch failures, parser errors
        reason = str(error) or type(error).__name__
        if error.__cause__ is not None and str(error.__cause__):
            reason += f": {error.__cause__}"
        return {**base, "status": "unavailable", "reason": reason, "listings": [], "caveat": UNAVAILABLE_CAVEAT}
    hits = [hit.to_dict() for hit in report.hits]
    status = "partial" if report.warnings or report.pending_urls else ("results" if hits else "no_matches")
    return {**base, "status": status, "listings": hits, "pages_visited": report.pages_visited, "pending_urls": report.pending_urls,
            "warnings": report.warnings, "stop_reason": report.stop_reason,
            "caveat": "Not an exhaustive inventory check. Verify location, criteria and availability on detail pages."}
