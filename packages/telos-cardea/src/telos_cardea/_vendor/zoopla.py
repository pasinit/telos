"""Zoopla HTML → structured extraction.

Zoopla search pages are usable in two common shapes:

- app data embedded as JSON in script tags
- rendered HTML with listing-card links to ``/details/<id>/``

The parser favours structured script data and falls back to visible card
text so browser-rendered pages still produce useful search results.
Returns ``None`` for non-Zoopla URLs and unrecognised page shapes.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_JSON_SCRIPT_RE = re.compile(
    r"<script[^>]*(?:type=[\"']application/(?:ld\+)?json[\"']|id=[\"']__NEXT_DATA__[\"'])[^>]*>(.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
_DETAIL_PATH_RE = re.compile(r"/(?:for-sale|to-rent|new-homes)/details/(\d+)/?")
_SEARCH_PATH_RE = re.compile(r"/(?:for-sale|to-rent)/(?:property|flats|houses|bungalows|studios|new-homes)/")
_PRICE_RE = re.compile(r"£[\d,]+(?:\s*(?:pcm|pw|per month|per week))?", re.IGNORECASE)
_BEDS_RE = re.compile(r"\b(\d+)\s+bed(?:s|rooms?)?\b", re.IGNORECASE)
_BATHS_RE = re.compile(r"\b(\d+)\s+bath(?:s|rooms?)?\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"\b(\d[\d,]*)\s*sq\s*ft\b", re.IGNORECASE)


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

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}


def matches(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host == "zoopla.co.uk" or host.endswith(".zoopla.co.uk")


def enrich(url: str, html: str) -> dict | None:
    if not matches(url):
        return None
    match = _DETAIL_PATH_RE.search(url)
    if match:
        return _enrich_listing(url, html, listing_id=match.group(1))
    if _SEARCH_PATH_RE.search(url):
        return _enrich_search(url, html)
    return None


def _enrich_listing(url: str, html: str, listing_id: str) -> dict | None:
    listing = _listing_from_json(url, html, listing_id) or _listing_from_html(url, html, listing_id)
    if listing is None:
        return None
    return {
        "kind": "listing",
        "summary": f"Zoopla listing — {listing.title or listing.address or listing.id}",
        "data": listing.to_dict(),
    }


def _enrich_search(url: str, html: str) -> dict | None:
    results = _search_results_from_json(url, html)
    if not results:
        results = _search_results_from_html(url, html)
    if not results:
        return None
    return {
        "kind": "search_results",
        "summary": f"Zoopla search — {len(results)} results",
        "data": {"results": [result.to_dict() for result in results], "count": len(results)},
    }


def _listing_from_json(url: str, html: str, listing_id: str) -> _Listing | None:
    candidates = _listing_candidates_from_json(url, html)
    for listing in candidates:
        if listing.id == listing_id:
            return listing
    return candidates[0] if candidates else None


def _search_results_from_json(url: str, html: str) -> list[_SearchResult]:
    out: list[_SearchResult] = []
    seen: set[str] = set()
    for listing in _listing_candidates_from_json(url, html):
        key = listing.id or listing.url
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(_SearchResult(
            site="zoopla",
            id=listing.id,
            url=listing.url,
            title=listing.title or listing.address,
            price=listing.price,
            summary=_summary(listing),
            photo=listing.photos[0] if listing.photos else "",
        ))
        if len(out) >= 25:
            break
    return out


def _listing_candidates_from_json(url: str, html: str) -> list[_Listing]:
    out: list[_Listing] = []
    seen: set[str] = set()
    for blob in _extract_json_blobs(html):
        for node in _walk(blob):
            if not isinstance(node, dict):
                continue
            listing = _listing_from_mapping(url, node)
            if listing is None:
                continue
            key = listing.id or listing.url
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(listing)
    return out


def _listing_from_mapping(base_url: str, data: dict[str, Any]) -> _Listing | None:
    listing_id = _first_text(data, "listingId", "listing_id", "propertyId", "property_id", "id")
    detail_url = _first_text(
        data,
        "url",
        "uri",
        "listingUrl",
        "listing_url",
        "detailUrl",
        "detailsUrl",
        "propertyUrl",
        "canonicalUrl",
    )
    nested_uris = data.get("listingUris")
    if not detail_url and isinstance(nested_uris, dict):
        detail_url = _first_text(nested_uris, "detail", "details", "web", "desktop")
    if detail_url:
        detail_url = urljoin(base_url, detail_url)
        match = _DETAIL_PATH_RE.search(detail_url)
        if match and not listing_id:
            listing_id = match.group(1)

    title = _first_text(data, "title", "headline", "displayAddress", "shortDescription", "name")
    address = _address_text(data.get("address")) or _first_text(data, "address", "displayAddress")
    price = _price_text(data.get("price")) or _price_text(data.get("pricing")) or _first_text(data, "price")

    if not (listing_id or detail_url) or not (title or address or price):
        return None

    description = _first_text(data, "description", "summary", "shortDescription", "propertySummary")
    photos = _photos(data)
    return _Listing(
        site="zoopla",
        id=listing_id or "",
        url=detail_url or base_url,
        title=title or address,
        address=address,
        price=price,
        beds=_first_int(data, "bedrooms", "beds", "numBedrooms", "numberOfBedrooms"),
        baths=_first_int(data, "bathrooms", "baths", "numBathrooms", "numberOfBathrooms"),
        sqft=_first_int(data, "sqft", "floorAreaSqFt", "sizeSqFt", "areaSqFt"),
        description=_strip_html(description)[:2000],
        photos=photos[:8],
        host_or_agent=_agent_text(data),
    )


def _listing_from_html(url: str, html: str, listing_id: str) -> _Listing | None:
    soup = BeautifulSoup(html, "html.parser")
    text = _clean_text(soup.get_text(" ", strip=True))
    if not text:
        return None
    title = _clean_text((soup.find("h1") or soup.find("title") or soup).get_text(" ", strip=True))
    description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        description = str(meta["content"])
    photos = [
        urljoin(url, str(img.get("src")))
        for img in soup.find_all("img")
        if img.get("src")
    ][:8]
    return _Listing(
        site="zoopla",
        id=listing_id,
        url=url,
        title=title,
        price=_regex_text(_PRICE_RE, text),
        beds=_regex_int(_BEDS_RE, text),
        baths=_regex_int(_BATHS_RE, text),
        sqft=_regex_int(_SQFT_RE, text),
        description=_strip_html(description or text)[:2000],
        photos=photos,
    )


def _search_results_from_html(url: str, html: str) -> list[_SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[_SearchResult] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href"))
        match = _DETAIL_PATH_RE.search(href)
        if not match or match.group(1) in seen:
            continue
        text = _clean_text(anchor.get_text(" ", strip=True))
        if not text:
            continue
        seen.add(match.group(1))
        out.append(_SearchResult(
            site="zoopla",
            id=match.group(1),
            url=urljoin(url, href),
            title=_title_from_card_text(text),
            price=_regex_text(_PRICE_RE, text),
            summary=_summary_from_text(text),
            photo=_nearby_image(anchor, url),
        ))
        if len(out) >= 25:
            break
    return out


def _extract_json_blobs(html: str) -> list[Any]:
    blobs: list[Any] = []
    for raw in _JSON_SCRIPT_RE.findall(html):
        try:
            blobs.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blobs


def _walk(value: Any) -> list[Any]:
    out = [value]
    if isinstance(value, dict):
        for inner in value.values():
            out.extend(_walk(inner))
    elif isinstance(value, list):
        for inner in value:
            out.extend(_walk(inner))
    return out


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _first_int(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"\d[\d,]*", value)
            if match:
                return int(match.group(0).replace(",", ""))
    return None


def _address_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    text = _first_text(value, "displayAddress", "streetAddress", "addressLine1", "line1")
    if text:
        return text
    parts = [
        str(value.get(key, "")).strip()
        for key in ("line1", "line2", "town", "city", "postcode")
        if str(value.get(key, "")).strip()
    ]
    return ", ".join(parts)


def _price_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return f"£{int(value):,}"
    if not isinstance(value, dict):
        return ""
    text = _first_text(value, "displayPrice", "label", "text", "price", "rent", "value")
    if text:
        return text
    amount = _first_int(value, "amount", "perMonth", "monthly", "pcm")
    return f"£{amount:,}" if amount is not None else ""


def _photos(data: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("image", "imageUrl", "mainImage", "mainImageUrl", "photo", "photos", "images"):
        value = data.get(key)
        if value:
            values.append(value)
    out: list[str] = []
    for value in values:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            text = _first_text(value, "url", "src", "href")
            if text:
                out.append(text)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    text = _first_text(item, "url", "src", "href")
                    if text:
                        out.append(text)
    return out


def _agent_text(data: dict[str, Any]) -> str:
    for key in ("agent", "branch", "agentBranch", "seller", "customer"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            text = _first_text(value, "name", "branchName", "displayName", "companyName")
            if text:
                return text
    return _first_text(data, "agentName", "branchName")


def _summary(listing: _Listing) -> str:
    parts = []
    if listing.beds is not None:
        parts.append(f"{listing.beds} beds")
    if listing.baths is not None:
        parts.append(f"{listing.baths} baths")
    if listing.sqft is not None:
        parts.append(f"{listing.sqft} sq ft")
    if listing.description:
        parts.append(listing.description[:160])
    return " | ".join(parts)


def _summary_from_text(text: str) -> str:
    parts = []
    beds = _regex_int(_BEDS_RE, text)
    baths = _regex_int(_BATHS_RE, text)
    sqft = _regex_int(_SQFT_RE, text)
    if beds is not None:
        parts.append(f"{beds} beds")
    if baths is not None:
        parts.append(f"{baths} baths")
    if sqft is not None:
        parts.append(f"{sqft} sq ft")
    return " | ".join(parts)


def _title_from_card_text(text: str) -> str:
    without_price = _PRICE_RE.sub("", text, count=1)
    without_counts = _BEDS_RE.sub("", without_price)
    without_counts = _BATHS_RE.sub("", without_counts)
    without_counts = _SQFT_RE.sub("", without_counts)
    return _clean_text(without_counts)[:180]


def _nearby_image(anchor: Any, base_url: str) -> str:
    container = anchor
    for _ in range(4):
        img = container.find("img") if hasattr(container, "find") else None
        if img and img.get("src"):
            return urljoin(base_url, str(img["src"]))
        container = getattr(container, "parent", None)
        if container is None:
            break
    return ""


def _regex_text(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0).strip() if match else ""


def _regex_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match.group(1).replace(",", "")) if match else None


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
