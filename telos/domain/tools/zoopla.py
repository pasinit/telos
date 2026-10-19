"""``zoopla_search`` tool — builds a Zoopla search URL and hands it to
the deployment-wide ``web_fetch`` callable.

HTML extraction happens in :mod:`telos.transport.browser.zoopla`, called
from inside ``web_fetch`` after the page is fetched. This module only
declares the LLM-facing tool.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for


class ZooplaSearchInput(BaseModel):
    location: str = Field(
        description="UK location (postcode, town, or area), e.g. 'Brixton', 'SW7', 'Manchester'.",
    )
    min_beds: int | None = Field(
        default=None,
        description="Minimum number of bedrooms.",
    )
    max_price: int | None = Field(
        default=None,
        description="Maximum price in GBP. For rentals this is monthly rent.",
    )
    for_sale: bool = Field(
        default=True,
        description="True for properties for sale (default), False for to-rent.",
    )


class ZooplaSearchTool:
    """``zoopla_search`` — search Zoopla via ``web_fetch``.

    ``web_fetch`` runs Zoopla pages through
    :func:`telos.transport.browser.zoopla.enrich`, returning structured
    listing data when the page shape is recognised.
    """

    name = "zoopla_search"
    description = (
        "Search Zoopla for UK property listings. Builds a search URL from "
        "the location/filters and returns structured results when available."
    )

    def __init__(self, web_fetch: Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._web_fetch = web_fetch

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: ZooplaSearchInput, ctx: ToolContext) -> ToolResult:
        urls = _build_search_urls(
            location=inp.location,
            min_beds=inp.min_beds,
            max_price=inp.max_price,
            for_sale=inp.for_sale,
        )
        reason = "unrecognised search response"
        for url in urls:
            try:
                result = await self._web_fetch(url)
            except Exception as exc:
                reason = f"fetch failed ({type(exc).__name__})"
                continue
            status = result.get("status_code", 200)
            if status >= 400:
                reason = f"HTTP {status}"
            elif _has_search_results(result):
                return ToolResult(text=result["text"])
        return ToolResult(text=f"Zoopla search unavailable: {reason}. This is not evidence of zero inventory.", is_error=True)


def _build_search_url(
    location: str,
    min_beds: int | None = None,
    max_price: int | None = None,
    for_sale: bool = True,
    *,
    location_path: str | None = None,
) -> str:
    location_path = location_path or _location_path(location)
    base = (
        f"https://www.zoopla.co.uk/for-sale/property/{location_path}/"
        if for_sale
        else f"https://www.zoopla.co.uk/to-rent/property/{location_path}/"
    )
    params: dict[str, str | int] = {}
    if min_beds is not None:
        params["beds_min"] = min_beds
    if max_price is not None:
        params["price_max"] = max_price
    if not for_sale:
        params["price_frequency"] = "per_month"
    return base if not params else f"{base}?{urlencode(params)}"


def _build_search_urls(
    location: str,
    min_beds: int | None = None,
    max_price: int | None = None,
    for_sale: bool = True,
) -> list[str]:
    primary_path = _location_path(location)
    paths = [primary_path]
    fallback_path = _london_fallback_path(location, primary_path)
    if fallback_path and fallback_path not in paths:
        paths.append(fallback_path)
    return [
        _build_search_url(
            location=location,
            min_beds=min_beds,
            max_price=max_price,
            for_sale=for_sale,
            location_path=path,
        )
        for path in paths
    ]


_POSTCODE_RE = re.compile(r"^[a-z]{1,2}\d[a-z\d]?(-?\d[a-z]{2})?$")


def _location_path(location: str) -> str:
    slug = _location_slug(location)
    if not slug:
        return "uk"

    parts = slug.split("-")
    if _POSTCODE_RE.fullmatch(slug):
        return slug

    if "london" in parts:
        area = "-".join(part for part in parts if part not in {"london", "uk", "england"})
        return f"london/{area}" if area else "london"

    return slug


def _london_fallback_path(location: str, primary_path: str) -> str | None:
    slug = _location_slug(location)
    if "/" in primary_path or "-" in slug or slug in {"uk", "london"}:
        return None
    if _POSTCODE_RE.fullmatch(slug):
        return None
    return f"london/{slug}"


def _has_search_results(result: dict[str, Any]) -> bool:
    structured = result.get("structured")
    return isinstance(structured, dict) and structured.get("kind") == "search_results"


def _location_slug(location: str) -> str:
    parts: list[str] = []
    current: list[str] = []
    for char in location.strip().lower():
        if char.isalnum():
            current.append(char)
        elif current:
            parts.append("".join(current))
            current = []
    if current:
        parts.append("".join(current))
    return "-".join(parts) or "uk"
