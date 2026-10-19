"""Rightmove HTML → structured extraction.

Rightmove is a Next.js + React-Server-Components hybrid. Listing pages
embed the listing in ``window.__PAGE_MODEL = { "data": "[...flight...]" }``
where the inner string is a stringified flat array; every value
position in that array's structures is an integer index back into the
same array. Search pages embed results inside ``__NEXT_DATA__`` under
``props.pageProps.searchResults.properties``.

Called by :func:`telos.transport.browser.fetcher.fetch` and the
``BrowserSession`` after a successful page fetch. Returns ``None`` for
non-Rightmove URLs and unrecognised page shapes.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(\{.*?\})</script>',
    re.DOTALL,
)
_LISTING_PATH_RE = re.compile(r"/properties/(\d+)")
_PAGE_MODEL_MARKERS = ("window.__PAGE_MODEL", "window.PAGE_MODEL")


@dataclass
class _Listing:
    site: str
    id: str
    url: str
    title: str
    address: str = ""
    price: str = ""
    beds: int | None = None
    baths: int | None = None
    sqft: int | None = None
    description: str = ""
    photos: list[str] = field(default_factory=list)
    host_or_agent: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", None, [], {})}


@dataclass
class _SearchResult:
    site: str
    id: str
    url: str
    title: str
    price: str = ""
    summary: str = ""
    photo: str = ""
    beds: int | None = None
    availability_date: str = ""
    display_status: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}


def matches(url: str) -> bool:
    """True iff ``url`` belongs to Rightmove (incl. ``m.`` subdomain)."""
    host = (urlparse(url).hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host == "rightmove.co.uk" or host.endswith(".rightmove.co.uk")


def enrich(url: str, html: str) -> dict | None:
    """Parse a Rightmove URL's HTML into structured data, or ``None`` if
    the URL isn't a Rightmove listing/search page (or the shape changed).

    Return shape mirrors what the old adapter exposed: ``{kind, summary, data}``.
    """
    if not matches(url):
        return None
    m = _LISTING_PATH_RE.search(url)
    if m:
        return _enrich_listing(url, html, listing_id=m.group(1))
    if "/property-for-sale/" in url or "/property-to-rent/" in url:
        return _enrich_search(url, html)
    return None


def _enrich_listing(url: str, html: str, listing_id: str) -> dict | None:
    prop = _extract_listing_property_data(html)
    if not prop:
        blob = _extract_next_data(html)
        prop = (blob or {}).get("props", {}).get("pageProps", {}).get("propertyData") or {}
    if not prop:
        return None

    addr = prop.get("address") or {}
    prices = prop.get("prices") or {}
    bedrooms = prop.get("bedrooms")
    bathrooms = prop.get("bathrooms")
    sizings = prop.get("sizings") or []
    sqft = next(
        (s.get("maximumSize") for s in sizings
         if isinstance(s, dict) and s.get("unit") == "sqft"),
        None,
    )
    photos = [img.get("url", "") for img in (prop.get("images") or []) if isinstance(img, dict)][:8]
    agent = (prop.get("customer") or {}).get("branchDisplayName", "")
    text = (prop.get("text") or {}).get("description", "") or prop.get("description", "")

    listing = _Listing(
        site="rightmove",
        id=str(prop.get("id", listing_id)),
        url=url,
        title=prop.get("displayAddress") or addr.get("displayAddress", ""),
        address=addr.get("displayAddress", "") or addr.get("incode", ""),
        price=prices.get("primaryPrice") or prices.get("displayPrice") or "",
        beds=int(bedrooms) if isinstance(bedrooms, int) else None,
        baths=int(bathrooms) if isinstance(bathrooms, int) else None,
        sqft=int(sqft) if isinstance(sqft, (int, float)) else None,
        description=_strip_html(text)[:2000],
        photos=photos,
        host_or_agent=agent,
    )
    return {
        "kind": "listing",
        "summary": f"Rightmove listing — {listing.title or listing.address or listing.id}",
        "data": listing.to_dict(),
    }


def _enrich_search(url: str, html: str) -> dict | None:
    blob = _extract_next_data(html)
    if not blob:
        return None
    search = blob.get("props", {}).get("pageProps", {}).get("searchResults", {})
    results = search.get("properties")
    if not isinstance(results, list):
        return None
    out: list[dict] = []
    seen: set[str] = set()
    for r in results[:100]:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if str(r['id']) in seen:
            continue
        seen.add(str(r['id']))
        price = r.get("price") or {}
        prices = price.get("displayPrices") or [{}]
        res = _SearchResult(
            site="rightmove",
            id=str(r.get("id", "")),
            url=f"https://www.rightmove.co.uk/properties/{r.get('id')}" if r.get("id") else url,
            title=r.get("displayAddress") or "",
            price=prices[0].get("displayPrice", ""),
            summary=str(r.get("summary") or r.get("propertySubType") or "")[:1000],
            photo=(r.get("propertyImages") or {}).get("mainImageSrc", ""),
            beds=r.get("bedrooms"),
            availability_date=r.get("letAvailableDate") or "",
            display_status=r.get("displayStatus") or "",
        )
        out.append(res.to_dict())
    return {
        "kind": "search_results",
        "summary": f"Rightmove search — {len(out)} results",
        "data": {"results": out, "count": len(out), "total": search.get("resultCount"),
                 "coverage": "current_page", "returned": len(out),
                 "truncated": len(results) > 100,
                 "pagination": search.get("pagination"),
                 "availability_caveat": "Search-card dates are not confirmation of current availability."},
    }


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _extract_next_data(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _extract_listing_property_data(html: str) -> dict | None:
    raw = _extract_page_model_object(html)
    if raw is None:
        return None
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        return None
    data = outer.get("data") if isinstance(outer, dict) else None
    if isinstance(data, str):
        try:
            table = json.loads(data)
        except json.JSONDecodeError:
            return None
    else:
        table = data
    if not isinstance(table, list) or not table or not isinstance(table[0], dict):
        return None
    prop_idx = table[0].get("propertyData")
    if not isinstance(prop_idx, int) or not (0 <= prop_idx < len(table)):
        return None
    resolved = _resolve_flight(table[prop_idx], table)
    return resolved if isinstance(resolved, dict) else None


def _extract_page_model_object(html: str) -> str | None:
    for marker in _PAGE_MODEL_MARKERS:
        idx = html.find(marker)
        if idx == -1:
            continue
        eq = html.find("=", idx + len(marker))
        if eq == -1:
            continue
        brace = html.find("{", eq)
        if brace == -1:
            continue
        end = _find_matching_brace(html, brace)
        if end != -1:
            return html[brace : end + 1]
    return None


def _find_matching_brace(s: str, start: int) -> int:
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _resolve_flight(value: Any, table: list, depth: int = 0) -> Any:
    if depth > 60:
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if 0 <= value < len(table):
            inner = table[value]
            if isinstance(inner, (int, bool)):
                return inner
            return _resolve_flight(inner, table, depth + 1)
        return value
    if isinstance(value, dict):
        return {k: _resolve_flight(v, table, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_flight(v, table, depth + 1) for v in value]
    return value


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
