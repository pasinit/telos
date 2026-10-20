"""Extract OnTheMarket's observed Next.js state without inferring missing facts."""

import json
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError


class _Image(BaseModel):
    url: str = Field(alias="largeUrl")


class _Card(BaseModel):
    id: str | int
    url: str = Field(alias="details-url")
    address: str = ""
    price: str = ""
    beds: int | None = Field(default=None, alias="bedrooms")
    baths: int | None = Field(default=None, alias="bathrooms")
    features: list[str] = Field(default_factory=list)
    label: str | None = Field(default=None, alias="main-label")
    recency: str | None = Field(default=None, alias="days-since-added-reduced")


class _Search(BaseModel):
    cards: list[_Card] = Field(alias="list")
    total: int = Field(alias="totalResults", ge=0)


class _Feature(BaseModel):
    feature: str


class _Property(BaseModel):
    id: str | int
    title: str = Field(alias="propertyTitle")
    address: str = Field(alias="displayAddress")
    price: str = ""
    beds: int | None = Field(default=None, alias="bedrooms")
    baths: int | None = Field(default=None, alias="bathrooms")
    sqft: float | None = Field(default=None, alias="minimumAreaSqFt")
    sqm: float | None = Field(default=None, alias="minimumAreaSqM")
    description: str = ""
    features: list[_Feature] = Field(default_factory=list)
    letting_details: dict[str, list[str]] = Field(default_factory=dict, alias="lettingDetails")
    images: list[_Image] = Field(default_factory=list)
    floorplans: list[_Image] = Field(default_factory=list)


def matches(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == "onthemarket.com" or host.endswith(".onthemarket.com")


def _state(soup: BeautifulSoup) -> dict:
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        return {}
    try:
        data = json.loads(script.get_text())
    except (ValueError, TypeError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("props"), dict) and "pageProps" in data["props"]:
        data = {"props": data["props"]["pageProps"]}  # telos-cardea: Next.js pageProps nesting
    for key in ("props", "initialReduxState"):
        if not isinstance(data, dict):
            return {}
        data = data.get(key)
    return data if isinstance(data, dict) else {}


def _observed_url(base: str, href: str) -> str | None:
    url = urljoin(base, href)
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and matches(url):
        return url
    return None


def enrich(url: str, html: str) -> dict | None:
    """Return the common browser enrichment shape, or None for unknown pages."""
    if not matches(url):
        return None
    soup = BeautifulSoup(html, "html.parser")
    state = _state(soup)
    detail = re.fullmatch(r"/details/(\d+)/?", urlparse(url).path)
    try:
        if detail:
            prop = _Property.model_validate(state.get("property"))
            if str(prop.id) != detail[1]:
                return None
            data = prop.model_dump(exclude={"images", "floorplans", "letting_details", "features"}, exclude_none=True)
            data.update(
                site="onthemarket", url=url.split("#")[0],
                description=BeautifulSoup(prop.description, "html.parser").get_text(" ", strip=True),
                features=[feature.feature for feature in prop.features],
                letting_details=prop.letting_details.get("items", []),
                photos=[image.url for image in prop.images],
                floorplans=[image.url for image in prop.floorplans],
            )
            return {"kind": "listing", "summary": f"OnTheMarket listing: {prop.address}", "data": data}
        if not urlparse(url).path.startswith(("/to-rent/", "/for-sale/")):
            return None
        search = _Search.model_validate(state.get("results"))
    except ValidationError:
        return None
    if search.total and not search.cards:
        return None

    results = {}
    for card in search.cards:
        link = _observed_url(url, card.url)
        if link is None or urlparse(link).path.rstrip("/") != f"/details/{card.id}":
            return None
        results[str(card.id)] = {**card.model_dump(exclude_none=True), "id": str(card.id), "url": link}
    # Keep observed pagination, including its actual filter parameters.
    pagination = []
    for anchor in soup.select('a[href]'):
        link = _observed_url(url, str(anchor["href"]))
        if link and "page" in parse_qs(urlparse(link).query) and urlparse(link).path.startswith(("/to-rent/", "/for-sale/")):
            if link not in pagination:
                pagination.append(link)
    return {
        "kind": "search_results", "summary": f"OnTheMarket search: {len(results)} listings on this page",
        "data": {"site": "onthemarket", "results": list(results.values()), "count": len(results),
                 "total": search.total, "coverage": "current_page", "pagination": pagination,
                 "availability_caveat": "Search cards and recency labels do not confirm current availability."},
    }


def image_urls(url: str, html: str) -> list[str] | None:
    """Prioritize listing floorplans over photos; never attach OTM advertisements."""
    result = enrich(url, html)
    if result is None:
        return [] if matches(url) else None
    if result["kind"] != "listing":
        return []
    data = result["data"]
    plans = data["floorplans"]
    selected = re.fullmatch(r"/(floorplans|photos)/(\d+)", urlparse(url).fragment)
    if selected:
        images = data[selected[1]]
        index = int(selected[2]) - 1
        return images[index:index + 1] if 0 <= index < len(images) else []
    return list(dict.fromkeys(plans + data["photos"]))[:5]
