"""Estate-agent search — one implementation per agency behind a protocol.

`search` is a protocol (:class:`AgencySearch`); each agency implements it
its own way, so heterogeneous sites coexist cleanly:

- HTML agencies (Foxtons, Dexters, Chestertons, Hamptons, Ash Ponsonby) —
  :class:`_HtmlAgency`: ``search`` renders the search URL and extracts the
  listing detail URLs; a detail page resolves to a full record via a parse
  ladder (schema.org property JSON-LD → Open Graph/`<title>` fallback).
- Marsh & Parsons and OpenRent use observed listing URL shapes. Access may
  still be challenged; an unrecognised page is an error, not empty inventory.

The fetcher's ``_enrich_known_site`` still calls :func:`enrich` (pure, HTML
in → structured dict out) so ``web_fetch`` on any agency URL keeps working.
The ``agency_search`` tool calls :func:`make_search_dispatch`, whose impls
fetch their own way via the injected ``fetch_html`` / ``fetch_json``.
"""
import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from urllib.parse import ParseResult, parse_qs, quote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from telos.domain.agencies import AgencySearchUnavailable, Listing, SearchFilters, SearchHit, SearchReport

FetchHtml = Callable[[str], Awaitable[str]]
FetchJson = Callable[[str], Awaitable[object]]

_LDJSON_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.DOTALL)
_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_PROPERTY_TYPES = {"House", "Apartment", "Residence", "SingleFamilyResidence", "Product"}
_BEDS_RE = re.compile(r"\b(\d+)\s*bed", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+)\s*bath", re.IGNORECASE)
_PRICE_RE = re.compile(r"£\s?([\d,]+(?:\.\d{1,2})?)\s*\(?\s*(pcm|pw|p/w|per week|per month|per annum|pa)?\b\)?", re.IGNORECASE)
_ADDR_RE = re.compile(r"\bin\s+([A-Z][^|£]+?)(?:\s*[-–]\s*£|\s*\(Ref|\s*\||$)", re.IGNORECASE)
_SLUG_BEDS_RE = re.compile(r"(\d+)-bed", re.IGNORECASE)  # "2-bedroom-…" in a detail-URL slug
_PAGINATION_LABEL_RE = re.compile(
    r"^(?:(?:next|last|previous)(?: page)?|(?:load|show) more(?: properties| results)?)$", re.I,
)
# Cards for already-gone stock are still listed by most agencies; these are the
# badges they use ("Let Agreed", "Let (Tenant Info)", "Under Offer", "SSTC").
# Matched against a listing's own card segment so unavailable stock is dropped.
_UNAVAILABLE_RE = re.compile(
    r"\b(let\s*agreed|under\s*offer|sold\s*stc|sstc|sale\s*agreed|tenancy\s*agreed|"
    r"let\s*\(|now\s*let|reserved|agreement\s+signed)\b",
    re.IGNORECASE,
)


def _price_pcm(text: str) -> int | None:
    """Prefer explicit monthly rent; otherwise normalise the first price."""
    matches = list(_PRICE_RE.finditer(text))
    m = next((m for m in matches if (m.group(2) or '').lower() in ('pcm', 'per month')), next(iter(matches), None))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit in ("pw", "p/w", "per week"):
        return round(num * 52 / 12)
    if unit in ("pa", "per annum"):
        return round(num / 12)
    return round(num)


def _slug(location: str) -> str:
    return quote(location.strip().lower().replace(",", "").replace(" ", "-"))


def _bare_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().lstrip(".")
    return host[4:] if host.startswith("www.") else host


# ── protocol ──────────────────────────────────────────────────────────

class AgencySearch(ABC):
    name: str
    label: str
    host: str
    detail_url_re: re.Pattern

    def matches(self, url: str) -> bool:
        h = _bare_host(url)
        return h == self.host or h.endswith("." + self.host)

    @abstractmethod
    def search_url(self, location: str, for_sale: bool) -> str: ...

    @abstractmethod
    async def search(self, location: str, for_sale: bool, filters: SearchFilters, *,
                     fetch_html: FetchHtml, fetch_json: FetchJson,
                     fetch_html_js: FetchHtml | None = None) -> list[SearchHit]: ...

    def enrich_detail(self, url: str, html: str) -> dict | None:
        return None

    def parse_search(self, url: str, html: str) -> dict | None:
        return None

    async def search_report(self, location: str, for_sale: bool, filters: SearchFilters, *, max_pages: int,
                            fetch_html: FetchHtml, fetch_json: FetchJson, fetch_html_js: FetchHtml | None = None) -> SearchReport:
        hits = await self.search(location, for_sale, filters, fetch_html=fetch_html,
                                 fetch_json=fetch_json, fetch_html_js=fetch_html_js)
        return SearchReport(hits=hits, pages_visited=[self.search_url(location, for_sale)])


# ── shared HTML parsing ──────────────────────────────────────────────

def _iter_ldjson(html: str):
    for m in _LDJSON_RE.finditer(html):
        try:
            d = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        yield from (d if isinstance(d, list) else [d])


def _property_ld(html: str) -> dict | None:
    for d in _iter_ldjson(html):
        if not isinstance(d, dict):
            continue
        t = d.get("@type")
        types = t if isinstance(t, list) else [t]
        if _PROPERTY_TYPES.intersection(types) and ("numberOfBedrooms" in d or "offers" in d or "address" in d):
            return d
    return None


def _og_tags(html: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for m in _META_RE.finditer(html):
        tag = m.group(0)
        key = re.search(r'(?:property|name)=["\']([^"\']+)["\']', tag)
        val = re.search(r'content=["\']([^"\']*)["\']', tag)
        if key and val:
            tags.setdefault(key.group(1).lower(), val.group(1))
    t = _TITLE_RE.search(html)
    if t:
        tags.setdefault("<title>", re.sub(r"\s+", " ", t.group(1)).strip())
    return tags


def _int(m: re.Match | None) -> int | None:
    return int(m.group(1)) if m else None


def _listing_from_ld(agency: "AgencySearch", url: str, lid: str, ld: dict) -> Listing:
    addr = ld.get("address") or {}
    address = ", ".join(str(x) for x in (addr.get("streetAddress"), addr.get("addressLocality"), addr.get("postalCode")) if x)
    spec = (ld.get("offers") or {}).get("priceSpecification") or {}
    price = ""
    if isinstance(spec.get("price"), (int, float)):
        price = f"{'£' if spec.get('priceCurrency') == 'GBP' else ''}{int(spec['price']):,}"
    fs = ld.get("floorSize") or {}
    sqft = int(fs["value"]) if isinstance(fs.get("value"), (int, float)) else None  # value is sqft on UK sites
    beds, baths = ld.get("numberOfBedrooms"), ld.get("numberOfBathroomsTotal")
    img = ld.get("image")
    photos = [img] if isinstance(img, str) else [p for p in (img or []) if isinstance(p, str)][:8]
    return Listing(
        site=agency.name, id=lid, url=url, title=ld.get("name") or address, address=address, price=price,
        beds=beds if isinstance(beds, int) else None, baths=baths if isinstance(baths, int) else None,
        sqft=sqft, description=str(ld.get("description") or "")[:2000], photos=photos,
        host_or_agent=(ld.get("offers") or {}).get("seller", {}).get("name", "") or agency.label,
    )


def _listing_from_og(agency: "AgencySearch", url: str, lid: str, og: dict[str, str]) -> Listing | None:
    blob = " ".join(v for k, v in og.items() if k in ("og:title", "og:description", "description", "<title>"))
    if not blob.strip():
        return None
    pm = _PRICE_RE.search(blob)
    price = f"£{pm.group(1)}{' ' + pm.group(2).lower() if pm.group(2) else ''}" if pm else ""
    am = _ADDR_RE.search(blob)
    address = am.group(1).strip().rstrip(".").strip() if am else ""
    img = og.get("og:image") or og.get("twitter:image", "")
    title = og.get("og:title") or og.get("<title>") or address
    return Listing(
        site=agency.name, id=lid, url=url,
        title=re.sub(r"\s*\|\s*" + re.escape(agency.label) + r"\s*$", "", title).strip(),
        address=address, price=price, beds=_int(_BEDS_RE.search(blob)), baths=_int(_BATHS_RE.search(blob)),
        description=og.get("og:description") or og.get("description", ""),
        photos=[img] if img else [], host_or_agent=agency.label,
    )


# ── HTML agency base ─────────────────────────────────────────────────

class _HtmlAgency(AgencySearch):
    """Renders the search page, extracts detail URLs; detail pages resolve
    via the JSON-LD → OG parse ladder."""

    #: Set on agencies whose *server* HTML lacks card data that JS injects
    #: (e.g. Dexters prices) — search then uses the browser-only fetcher
    #: when one is provided, instead of the curl-vs-browser race.
    js_search = False
    card_selector = 'article, .property-card, .pegasus-property-card, .property-tile, .property-card-wrapper, li.result, ul.item-list > li, a.search-property-card'

    async def search(self, location: str, for_sale: bool, filters: SearchFilters, *,
                     fetch_html: FetchHtml, fetch_json: FetchJson,
                     fetch_html_js: FetchHtml | None = None) -> list[SearchHit]:
        fetcher = fetch_html_js if (self.js_search and fetch_html_js is not None) else fetch_html
        html = await fetcher(self.search_url(location, for_sale))
        if not self._listing_links(BeautifulSoup(html, 'html.parser')):
            raise AgencySearchUnavailable(
                f"{self.label}: no recognisable listing cards at {self.search_url(location, for_sale)}. "
                "The source may be blocked, changed or empty; do not interpret this as zero inventory."
            )
        return self._hits(html, filters, for_sale=for_sale)

    def _listing_links(self, node: Tag) -> list[tuple[Tag, re.Match]]:
        anchors = ([node] if node.name == 'a' and node.get('href') else []) + list(node.select('a[href]'))
        links = []
        for anchor in anchors:
            href = str(anchor.get('href', ''))
            try:
                url = urljoin(f'https://www.{self.host}', href)
                parsed = urlparse(url)
            except ValueError:
                continue
            if not self.matches(url) or parsed.scheme not in ('https', 'http'):
                continue
            match = self.detail_url_re.search(parsed.path)
            if match:
                links.append((anchor, match))
        return links

    def _pagination(self, url: str, soup: BeautifulSoup) -> list[str]:
        urls = []
        source = urlparse(url)
        routes = [source]
        canonical = soup.select_one('link[rel="canonical"][href]')
        if canonical:
            try:
                parsed = urlparse(urljoin(url, str(canonical['href'])))
                if parsed.netloc == source.netloc and parsed.scheme in ('http', 'https'):
                    routes.append(parsed)
            except ValueError:
                pass
        for anchor in soup.select('a[href]'):
            try:
                target = urljoin(url, str(anchor['href'])).split('#')[0]
                parsed = urlparse(target)
            except ValueError:
                continue
            if parsed.netloc != source.netloc or parsed.scheme not in ('http', 'https') or self.detail_url_re.search(parsed.path):
                continue
            if not any(self._page_route(parsed) == self._page_route(route) for route in routes):
                continue
            label = anchor.get_text(' ', strip=True) or str(anchor.get('aria-label', ''))
            query = parse_qs(parsed.query)
            page_link = re.search(r'/page-\d+/?$', parsed.path) or any(k in query for k in ('page', 'start'))
            if page_link and (label.isdigit() or _PAGINATION_LABEL_RE.fullmatch(label) or 'next' in anchor.get('rel', [])):
                if target != url and target not in urls:
                    urls.append(target)
        return urls

    @staticmethod
    def _page_route(parsed: ParseResult) -> tuple[str, dict[str, list[str]]]:
        path = re.sub(r'/page-\d+/?$', '', parsed.path).rstrip('/')
        query = {key: value for key, value in parse_qs(parsed.query).items() if key not in ('page', 'start')}
        return path, query

    async def search_report(self, location: str, for_sale: bool, filters: SearchFilters, *, max_pages: int,
                            fetch_html: FetchHtml, fetch_json: FetchJson, fetch_html_js: FetchHtml | None = None) -> SearchReport:
        fetcher = fetch_html_js if self.js_search and fetch_html_js is not None else fetch_html
        report = SearchReport(stop_reason='no_observed_next_link')
        pending = [self.search_url(location, for_sale)]
        seen: set[str] = set()
        hits: dict[str, SearchHit] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90
        while pending and len(report.pages_visited) < max_pages:
            if loop.time() >= deadline:
                report.stop_reason = 'time_budget'
                break
            url = pending.pop(0)
            seen.add(url)
            try:
                html = await asyncio.wait_for(fetcher(url), timeout=min(45, deadline - loop.time()))
                soup = BeautifulSoup(html, 'html.parser')
                if not self._listing_links(soup):
                    raise AgencySearchUnavailable('No recognised listing cards; may be empty, blocked or changed')
                page_hits = self._hits(html, filters, for_sale=for_sale)
                next_pages = self._pagination(url, soup)
            except Exception as exc:
                if not report.pages_visited:
                    raise AgencySearchUnavailable(f'{self.label}: search page unavailable ({type(exc).__name__})') from exc
                pending.insert(0, url)
                report.warnings.append(f'Failed to read {url} ({type(exc).__name__}); retained earlier results')
                report.stop_reason = 'page_failed'
                break
            report.pages_visited.append(url)
            for hit in page_hits:
                hits.setdefault(hit.id, hit)
            for link in next_pages:
                if link not in seen and link not in pending:
                    pending.append(link)
            if any(_PAGINATION_LABEL_RE.fullmatch(control.get_text(' ', strip=True) or str(control.get('aria-label', '')))
                   for control in soup.select('button, [role="button"], a:not([href]), a[href="#"], a[href^="javascript:"]')):
                warning = 'Button-based pagination may contain more results; only observed page links are followed.'
                if warning not in report.warnings:
                    report.warnings.append(warning)
        if pending and report.stop_reason == 'no_observed_next_link':
            report.stop_reason = 'page_limit'
        report.hits = list(hits.values())
        report.pending_urls = pending
        return report

    def _hits(self, html: str, filters: SearchFilters | None = None, *, for_sale: bool = False) -> list[SearchHit]:
        """Only attribute facts within a card containing one distinct listing.

        Orphan links remain discoverable, but never borrow price or status
        from document-order neighbours or unparsed hydration scripts.
        """
        filters = filters or SearchFilters()
        soup = BeautifulSoup(html, 'html.parser')
        for node in soup.select('script, style, noscript'):
            node.decompose()
        records = {match[1]: (anchor, match, None) for anchor, match in self._listing_links(soup)}
        for card in soup.select(self.card_selector):
            links = self._listing_links(card)
            if len({match[1] for _, match in links}) == 1:
                anchor, match = links[0]
                records[match[1]] = (anchor, match, card)
        hits = []
        for lid, (anchor, match, card) in records.items():
            path = match[0]
            text = card.get_text(' ', strip=True) if card is not None else ''
            labels = ' '.join(str(node.get(attr, '')) for node in card.select('[alt], [aria-label]')
                              for attr in ('alt', 'aria-label')) if card is not None else ''
            if _UNAVAILABLE_RE.search(text + ' ' + labels) or not filters.accepts_location(path + ' ' + text):
                continue
            beds = self._card_beds(card, path, text + ' ' + labels)
            price = _price_pcm(text)
            if for_sale:
                amount = _PRICE_RE.search(text)
                price = round(float(amount[1].replace(',', ''))) if amount else None
            if not filters.accepts(price=price, beds=beds):
                continue
            hits.append(SearchHit(
                site=self.name, id=lid, url=urljoin(f'https://www.{self.host}', str(anchor['href'])),
                title=(text or anchor.get_text(' ', strip=True) or f'{self.label} listing {lid}')[:180],
                price=(f'£{price:,}' + ('' if for_sale else ' pcm')) if price is not None else '', beds=beds,
                summary='Verify full detail page and current availability.' + (' Price or bedrooms unknown.' if price is None or beds is None else ''),
            ))
        return hits

    def _card_beds(self, card: Tag | None, path: str, text: str) -> int | None:
        match = _SLUG_BEDS_RE.search(path) or _BEDS_RE.search(text) or re.search(r'\bBedrooms?\s+(\d+)\b', text, re.I)
        if match:
            return int(match[1])
        if 'studio' in path.lower():
            return 0
        if card is not None:
            node = card.select_one('.property-card__bedbath-item--bed')
            if node and node.get_text(strip=True).isdigit():
                return int(node.get_text(strip=True))
        words = re.search(r'\b(one|two|three|four|five|six|seven|eight|nine|ten)[ -]+bedroom', text, re.I)
        if words:
            return ('one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten').index(words[1].lower()) + 1
        return None

    def parse_search(self, url: str, html: str) -> dict | None:
        hits = self._hits(html)
        if not hits:
            return None
        return {"kind": "search", "summary": f"{len(hits)} {self.label} listing(s) — fetch a url for details",
                "data": [h.to_dict() for h in hits]}

    def enrich_detail(self, url: str, html: str) -> dict | None:
        m = self.detail_url_re.search(urlparse(url).path)
        lid = m.group(1) if m else url
        ld = _property_ld(html)
        listing = _listing_from_ld(self, url, lid, ld) if ld is not None else _listing_from_og(self, url, lid, _og_tags(html))
        if listing is None:
            return None
        summary = " — ".join(x for x in (listing.title, listing.price) if x) or listing.url
        return {"kind": "listing", "summary": summary, "data": listing.to_dict()}


# ── implementations ──────────────────────────────────────────────────

class FoxtonsSearch(_HtmlAgency):
    """Foxtons exposes a structured JSON search API
    (``/api/v2/property-search/pins``) that returns every listing in an area —
    with bedrooms and price — in one call. So lettings ``search`` hits carry
    price + beds directly and are filtered here (no per-listing fetch needed);
    sales fall back to the HTML URL-extraction path. Detail pages still resolve
    via JSON-LD (see :class:`_HtmlAgency`).

    Price unit note: the pins API returns lettings ``priceFrom`` as £/week
    (verified: the Seymour Place 5-bed reads 2925 here vs £12,675 pcm in its
    JSON-LD — 2925 x 52/12); we convert to £ pcm to match the tool's filter.
    """
    name, label, host = "foxtons", "Foxtons", "foxtons.co.uk"
    detail_url_re = re.compile(r"/properties-(?:to-rent|for-sale|to-let)/[A-Za-z0-9]{1,4}/([a-z]{2,}\d{4,})")

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.foxtons.co.uk/{'properties-for-sale' if for_sale else 'properties-to-rent'}/{_slug(location)}"

    def _api_url(self, location: str) -> str:
        # InstructionType LON+BDC = lettings; PageSize=1000 pulls the whole area
        # (the site itself fetches all then filters client-side, so we do too).
        return (
            "https://www.foxtons.co.uk/api/v2/property-search/pins?"
            f"FreeText={quote(location.strip())}"
            "&PageSize=1000&OrderBy=Price&OrderDirection=Asc&Page=1"
            "&IncludeSummary=true&InstructionType=LON&InstructionType=BDC"
        )

    async def search_report(self, location: str, for_sale: bool, filters: SearchFilters, *, max_pages: int,
                            fetch_html: FetchHtml, fetch_json: FetchJson, fetch_html_js: FetchHtml | None = None) -> SearchReport:
        if for_sale:
            return await super().search_report(location, for_sale, filters, max_pages=max_pages, fetch_html=fetch_html,
                                               fetch_json=fetch_json, fetch_html_js=fetch_html_js)
        hits = await self.search(location, for_sale, filters, fetch_html=fetch_html, fetch_json=fetch_json)
        return SearchReport(hits=hits, pages_visited=[self._api_url(location)],
                            warnings=['Foxtons API response is limited to 1000 source records; not an exhaustive inventory claim.'])

    async def search(self, location: str, for_sale: bool, filters: SearchFilters, *,
                     fetch_html: FetchHtml, fetch_json: FetchJson,
                     fetch_html_js: FetchHtml | None = None) -> list[SearchHit]:
        if for_sale:  # sale prices/instruction types differ — use the HTML path
            return await super().search(location, for_sale, filters,
                                        fetch_html=fetch_html, fetch_json=fetch_json, fetch_html_js=fetch_html_js)
        data = await fetch_json(self._api_url(location))
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise AgencySearchUnavailable("Foxtons returned an unrecognised API response")
        if any(not isinstance(item, dict) or not item.get('propertyReference') for item in items):
            raise AgencySearchUnavailable("Foxtons returned malformed listing records")
        return [h for it in items if isinstance(it, dict) for h in self._hit_from_api(it, filters)]

    def _hit_from_api(self, it: dict, filters: SearchFilters) -> list[SearchHit]:
        """One API item → zero or one :class:`SearchHit` (empty list when the
        item is unavailable, fails a filter, or lacks a usable reference)."""
        # The pins API returns let/sold stock alongside available listings —
        # drop anything already gone (status flags are authoritative here).
        if it.get("hasRecentlyBeenLet") or it.get("hasRecentlyBeenSold") or it.get("isUnderOffer"):
            return []
        beds = it.get("bedrooms") if isinstance(it.get("bedrooms"), int) else None
        weekly = it.get("priceFrom")
        pcm = round(weekly * 52 / 12) if isinstance(weekly, (int, float)) and weekly else None
        if not filters.accepts(price=pcm, beds=beds):
            return []
        ref = str(it.get("propertyReference") or "").strip()
        if not ref:
            return []
        postcode = str(it.get("postcode") or "")
        district = postcode.split(" ")[0] if postcode else _slug(str(it.get("location") or "london"))
        street, loc = str(it.get("street") or ""), str(it.get("location") or "")
        if not filters.accepts_location(postcode):
            return []
        baths = it.get("bathrooms") if isinstance(it.get("bathrooms"), int) else None
        summary = ", ".join(x for x in (f"{beds} bed" if beds is not None else "",
                                        f"{baths} bath" if baths is not None else "", postcode) if x)
        return [SearchHit(
            site=self.name, id=ref.lower(),
            url=f"https://www.foxtons.co.uk/properties-to-rent/{district}/{ref.lower()}",
            title=", ".join(x for x in (street, loc, postcode) if x) or f"{self.label} {ref}",
            price=f"£{pcm:,} pcm" if pcm else "", beds=beds, summary=summary,
        )]


class DextersSearch(_HtmlAgency):
    name, label, host = "dexters", "Dexters", "dexters.co.uk"
    detail_url_re = re.compile(r"/property-(?:for-rent|for-sale)/[a-z0-9-]+/(\d{4,})")
    # Card prices ("£3,462 Pw / £15,000 Pcm") are injected client-side — the
    # server HTML the curl race returns has none, so force the browser path.
    js_search = True

    def search_url(self, location: str, for_sale: bool) -> str:
        section = "property-sales" if for_sale else "property-lettings"
        verb = "properties-for-sale-in" if for_sale else "properties-to-rent-in"
        return f"https://www.dexters.co.uk/{section}/{verb}-{_slug(location)}"


class _HomeflowAgency(_HtmlAgency):
    """Chestertons, Hamptons and many UK agencies run on Homeflow — detail
    URLs are /properties/{id}/(lettings|sales)."""
    detail_url_re = re.compile(r"/properties/(\d{4,})/(?:lettings|sales)")


class ChestertonsSearch(_HomeflowAgency):
    name, label, host = "chestertons", "Chestertons", "chestertons.co.uk"

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.chestertons.co.uk/london/{_slug(location)}/{'sales' if for_sale else 'lettings'}"


class HamptonsSearch(_HomeflowAgency):
    name, label, host = "hamptons", "Hamptons", "hamptons.co.uk"

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.hamptons.co.uk/search/?channel={'sales' if for_sale else 'lettings'}&location={_slug(location)}"


class AshPonsonbySearch(_HtmlAgency):
    name, label, host = "ashponsonby", "Ash Ponsonby", "ashponsonby.com"
    detail_url_re = re.compile(r"/(?:to-rent|for-sale)/[^/\"]+/[^/\"]+/[^/\"]+/(\d{4,})")

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.ashponsonby.com/{'for-sale' if for_sale else 'to-rent'}"


class KfhSearch(_HtmlAgency):
    """Kinleigh Folkard & Hayward — server-rendered; detail URLs are a long
    descriptive slug ending in the numeric listing id:
    ``/property-to-rent/2-bedroom-apartment-to-rent-in-hans-place-…-sw1x-9021/``."""
    name, label, host = "kfh", "KFH", "kfh.co.uk"
    card_selector = '.sales-wrap'
    # Gatsby's server HTML is a generic London shell. Rendering applies the
    # area filter and fetches the actual local results from the search API.
    js_search = True
    detail_url_re = re.compile(r"/property-(?:to-rent|for-sale)/[a-z0-9-]+-(\d{4,})/")

    def search_url(self, location: str, for_sale: bool) -> str:
        section = "for-sale" if for_sale else "to-rent"
        return f"https://www.kfh.co.uk/property/{section}/in-{_slug(location)}/"


class PorticoSearch(_HtmlAgency):
    """Portico — server-rendered, London-wide. Detail URLs are a descriptive
    slug plus a short branch-coded reference:
    ``/properties-to-rent/3-bedroom-house-…-london-n1/pis230027/``."""
    name, label, host = "portico", "Portico", "portico.com"
    detail_url_re = re.compile(r"/properties-(?:to-rent|for-sale)/[a-z0-9-]+/([a-z]{2,4}\d{5,})/")

    def search_url(self, location: str, for_sale: bool) -> str:
        section = "for-sale" if for_sale else "to-rent"
        return f"https://www.portico.com/properties/{section}/london/{_slug(location)}"


class KayAndCoSearch(_HtmlAgency):
    """Kay & Co — Marylebone/Bayswater/Hyde Park specialist. Boutique: the
    rentals list isn't area-filterable in the URL, so ``search_url`` returns
    the whole (W1/W2-centric) list and the caller filters. Detail URLs are a
    descriptive slug ending in the numeric id (absolute hrefs)."""
    name, label, host = "kayandco", "Kay & Co", "kayandco.com"
    detail_url_re = re.compile(r"/property-(?:to-rent|for-sale)/[a-z0-9-]+-(\d{5,})")

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.kayandco.com/properties-{'for-sale' if for_sale else 'for-rent'}/"


class ParkheathSearch(_HtmlAgency):
    """Parkheath — North-West London (Hampstead/Belsize Park/Paddington).
    Boutique list (not area-filterable in the URL). Detail URLs are
    ``/lettings/properties-to-let/property/{id}-{street}-london``."""
    name, label, host = "parkheath", "Parkheath", "parkheath.com"
    detail_url_re = re.compile(r"/(?:lettings/properties-to-let|sales/properties-for-sale)/property/(\d{3,})-[a-z0-9-]+")

    def search_url(self, location: str, for_sale: bool) -> str:
        section = "sales/properties-for-sale" if for_sale else "lettings/properties-to-let"
        return f"https://www.parkheath.com/{section}/"


class MarshAndParsonsSearch(_HtmlAgency):
    name, label, host = "marshandparsons", "Marsh & Parsons", "marshandparsons.co.uk"
    detail_url_re = re.compile(r"/properties-(?:to-rent|for-sale)/london/property/([A-Za-z]{2,4}\d{5,})/[a-z0-9-]+/?")

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.marshandparsons.co.uk/{'properties-for-sale' if for_sale else 'properties-to-rent'}/london/{_slug(location)}"


class OpenRentSearch(_HtmlAgency):
    # Rentals only. Detail links include separate area, slug and numeric ID.
    name, label, host = "openrent", "OpenRent", "openrent.co.uk"
    detail_url_re = re.compile(r"/property-to-rent/[a-z0-9-]+/[a-z0-9-]+/(\d{5,})(?=[/\"'?#\s<>]|$)")

    def search_url(self, location: str, for_sale: bool) -> str:
        return f"https://www.openrent.co.uk/properties-to-rent/{_slug(location)}"


REGISTRY: dict[str, AgencySearch] = {
    a.name: a for a in (
        FoxtonsSearch(), DextersSearch(), ChestertonsSearch(), HamptonsSearch(),
        AshPonsonbySearch(), MarshAndParsonsSearch(), OpenRentSearch(), KfhSearch(),
        PorticoSearch(), KayAndCoSearch(), ParkheathSearch(),
    )
}


def _agency_for(url: str) -> AgencySearch | None:
    return next((a for a in REGISTRY.values() if a.matches(url)), None)


def matches(url: str) -> bool:
    return _agency_for(url) is not None


def enrich(url: str, html: str) -> dict | None:
    """Fetcher hook: parse an already-fetched agency page (search or detail)."""
    agency = _agency_for(url)
    if agency is None:
        return None
    if agency.detail_url_re.search(urlparse(url).path):
        return agency.enrich_detail(url, html)
    return agency.parse_search(url, html)


def make_search_dispatch(fetch_html: FetchHtml, fetch_json: FetchJson, fetch_html_js: FetchHtml | None = None):
    """Build the callable the ``agency_search`` tool uses — resolves the
    agency impl and runs its own ``search`` (HTML render or API call),
    passing the caller's ``filters``. ``fetch_html_js`` (browser-only, no
    curl race) backs agencies flagged ``js_search`` whose card data is
    injected client-side."""
    async def dispatch(agency: str, location: str, for_sale: bool, filters: SearchFilters, max_pages: int = 5) -> SearchReport:
        impl = REGISTRY.get(agency.strip().lower())
        if impl is None:
            raise KeyError(agency)
        return await impl.search_report(location, for_sale, filters, max_pages=max_pages,
                                        fetch_html=fetch_html, fetch_json=fetch_json, fetch_html_js=fetch_html_js)
    return dispatch


__all__ = ["AgencySearch", "REGISTRY", "matches", "enrich", "make_search_dispatch"]
