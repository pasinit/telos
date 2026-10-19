# Estate-agent search — architecture, per-agency status, and the XHR path

## Architecture

Direct London estate-agent search (beyond Rightmove/Zoopla) so agents like
`homey` reach agent-direct / off-portal inventory.

- **Protocol + impls:** `AgencySearch` ABC in
  `telos/transport/browser/agencies.py`, one subclass per agency; pure types
  (`Listing`/`SearchHit`, `AGENCY_LABELS`) live in `telos/domain/agencies.py`
  so the tool (domain layer) uses them without importing transport. Adding an
  agency = one subclass.
- **Tool:** `telos/domain/tools/agency_search.py` — a single
  `agency_search(agency, location, for_sale)` covering every registered
  agency.
- **Enricher:** `telos/transport/browser/agencies.py` — the same impls back
  `enrich(url, html)`, registered in `_enrich_known_site`. Two shapes:
  - **search** → the current listing detail URLs + area (agency search
    pages don't server-render list-level price/beds).
  - **detail** → a robustness ladder: (1) schema.org property JSON-LD
    (Foxtons); (2) Open Graph / `<title>` fallback — every site tested
    exposes `og:title`/`description` like "3 bedroom … in Kidderpore
    Gardens, London, NW3 - £7,500 pcm", so beds/baths/price/address regex
    out even with no JSON-LD.

Best-effort by construction: an unparseable shape yields `None` (raw page
text still reaches the caller), never an exception.

## Per-agency status (verified end-to-end live 2026-08-16)

| Agency | Working? | Detail source | Notes |
|---|---|---|---|
| Foxtons | ✅ 19 listings + £-detail | JSON-LD (rich) | own platform + internal API (below) |
| Dexters | ✅ 18 listings + detail | OG (beds/baths/address; no price in OG) | own platform |
| Chestertons | ✅ 12 listings + £-detail | OG (beds/price/address/image) | **Homeflow** |
| Hamptons | ✅ 9 listings + £-detail | OG (Homeflow) | search: `/search?channel=lettings&location=` |
| Ash Ponsonby | ✅ 10 listings + detail | OG | Wix; `/to-rent/{area}/{street}/{desc}/{id}` |
| Marsh & Parsons | ⚠️ via unblocker | OG (once rendered) | **Cloudflare-walled** ("Just a moment…"); syndicates to the portals (low unique value) |
| OpenRent | ⚠️ via unblocker | OG (once rendered) | **AWS-WAF-walled** (results page `awswaf.com/…/mp_verify`); landlord-direct (highest unique value) |

**5 of 7 work with the free fetch race.** Marsh & Parsons and OpenRent are
behind active bot-defences that patchright cannot pass (verified live
2026-08-16: M&P stays on the Cloudflare interstitial after a 25s
challenge-wait; OpenRent's results page `/properties-to-rent/{area}` serves
an AWS WAF challenge shell). They are wired as `_HtmlAgency` subclasses that
only render once the fetch race falls through to the unblocker — see below.

## Unblocker (render-and-solve proxy) — the walled-SPA path

`telos/transport/browser/fetcher.py` `fetch_via_unblocker(url)` is the
last resort in `_race_curl_vs_playwright`, reached only after both curl and
patchright hit a wall (`_looks_like_bot_page` for visible-text shells;
`_looks_like_challenge` for AWS-WAF/Cloudflare/DataDome fingerprints that
live in scripts and survive into the raw HTML). It is a no-op — and free —
until configured, so nothing changes for the 5 working agencies or the
Rightmove/Zoopla tools.

Configure with a provider-agnostic GET template holding a literal `{url}`
placeholder (target is URL-encoded and substituted in) plus the provider's
own API key. Works with any GET-style unblocker (ScrapingBee, ScraperAPI,
ZenRows, ScrapingAnt, …):

```
TELOS_UNBLOCKER_URL='https://app.scrapingbee.com/api/v1/?api_key=KEY&render_js=true&premium_proxy=true&url={url}'
TELOS_UNBLOCKER_TIMEOUT=75   # optional, seconds
```

**Remaining live-tuning (needs a configured key):** the detail-URL regexes
for M&P (`/propert…/{id}`) and OpenRent (`/property-to-rent/…{id}`) are
best-effort — we've never seen either results page past its wall. Once
`TELOS_UNBLOCKER_URL` is set, capture one rendered results page per agency
through the proxy and confirm/adjust `detail_url_re` (and add JSON-LD/OG
detail parsing if the rendered detail pages differ from the OG shape the
other agencies use).

## The XHR / internal-API path (richer results — "Level 2")

Search pages hydrate cards from the site's **own internal JSON API**.
Calling it directly returns filterable price/beds results in one shot.
Capture an agency's API with `scripts/probe_adapter_xhr.py <search_url>`
(runs patchright on the deployment, logs listing-shaped XHR/fetch JSON).

### Foxtons ✅ IMPLEMENTED (lettings — filtered + structured)
`FoxtonsSearch.search` calls the JSON API for lettings, so hits carry
price + beds directly (no per-listing fetch) and `max_price`/`min_beds`/
`max_beds` are applied client-side. Sales fall back to the HTML URL path.
- `GET https://www.foxtons.co.uk/api/v2/property-search/pins?FreeText={area}&PageSize=1000&OrderBy=Price&OrderDirection=Asc&Page=1&IncludeSummary=true&InstructionType=LON&InstructionType=BDC`
  (LON+BDC = lettings; the site pulls the whole area and filters client-side, so we do too).
- Response `{ total, page, totalPages, data:[...] }`; `data[]` items:
  `propertyReference`, `street`, `location`, `postcode`, `bedrooms`,
  `bathrooms`, `priceFrom`/`priceTo`, `point` (geo), `instructionType`, `assets`.
- **Price unit: lettings `priceFrom` is £/week** (verified: Seymour Place
  5-bed = 2925 here vs £12,675 pcm in JSON-LD → ×52/12). We convert to £ pcm.
- Detail URL rebuilt as `/properties-to-rent/{postcode-district}/{ref.lower()}`.

### Homeflow platform (Chestertons, Hamptons) — no listings API
Probed 2026-08-16: the only `concierge.homeflow.co.uk` call is
`/api/agency/{uuid}` (agency **config** — branches, selectors — not
listings; Chestertons uuid `b0def7ae-0bf1-40ac-936f-0d64610ce070`).
Listings are **server-rendered** (Next.js) — no JSON search endpoint. The
SSR cards *do* carry price/beds/address, but the markup is brittle (a
featured-carousel image-card duplicates each listing ahead of the real
data-card, 3 `<a>` per property, price not co-located with beds), so
card-parsing is **deferred** rather than shipped. Chestertons/Hamptons stay
on the HTML URL-extraction path (filters ignored; drill into each URL).

### To capture (targets)
Dexters (own API?), Homeflow card-parse (if the brittleness is deemed worth
it). Marsh & Parsons + OpenRent are bot-walled (see Unblocker section), not
just SPAs. Level-2 for an agency = a `fetch_json` + `data[]`→`SearchHit`
mapper, or (Homeflow) an SSR card parser.

### KFH (Kinleigh Folkard & Hayward) ✅ HTML
Server-rendered; `search_url` = `/property-to-rent/london/{area}`, detail URLs
are a descriptive slug ending in the numeric id
(`/property-to-rent/…-sw1x-9021/`). URL-only (agent fetches each for details).

### SPA holdouts — not feasible via the fetcher (2026-08-17)
Client-only SPAs that don't render usable results under `fetch_raw` (even with
a long wait + scroll), so no href/blob to scrape and each needs bespoke API
reverse-engineering:
- **Savills** — search API is `POST livev6-searchapi.savills.com/Data/SearchByUrl`
  with body `{"url":"?SearchList=Id_{regionId}&Tenure=…&Category=GRS_CAT_RES&…"}`.
  Response `Results.Properties[]` is rich (Bedrooms, DisplayPriceText, address,
  `MetaInformation.CanonicalUrl` → `property-detail/{ref}`). BUT location needs a
  pre-resolved region id (autocomplete step; postcode `Shapes=W2` → 500, text
  search → 0 results) and the lettings `Tenure` code still needs capturing
  (`GRS_T_B` returned for-sale). 2-step + opaque → deferred.
- **Knight Frank** — no discoverable search endpoint (homepage exposes no
  results URL; fully JS-built).
- **Winkworth, Douglas & Gordon, Strutt & Parker, Carter Jonas** — render to
  empty/near-empty shells; no listings in DOM.
- **John D Wood** (Foxtons-owned) — flaky under `fetch_raw` (ERR_ABORTED / only
  the self search-URL as a link); no shared Foxtons pins API.

## Search filters (tool-level)

`agency_search` accepts optional `max_price` (£ pcm for lettings / £ for
sales), `min_beds`, `max_beds` (see `SearchFilters` in
`telos/domain/agencies.py`). Only agencies with a structured source apply
them at search time — **Foxtons** today. HTML-scrape agencies ignore them
(the agent filters after fetching each listing). `SearchFilters.accepts`
never excludes on missing data (unknown price/beds → surfaced, not dropped).
