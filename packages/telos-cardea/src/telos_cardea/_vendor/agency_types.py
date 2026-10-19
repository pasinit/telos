"""Estate-agent search — domain types + agency labels (pure, no I/O).

`search` is a protocol (:class:`telos.transport.browser.agencies.AgencySearch`)
with one implementation per agency, so each can fetch its own way — scrape
HTML (Foxtons/Dexters/…) or call an internal JSON API (the SPA holdouts).
The types the protocol trades in live here in ``domain`` so the
``agency_search`` tool can consume them without importing ``transport``;
the implementations and the fetching live in ``transport``.
"""
import re
from dataclasses import asdict, dataclass, field


class AgencySearchUnavailable(RuntimeError):
    """The source could not establish a usable search result."""


@dataclass
class Listing:
    """A fully-resolved property record (from a detail page or an API)."""

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
class SearchHit:
    """A search candidate with fields observed on its card or API record.

    Missing fields require detail verification, not inference from neighbours.
    """

    site: str
    id: str
    url: str
    title: str
    price: str = ""
    beds: int | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", None)}


@dataclass
class SearchReport:
    """Observed results and coverage, not a claim about the site's whole inventory."""

    hits: list[SearchHit] = field(default_factory=list)
    pages_visited: list[str] = field(default_factory=list)
    pending_urls: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stop_reason: str = "single_response"


@dataclass
class SearchFilters:
    """Optional criteria for :func:`agency_search`. ``max_price`` is monthly
    rent (£ pcm) for lettings or the sale price (£) for sales — the agency
    impls normalise their own price units to this. Known card/API values are
    filtered; unknowns remain candidates requiring detail verification."""

    max_price: int | None = None
    min_beds: int | None = None
    max_beds: int | None = None
    postcode_districts: tuple[str, ...] = ()

    def accepts_location(self, text: str) -> bool:
        """Exclude known out-of-area districts; leave unknowns for verification."""
        if not self.postcode_districts:
            return True
        districts = re.findall(r"\b([a-z]{1,2}\d{1,2}[a-z]?)\b", text, re.I)
        if not districts:
            return True
        return any(
            re.fullmatch(re.escape(allowed) + r"[a-z]?", district, re.I)
            for allowed in self.postcode_districts for district in districts
        )

    def is_empty(self) -> bool:
        return self.max_price is None and self.min_beds is None and self.max_beds is None

    def accepts(self, *, price: int | None, beds: int | None) -> bool:
        """True if a listing with ``price`` (same unit as ``max_price``) and
        ``beds`` passes every set filter. Unknown fields (``None``) never
        exclude — better to surface a listing for the agent to check than to
        silently drop it on missing data."""
        if self.max_price is not None and price is not None and price > self.max_price:
            return False
        if self.min_beds is not None and beds is not None and beds < self.min_beds:
            return False
        if self.max_beds is not None and beds is not None and beds > self.max_beds:
            return False
        return True


# name -> human label, for the agency_search tool's schema/validation. The
# implementations (transport) are keyed by the same names.
AGENCY_LABELS: dict[str, str] = {
    "foxtons": "Foxtons",
    "dexters": "Dexters",
    "chestertons": "Chestertons",
    "hamptons": "Hamptons",
    "marshandparsons": "Marsh & Parsons",
    "openrent": "OpenRent",
    "ashponsonby": "Ash Ponsonby",
    "kfh": "KFH",
    "portico": "Portico",
    "kayandco": "Kay & Co",
    "parkheath": "Parkheath",
}


__all__ = ["Listing", "SearchHit", "SearchFilters", "SearchReport", "AgencySearchUnavailable", "AGENCY_LABELS"]
