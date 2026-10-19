"""Tests for property-site HTML extractors and search tools.

Fixtures here mirror the production HTML shapes verified via
``scripts/probe_adapters.py``.
"""
import json
from pathlib import Path

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.agencies import AgencySearchUnavailable, SearchFilters, SearchHit, SearchReport
from telos.domain.tools import ToolContext
from telos.domain.tools.agency_search import AgencySearchTool
from telos.domain.tools.rightmove import RightmoveSearchTool
from telos.domain.tools.zoopla import ZooplaSearchTool, _build_search_url, _build_search_urls
from telos.tools import tool_repository
from telos.transport.browser import agencies as _agencies
from telos.transport.browser import rightmove as _rightmove
from telos.transport.browser import zoopla as _zoopla
from telos.transport.browser.agencies import REGISTRY, make_search_dispatch
from tests._helpers import dispatch

# ---------------------------------------------------------------------
# Flight-table fixture builder (shared by Rightmove listing tests)
# ---------------------------------------------------------------------
# Production listing pages encode propertyData as a flat array where every
# value position is an integer index back into the array. The helpers
# below emit the same shape from a regular nested dict so each test can
# stay readable.

def _pack_flight(value, table: list) -> int:
    if isinstance(value, dict):
        packed = {k: _pack_flight(v, table) for k, v in value.items()}
        table.append(packed)
        return len(table) - 1
    if isinstance(value, list):
        packed = [_pack_flight(v, table) for v in value]
        table.append(packed)
        return len(table) - 1
    table.append(value)
    return len(table) - 1


def _build_listing_html(property_data: dict) -> str:
    table: list = [None]  # reserve index 0 for the structure dict
    pd_idx = _pack_flight(property_data, table)
    table[0] = {"propertyData": pd_idx}
    page_model = {"data": json.dumps(table)}
    return (
        "<html><body><script>"
        f"window.__PAGE_MODEL = {json.dumps(page_model)};"
        "</script></body></html>"
    )


# ---------------------------------------------------------------------
# URL matching
# ---------------------------------------------------------------------

def test_matches_main_host():
    assert _rightmove.matches("https://www.rightmove.co.uk/properties/123") is True
    assert _rightmove.matches("https://example.com/whatever") is False


def test_matches_subdomain():
    assert _rightmove.matches("https://m.rightmove.co.uk/properties/1") is True


# ---------------------------------------------------------------------
# Listing extraction
# ---------------------------------------------------------------------

def test_listing_extracts_page_model_flight():
    """Listing pages ship as window.__PAGE_MODEL = {"data": "[flight-table]"}."""
    property_data = {
        "id": "161435000",
        "address": {"displayAddress": "1 Acacia Avenue, Brixton, London, SW2"},
        "prices": {"primaryPrice": "£600,000"},
        "bedrooms": 3,
        "bathrooms": 2,
        "text": {"description": "<p>Lovely flat near the tube.</p>"},
        "images": [{"url": "https://example.com/photo1.jpg"}],
        "customer": {"branchDisplayName": "Acme Estates"},
    }
    html = _build_listing_html(property_data)
    url = "https://www.rightmove.co.uk/properties/161435000"
    result = _rightmove.enrich(url, html)
    assert result is not None
    assert result["kind"] == "listing"
    assert result["data"]["id"] == "161435000"
    assert result["data"]["beds"] == 3
    assert result["data"]["baths"] == 2
    assert "Brixton" in result["data"]["address"]
    assert result["data"]["price"] == "£600,000"
    assert result["data"]["host_or_agent"] == "Acme Estates"
    assert "Lovely flat" in result["data"]["description"]


def test_listing_handles_null_room_counts():
    """Land listings encode bedrooms/bathrooms as null — the parser must not
    fabricate counts."""
    property_data = {
        "id": "173529038",
        "address": {"displayAddress": "Plot of land, Brixton"},
        "prices": {"primaryPrice": "£155,000"},
        "bedrooms": None,
        "bathrooms": None,
        "text": {"description": "Auction lot."},
        "images": [],
        "customer": {"branchDisplayName": "SW Property Auctions"},
    }
    html = _build_listing_html(property_data)
    url = "https://www.rightmove.co.uk/properties/173529038"
    result = _rightmove.enrich(url, html)
    assert result is not None
    assert "beds" not in result["data"]  # to_dict() drops None
    assert "baths" not in result["data"]


# ---------------------------------------------------------------------
# Search extraction
# ---------------------------------------------------------------------

def test_search_extracts_next_data():
    """Search uses Next.js __NEXT_DATA__ with the plural key
    `searchResults.properties` (the singular `searchResult` does NOT exist
    on real pages — that was the original bug captured in issue #52)."""
    next_data = {
        "props": {
            "pageProps": {
                "searchResults": {
                    "properties": [
                        {
                            "id": 1,
                            "displayAddress": "Foo Street, SW2",
                            "price": {"displayPrices": [{"displayPrice": "£500,000"}]},
                            "propertySubType": "Flat",
                            "propertyImages": {"mainImageSrc": "https://example.com/i1.jpg"},
                        },
                        {
                            "id": 2,
                            "displayAddress": "Bar Road, SW9",
                            "price": {"displayPrices": [{"displayPrice": "£650,000"}]},
                            "propertySubType": "House",
                        },
                    ]
                }
            }
        }
    }
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(next_data)
        + "</script></body></html>"
    )
    url = "https://www.rightmove.co.uk/property-for-sale/Brixton.html"
    result = _rightmove.enrich(url, html)
    assert result is not None and result["kind"] == "search_results"
    assert result["data"]["count"] == 2
    assert result["data"]["results"][0]["id"] == "1"
    assert "£500,000" in result["data"]["results"][0]["price"]


def test_returns_none_on_unrelated_path():
    """Host matches Rightmove but path isn't a listing or search page."""
    result = _rightmove.enrich(
        "https://www.rightmove.co.uk/news/article", "<html></html>",
    )
    assert result is None


def test_returns_none_on_non_rightmove_host():
    result = _rightmove.enrich("https://example.com/whatever", "<html></html>")
    assert result is None


# ---------------------------------------------------------------------
# rightmove_search tool
# ---------------------------------------------------------------------

def test_rightmove_search_in_repository():
    assert tool_repository.get("rightmove_search") is not None


@pytest.mark.asyncio
async def test_rightmove_search_routes_through_web_fetch():
    """The Rightmove search tool builds a Rightmove URL with the given
    filters and hands it to the constructor-injected ``web_fetch``."""
    captured: list[str] = []

    async def fake_web_fetch(url: str) -> dict:
        captured.append(url)
        return {"text": "ok", "images": [], "status_code": 200, "final_url": url}

    search_tool = RightmoveSearchTool(fake_web_fetch)
    ctx = ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="x",
        workspace=Path("/tmp"),
        storage_root=Path("/tmp"),
    )
    await dispatch(
        search_tool,
        {"location": "Brixton", "min_beds": 2, "max_price": 600000},
        ctx,
    )
    assert "rightmove.co.uk" in captured[-1]
    assert "Brixton" in captured[-1]
    assert "minBedrooms=2" in captured[-1]
    assert "maxPrice=600000" in captured[-1]


# ---------------------------------------------------------------------
# Zoopla extraction
# ---------------------------------------------------------------------

def test_zoopla_matches_main_host_and_subdomain():
    assert _zoopla.matches("https://www.zoopla.co.uk/to-rent/details/70562156/") is True
    assert _zoopla.matches("https://m.zoopla.co.uk/for-sale/details/123/") is True
    assert _zoopla.matches("https://example.com/to-rent/details/123/") is False


def test_zoopla_listing_extracts_json_script():
    data = {
        "props": {
            "pageProps": {
                "listing": {
                    "listingId": 70562156,
                    "listingUrl": "/to-rent/details/70562156/",
                    "displayAddress": "Bessemer Place, London SE10",
                    "price": {"displayPrice": "£2,600 pcm"},
                    "bedrooms": 2,
                    "bathrooms": 2,
                    "floorAreaSqFt": 900,
                    "description": "<p>Bright riverside flat.</p>",
                    "images": [{"url": "https://example.com/z1.jpg"}],
                    "agent": {"name": "Acme Lettings"},
                }
            }
        }
    }
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script></body></html>"
    )
    result = _zoopla.enrich("https://www.zoopla.co.uk/to-rent/details/70562156/", html)
    assert result is not None
    assert result["kind"] == "listing"
    assert result["data"]["site"] == "zoopla"
    assert result["data"]["id"] == "70562156"
    assert result["data"]["price"] == "£2,600 pcm"
    assert result["data"]["beds"] == 2
    assert result["data"]["baths"] == 2
    assert result["data"]["sqft"] == 900
    assert result["data"]["host_or_agent"] == "Acme Lettings"
    assert "Bright riverside flat" in result["data"]["description"]


def test_zoopla_search_extracts_json_script():
    data = {
        "props": {
            "pageProps": {
                "listings": [
                    {
                        "listingId": "1",
                        "listingUrl": "/to-rent/details/1/",
                        "displayAddress": "Foo Street, SW2",
                        "price": {"displayPrice": "£2,000 pcm"},
                        "bedrooms": 2,
                        "bathrooms": 1,
                        "imageUrl": "https://example.com/foo.jpg",
                    },
                    {
                        "listingId": "2",
                        "listingUrl": "/to-rent/details/2/",
                        "displayAddress": "Bar Road, SW9",
                        "price": {"displayPrice": "£2,500 pcm"},
                        "bedrooms": 3,
                    },
                ]
            }
        }
    }
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script></body></html>"
    )
    result = _zoopla.enrich("https://www.zoopla.co.uk/to-rent/property/brixton/", html)
    assert result is not None and result["kind"] == "search_results"
    assert result["data"]["count"] == 2
    assert result["data"]["results"][0]["site"] == "zoopla"
    assert result["data"]["results"][0]["id"] == "1"
    assert "£2,000 pcm" in result["data"]["results"][0]["price"]
    assert "2 beds" in result["data"]["results"][0]["summary"]


def test_zoopla_search_falls_back_to_rendered_listing_links():
    html = """
    <html><body>
      <article>
        <a href="/to-rent/details/99/">
          £2,750 pcm 2 beds 1 bath 800 sq ft Acacia Avenue, London SW2
          Bright flat near the tube.
        </a>
        <img src="https://example.com/card.jpg" />
      </article>
    </body></html>
    """
    result = _zoopla.enrich("https://www.zoopla.co.uk/to-rent/property/brixton/", html)
    assert result is not None and result["kind"] == "search_results"
    first = result["data"]["results"][0]
    assert first["id"] == "99"
    assert first["url"] == "https://www.zoopla.co.uk/to-rent/details/99/"
    assert first["price"] == "£2,750 pcm"
    assert "2 beds" in first["summary"]
    assert "1 baths" in first["summary"]


def test_zoopla_returns_none_on_unrelated_path():
    assert _zoopla.enrich("https://www.zoopla.co.uk/house-prices/london/", "<html></html>") is None
    assert _zoopla.enrich("https://example.com/to-rent/details/99/", "<html></html>") is None


# ---------------------------------------------------------------------
# zoopla_search tool
# ---------------------------------------------------------------------

def test_zoopla_search_in_repository():
    assert tool_repository.get("zoopla_search") is not None


@pytest.mark.parametrize(
    ("location", "expected_path"),
    [
        ("Brixton", "/to-rent/property/brixton/"),
        ("Brixton, London", "/to-rent/property/london/brixton/"),
        ("London Brixton", "/to-rent/property/london/brixton/"),
        ("SW9", "/to-rent/property/sw9/"),
        ("SW9 8AB", "/to-rent/property/sw9-8ab/"),
        ("Manchester", "/to-rent/property/manchester/"),
    ],
)
def test_zoopla_search_url_uses_location_path(location: str, expected_path: str):
    url = _build_search_url(location=location, for_sale=False)

    assert expected_path in url


def test_zoopla_search_url_candidates_retry_london_for_single_token_location():
    urls = _build_search_urls(location="Brixton", min_beds=2, max_price=3000, for_sale=False)

    assert len(urls) == 2
    assert "/to-rent/property/brixton/" in urls[0]
    assert "/to-rent/property/london/brixton/" in urls[1]
    assert "beds_min=2" in urls[0]
    assert "beds_min=2" in urls[1]


@pytest.mark.parametrize("location", ["Brixton, London", "SW9", "SW9 8AB"])
def test_zoopla_search_url_candidates_skip_london_retry_when_location_is_specific(location: str):
    urls = _build_search_urls(location=location, for_sale=False)

    assert len(urls) == 1


@pytest.mark.asyncio
async def test_zoopla_search_routes_through_web_fetch():
    captured: list[str] = []

    async def fake_web_fetch(url: str) -> dict:
        captured.append(url)
        return {"text": "ok", "images": [], "status_code": 200, "final_url": url}

    search_tool = ZooplaSearchTool(fake_web_fetch)
    ctx = ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="x",
        workspace=Path("/tmp"),
        storage_root=Path("/tmp"),
    )
    await dispatch(
        search_tool,
        {"location": "Brixton", "min_beds": 2, "max_price": 3000, "for_sale": False},
        ctx,
    )
    assert len(captured) == 2
    assert "zoopla.co.uk" in captured[0]
    assert "/to-rent/property/brixton/" in captured[0]
    assert "/to-rent/property/london/brixton/" in captured[1]
    assert "beds_min=2" in captured[0]
    assert "price_max=3000" in captured[0]
    assert "price_frequency=per_month" in captured[0]


@pytest.mark.asyncio
async def test_zoopla_search_does_not_retry_after_structured_results():
    captured: list[str] = []

    async def fake_web_fetch(url: str) -> dict:
        captured.append(url)
        return {
            "text": "ok\n\n---\n[search_results] Zoopla search - 1 result",
            "structured": {"kind": "search_results", "data": {"count": 1}},
            "images": [],
            "status_code": 200,
            "final_url": url,
        }

    search_tool = ZooplaSearchTool(fake_web_fetch)
    ctx = ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="x",
        workspace=Path("/tmp"),
        storage_root=Path("/tmp"),
    )
    await dispatch(search_tool, {"location": "Brixton", "for_sale": False}, ctx)

    assert len(captured) == 1
    assert "/to-rent/property/brixton/" in captured[0]




# ──────────────────────────────────────────────────────────────────────
# Agency search — protocol impls: JSON-LD detail (Foxtons), OG detail
# (Chestertons/Dexters), search() self-fetch, detail-page enrich hook, tool
# ──────────────────────────────────────────────────────────────────────

_FOXTONS_LD = {  # real schema.org shape from a live Foxtons detail page
    "@type": ["House", "Product"],
    "name": "House to Rent in W1H 5TG",
    "image": "https://web-prod-page-assets.foxtons.co.uk/x/chpk0034538-12.jpg",
    "numberOfBedrooms": 5, "numberOfBathroomsTotal": 5,
    "floorSize": {"@type": "QuantitativeValue", "value": 2969, "unitCode": "MTK"},
    "offers": {"@type": "Offer", "seller": {"name": "Marylebone and Mayfair"},
               "priceSpecification": {"price": 12675, "priceCurrency": "GBP"}},
    "address": {"streetAddress": "Seymour Place", "addressLocality": "Marylebone", "postalCode": "W1H"},
}


def _ld_html(ld: dict) -> str:
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'


def test_agency_matches_and_registry():
    assert set(REGISTRY) == {"foxtons", "dexters", "chestertons", "hamptons", "marshandparsons",
                             "openrent", "ashponsonby", "kfh", "portico", "kayandco", "parkheath"}
    assert _agencies.matches("https://www.foxtons.co.uk/properties-to-rent/marylebone")
    assert not _agencies.matches("https://example.com/x")


def test_foxtons_detail_via_jsonld():
    url = "https://www.foxtons.co.uk/properties-to-rent/W1H/chpk0034538"
    d = _agencies.enrich(url, _ld_html(_FOXTONS_LD))["data"]
    assert d["id"] == "chpk0034538" and d["beds"] == 5 and d["baths"] == 5
    assert d["price"] == "£12,675" and d["sqft"] == 2969 and d["site"] == "foxtons"
    assert "Seymour Place" in d["address"] and "W1H" in d["address"]


def test_chestertons_detail_via_og_fallback():
    og = (
        '<html><head>'
        '<meta property="og:title" content="3 bedroom property in Kidderpore Gardens, London, NW3 - £7,500 pcm | Chestertons">'
        '<meta property="og:image" content="https://mr0.homeflow-assets.co.uk/x/HEA265781_08.jpg">'
        "</head><body></body></html>"
    )
    d = _agencies.enrich("https://www.chestertons.co.uk/properties/22016750/lettings/HEA265781", og)["data"]
    assert d["id"] == "22016750" and d["site"] == "chestertons" and d["beds"] == 3
    assert d["price"] == "£7,500 pcm" and "Kidderpore Gardens" in d["address"] and "NW3" in d["address"]
    assert d["photos"] == ["https://mr0.homeflow-assets.co.uk/x/HEA265781_08.jpg"]


def test_dexters_detail_via_og_fallback():
    og = (
        '<html><head><meta name="description" '
        'content="View our 3 bedroom 2 bathroom 3 reception House to rent in Westmoreland Street, London, W1G. (Ref 278442)">'
        "</head><body></body></html>"
    )
    d = _agencies.enrich("https://www.dexters.co.uk/property-for-rent/house-to-rent-in-westmoreland-street-london-w1g/278442", og)["data"]
    assert d["id"] == "278442" and d["site"] == "dexters" and d["beds"] == 3 and d["baths"] == 2
    assert "Westmoreland Street" in d["address"]


async def test_search_self_fetches_and_extracts_hits():
    # An HTML agency's search() renders via the injected fetch_html and returns hits.
    search_html = (
        "<html><body>"
        '<a href="/properties/22016750/lettings/HEA265781">a</a>'
        '<a href="/properties/22016751/lettings/HEA265782">b</a>'
        '<a href="/properties/22016750/lettings/HEA265781">dup</a>'
        '<a href="/london/hampstead/lettings">area link</a>'
        "</body></html>"
    )
    captured = {}

    async def fake_html(url):
        captured["url"] = url
        return search_html

    async def fake_json(url):
        return None

    hits = await REGISTRY["chestertons"].search("Hampstead", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
    assert captured["url"] == "https://www.chestertons.co.uk/london/hampstead/lettings"
    assert [h.id for h in hits] == ["22016750", "22016751"]  # deduped, area link excluded
    assert hits[0].url == "https://www.chestertons.co.uk/properties/22016750/lettings/HEA265781"


async def test_walled_agencies_extract_hits_from_rendered_html():
    # URL shapes captured from live listing cards, not guessed numeric routes.
    cases = {
        "marshandparsons": (
            "<html><body>"
            '<a href="/properties-to-rent/london/property/CSG220682/kensington-gardens-square/">a</a>'
            '<a href="/properties-to-rent/london/property/BAY220683/queensway/">b</a>'
            '<a href="/properties-to-rent/london/property/CSG220682/kensington-gardens-square/">dup</a>'
            "</body></html>",
            ["CSG220682", "BAY220683"],
        ),
        "openrent": (
            "<html><body>"
            '<a href="/property-to-rent/london/studio-flat-w2/123456">a</a>'
            '<a href="/property-to-rent/london/2-bed-flat-w2/123457">b</a>'
            "</body></html>",
            ["123456", "123457"],
        ),
    }
    for name, (html, ids) in cases.items():
        async def fake_html(url, _html=html):
            return _html

        async def fake_json(url):
            return None

        hits = await REGISTRY[name].search("w2", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
        assert [h.id for h in hits] == ids
        assert all(h.url.startswith(f"https://www.{REGISTRY[name].host}") for h in hits)


def test_agency_search_url_builders():
    assert REGISTRY["foxtons"].search_url("Marylebone", False) == "https://www.foxtons.co.uk/properties-to-rent/marylebone"
    assert REGISTRY["dexters"].search_url("W1G", False) == "https://www.dexters.co.uk/property-lettings/properties-to-rent-in-w1g"
    assert REGISTRY["hamptons"].search_url("Marylebone", False) == "https://www.hamptons.co.uk/search/?channel=lettings&location=marylebone"
    assert REGISTRY["ashponsonby"].search_url("W2", False) == "https://www.ashponsonby.com/to-rent"
    assert REGISTRY["openrent"].search_url("W2", False) == "https://www.openrent.co.uk/properties-to-rent/w2"
    assert REGISTRY["marshandparsons"].search_url("W2", False) == "https://www.marshandparsons.co.uk/properties-to-rent/london/w2"
    assert REGISTRY["kfh"].search_url("Marylebone", False) == "https://www.kfh.co.uk/property/to-rent/in-marylebone/"


async def test_kfh_extracts_hits_from_slug_urls():
    # KFH detail URLs are a descriptive slug ending in the numeric id.
    html = (
        "<html><body>"
        '<a href="/property-to-rent/1-bedroom-apartment-to-rent-in-hans-place-knightsbridge-london-sw1x-9021/">a</a>'
        '<a href="/property-to-rent/4-bedroom-terraced-property-to-rent-in-marlborough-place-st-johns-wood-london-nw8-20102/">b</a>'
        '<a href="/property-to-rent/london/marylebone">area link (not a listing)</a>'
        "</body></html>"
    )

    async def fake_html(url):
        return html

    async def fake_json(url):
        return None

    hits = await REGISTRY["kfh"].search("Marylebone", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
    assert [h.id for h in hits] == ["9021", "20102"]
    assert hits[0].url == "https://www.kfh.co.uk/property-to-rent/1-bedroom-apartment-to-rent-in-hans-place-knightsbridge-london-sw1x-9021/"


async def test_new_html_agencies_extract_ids():
    # Portico (ref = branch code + digits), Kay & Co (absolute hrefs, trailing
    # id), Parkheath (/property/{id}-street) — real detail-URL shapes.
    cases = {
        "portico": (
            '<a href="/properties-to-rent/3-bedroom-house-to-rent-in-moon-street-london-n1/pis230027/">a</a>'
            '<a href="/properties-to-rent/4-bedroom-house-to-rent-in-chenies-mews-london-wc1e/pbl250137/">b</a>'
            '<a href="/properties/to-rent/london/marylebone">area (not a listing)</a>',
            ["pis230027", "pbl250137"],
        ),
        "kayandco": (
            '<a href="https://www.kayandco.com/property-to-rent/1-bedroom-apartment-to-rent-in-hyde-park-square-w2-813297">a</a>'
            '<a href="https://www.kayandco.com/property-to-rent/4-bedroom-terraced-house-to-rent-in-oval-road-nw1-807706">b</a>'
            '<a href="/properties-for-rent/">list (not a listing)</a>',
            ["813297", "807706"],
        ),
        "parkheath": (
            '<a href="/lettings/properties-to-let/property/7193-glenmore-road-london-london">a</a>'
            '<a href="/lettings/properties-to-let/property/7174-newcastle-place-paddington-london">b</a>',
            ["7193", "7174"],
        ),
    }
    for name, (body, ids) in cases.items():
        async def fake_html(url, _b=body):
            return f"<html><body>{_b}</body></html>"

        async def fake_json(url):
            return None

        hits = await REGISTRY[name].search("Marylebone", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
        assert [h.id for h in hits] == ids, name


async def test_html_agency_filters_by_slug_beds_and_card_price():
    # KFH bedrooms come from the slug; prices belong to individual cards.
    html = (
        '<div class="sales-wrap"><a href="/property-to-rent/1-bedroom-flat-to-rent-in-alpha-w2-111111/">a</a> £2,000 pcm</div>'
        '<div class="sales-wrap"><a href="/property-to-rent/2-bedroom-flat-to-rent-in-beta-w2-222222/">b</a> £575 pw</div>'
        '<div class="sales-wrap"><a href="/property-to-rent/3-bedroom-house-to-rent-in-gamma-w2-333333/">c</a> £6,000 pcm</div>'
    )

    async def fake_html(url):
        return f"<html><body>{html}</body></html>"

    async def fake_json(url):
        return None

    hits = await REGISTRY["kfh"].search(
        "W2", False, SearchFilters(min_beds=2, max_price=4000),
        fetch_html=fake_html, fetch_json=fake_json,
    )
    # 1-bed dropped (beds<2); 3-bed £6,000 dropped (price>4,000);
    # 2-bed £575pw → £2,492 pcm kept, with beds+price populated.
    assert [h.id for h in hits] == ["222222"]
    assert hits[0].beds == 2 and hits[0].price == "£2,492 pcm"


async def test_card_price_formats_and_dup_anchor_windows():
    # Real formats found live: Chestertons "£3,141 (pcm)" (parenthesised
    # unit); Dexters "£3,462 Pw / £15,000 Pcm" (capitalised, weekly first);
    # Portico-style duplicate anchors (image+title+button all link the same
    # listing) with the price after the LAST duplicate — the card window must
    # span to the next DISTINCT listing, not the next raw match.
    chesterton_html = (
        '<article><a href="/properties/22016482/lettings/HEA742388">img</a>'
        '<a href="/properties/22016482/lettings/HEA742388">title</a>'
        " Cumberland Court, Great Cumberland Place 1 bed 568 ft 2 £3,141 (pcm) Fees apply</article>"
        '<article><a href="/properties/22014101/lettings/CAL260067">img</a>'
        " Chester Terrace 5 bed £32,500 (pcm) Fees apply</article>"
    )

    async def fake_html(url, _h=chesterton_html):
        return _h

    async def fake_json(url):
        return None

    hits = await REGISTRY["chestertons"].search(
        "Marylebone", False, SearchFilters(max_price=4000), fetch_html=fake_html, fetch_json=fake_json)
    assert [h.id for h in hits] == ["22016482"]          # £32,500 dropped
    assert hits[0].price == "£3,141 pcm"                 # (pcm) parsed despite parens + dup anchors

    dexters_html = (
        '<article><a href="/property-for-rent/house-to-rent-in-westmoreland-street-london-w1g/278442">x</a>'
        " Westmoreland Street £3,462 Pw / £15,000 Pcm 3 Bedrooms</article>"
    )
    hits = await REGISTRY["dexters"].search(
        "Marylebone", False, SearchFilters(), fetch_html=lambda u: _ret(dexters_html), fetch_json=fake_json)
    assert hits[0].price == "£15,000 pcm"                # Prefer the stated monthly amount, not rounded weekly conversion.


async def _ret(value):
    return value


async def test_homeflow_carousel_does_not_steal_neighbour_price():
    # Homeflow pages list every property as image-only anchors (a featured
    # carousel) BEFORE the priced cards. Live bug: first-anchor windows sat in
    # the carousel and credited the £32,500 listing with a neighbour's £3,100.
    # Segment attribution must price each listing from ITS OWN card.
    html = (
        # carousel: A then B, no prices
        '<a href="/properties/1111/lettings/AAA">imgA</a>'
        '<a href="/properties/2222/lettings/BBB">imgB</a>'
        # real cards: A cheap, B super-prime
        '<article><a href="/properties/1111/lettings/AAA">cardA</a> Cumberland Court 568 ft 2 £3,141 (pcm)</article>'
        '<article><a href="/properties/2222/lettings/BBB">cardB</a> Chester Terrace 4049 ft 2 £32,500 (pcm)</article>'
    )

    async def fake_html(url):
        return f"<html><body>{html}</body></html>"

    async def fake_json(url):
        return None

    unfiltered = await REGISTRY["chestertons"].search(
        "Marylebone", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
    assert {h.id: h.price for h in unfiltered} == {"1111": "£3,141 pcm", "2222": "£32,500 pcm"}

    hits = await REGISTRY["chestertons"].search(
        "Marylebone", False, SearchFilters(max_price=4000), fetch_html=fake_html, fetch_json=fake_json)
    assert [h.id for h in hits] == ["1111"]  # £32,500 correctly priced → correctly dropped


async def test_hydration_blob_offbyone_price_is_ignored():
    # Live Chestertons shape: a Next.js flight blob repeats every listing with
    # the price BEFORE its url (so a segment there holds the NEXT listing's
    # price), then the real cards follow. Parsing must use the card DOM.
    html = (
        '<script id="__NEXT_F">self.__next_f.push(["'
        '\\"urlLabel\\":\\"/properties/1111/lettings\\" ... \\"price\\":\\"£32,500 pcm\\" '
        '\\"urlLabel\\":\\"/properties/2222/lettings\\" ... \\"price\\":\\"£3,100 pcm\\"'
        '"])</script>'
        '<article><a href="/properties/1111/lettings/AAA">card</a> Cumberland Court £3,141 (pcm)</article>'
        '<article><a href="/properties/2222/lettings/BBB">card</a> Chester Terrace £32,500 (pcm)</article>'
    )

    async def fake_html(url):
        return html

    async def fake_json(url):
        return None

    hits = await REGISTRY["chestertons"].search(
        "Marylebone", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
    assert {h.id: h.price for h in hits} == {"1111": "£3,141 pcm", "2222": "£32,500 pcm"}


async def test_script_only_unknown_state_is_not_inventory():
    # KFH now renders the actual area results. Arbitrary script strings are
    # not evidence of listings on the current search page.
    html = (
        "<script>window.__DATA__ = {\"u\":\"/property-to-rent/2-bedroom-flat-to-rent-in-x-w2-98765/\"}</script>"
        "<body>no anchors here</body>"
    )

    async def fake_html(url):
        return html

    async def fake_json(url):
        return None

    with pytest.raises(AgencySearchUnavailable):
        await REGISTRY["kfh"].search("W2", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)


async def test_unavailable_listings_are_dropped():
    # Agencies keep let/sold stock in their search results — real badges seen
    # live: Chestertons "Let Agreed", Dexters "Let (Tenant Info)".
    html = (
        '<article><a href="/properties/1111/lettings/AAA">card</a> Available Flat £3,000 (pcm)</article>'
        '<article><a href="/properties/2222/lettings/BBB">card</a> Let Agreed Chalfont Court £3,100 (pcm)</article>'
        '<article><a href="/properties/3333/lettings/CCC">card</a> Under Offer Baker St £2,500 (pcm)</article>'
    )

    async def fake_html(url):
        return html

    async def fake_json(url):
        return None

    hits = await REGISTRY["chestertons"].search(
        "Marylebone", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
    assert [h.id for h in hits] == ["1111"]


async def test_foxtons_api_drops_let_and_under_offer():
    async def fake_html(url):
        raise AssertionError("lettings uses the API")

    async def fake_json(url):
        return {"data": [
            {"propertyReference": "OK1", "street": "Available St", "location": "Marylebone",
             "postcode": "W1H 1AA", "bedrooms": 2, "priceFrom": 700,
             "hasRecentlyBeenLet": False, "isUnderOffer": False},
            {"propertyReference": "LET1", "street": "Gone St", "location": "Marylebone",
             "postcode": "W1H 2BB", "bedrooms": 2, "priceFrom": 700, "hasRecentlyBeenLet": True},
            {"propertyReference": "UO1", "street": "Offer St", "location": "Marylebone",
             "postcode": "W1H 3CC", "bedrooms": 2, "priceFrom": 700, "isUnderOffer": True},
        ]}

    hits = await REGISTRY["foxtons"].search(
        "Marylebone", False, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json)
    assert [h.id for h in hits] == ["ok1"]


async def test_js_search_agency_prefers_rendered_fetcher():
    # Dexters card prices are injected client-side: when a browser-only
    # fetcher is provided, its search must use it, not the curl race.
    used = {}

    async def race_html(url):
        used["race"] = True
        return "<html></html>"

    async def js_html(url):
        used["js"] = True
        return '<article><a href="/property-for-rent/x-street/123456">x</a> £2,000 Pcm</article>'

    async def fake_json(url):
        return None

    hits = await REGISTRY["dexters"].search(
        "W1", False, SearchFilters(), fetch_html=race_html, fetch_json=fake_json, fetch_html_js=js_html)
    assert used == {"js": True} and hits[0].price == "£2,000 pcm"
    # Without a js fetcher it falls back to the race fetcher.
    with pytest.raises(AgencySearchUnavailable):
        await REGISTRY["dexters"].search("W1", False, SearchFilters(), fetch_html=race_html, fetch_json=fake_json)
    assert used.get("race") is True


async def test_make_search_dispatch_routes_to_impl():
    # Foxtons lettings route through its JSON API (fetch_json), returning
    # structured hits with price (pcm) + beds — no HTML fetch.
    async def fake_html(url):
        raise AssertionError("Foxtons lettings must use the API, not HTML")

    async def fake_json(url):
        assert "property-search/pins" in url and "FreeText=marylebone" in url
        return {"total": 1, "data": [{
            "propertyReference": "CHPK0034538", "street": "Seymour Place",
            "location": "Marylebone", "postcode": "W1H 5TG",
            "bedrooms": 2, "bathrooms": 2, "priceFrom": 750, "priceTo": 750,
        }]}

    dispatch = make_search_dispatch(fetch_html=fake_html, fetch_json=fake_json)
    report = await dispatch("foxtons", "marylebone", False, SearchFilters())
    hits = [hit.to_dict() for hit in report.hits]
    assert len(hits) == 1
    h = hits[0]
    assert h["id"] == "chpk0034538" and h["site"] == "foxtons" and h["beds"] == 2
    assert h["price"] == "£3,250 pcm"  # 750/wk × 52/12
    assert h["url"] == "https://www.foxtons.co.uk/properties-to-rent/W1H/chpk0034538"


async def test_foxtons_api_search_filters_and_structures():
    async def fake_html(url):
        raise AssertionError("lettings must not hit HTML")

    async def fake_json(url):
        return {"data": [
            {"propertyReference": "A1", "street": "Cheap St", "location": "Marylebone",
             "postcode": "W1H 1AA", "bedrooms": 2, "priceFrom": 700},   # £3,033 pcm, 2 bed ✓
            {"propertyReference": "B2", "street": "Posh St", "location": "Marylebone",
             "postcode": "W1H 2BB", "bedrooms": 2, "priceFrom": 1200},  # £5,200 pcm — too pricey
            {"propertyReference": "C3", "street": "Studio St", "location": "Marylebone",
             "postcode": "W1H 3CC", "bedrooms": 1, "priceFrom": 500},   # 1 bed — too few
        ]}

    hits = await REGISTRY["foxtons"].search(
        "Marylebone", False, SearchFilters(max_price=3500, min_beds=2),
        fetch_html=fake_html, fetch_json=fake_json,
    )
    assert [h.id for h in hits] == ["a1"]
    assert hits[0].price == "£3,033 pcm" and hits[0].beds == 2
    assert hits[0].url == "https://www.foxtons.co.uk/properties-to-rent/W1H/a1"


async def test_foxtons_sale_falls_back_to_html_path():
    async def fake_html(url):
        assert "properties-for-sale" in url
        return '<a href="/properties-for-sale/W1H/chpk0099999">x</a>'

    async def fake_json(url):
        raise AssertionError("sale must not use the lettings API")

    hits = await REGISTRY["foxtons"].search(
        "marylebone", True, SearchFilters(), fetch_html=fake_html, fetch_json=fake_json,
    )
    assert hits and hits[0].id == "chpk0099999"


async def test_agency_search_tool_routes_and_rejects_unknown():
    ctx = ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="a", workspace=Path("/tmp"), storage_root=Path("/tmp"),
    )

    async def fake_dispatch(agency, location, for_sale, filters, max_pages):
        return SearchReport(hits=[SearchHit(site=agency, id="1", url="u", title="t")])

    ok = await dispatch(AgencySearchTool(fake_dispatch), {"agency": "foxtons", "location": "Marylebone"}, ctx)
    assert ok.is_error is False and '"count": 1' in ok.text

    bad = await dispatch(AgencySearchTool(fake_dispatch), {"agency": "nope", "location": "X"}, ctx)
    assert bad.is_error is True and "Unknown agency" in bad.text


async def test_agency_search_tool_threads_filters():
    ctx = ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="a", workspace=Path("/tmp"), storage_root=Path("/tmp"),
    )
    captured = {}

    async def fake_dispatch(agency, location, for_sale, filters, max_pages):
        captured["filters"] = filters
        return SearchReport(hits=[SearchHit(site=agency, id="1", url="u", title="t")])

    await dispatch(
        AgencySearchTool(fake_dispatch),
        {"agency": "foxtons", "location": "Marylebone", "max_price": 3500, "min_beds": 2},
        ctx,
    )
    assert captured["filters"].max_price == 3500
    assert captured["filters"].min_beds == 2
    assert captured["filters"].max_beds is None
