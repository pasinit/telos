import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from telos.domain.agencies import AgencySearchUnavailable, SearchFilters, SearchReport
from telos.domain.tools.agency_search import AgencySearchInput, AgencySearchTool
from telos.transport.browser.agencies import REGISTRY, _price_pcm, make_search_dispatch


def card(id='1111', price='£3,000 pcm', extra='2 Bedrooms', slug='flat-london-w2'):
    return f'<article><a href="/property-for-rent/{slug}/{id}">Example W2</a><p>{price}</p><p>{extra}</p></article>'


async def test_pagination_finds_matches_after_expensive_first_page():
    agency = REGISTRY['dexters']
    start = agency.search_url('Bayswater', False)
    pages = {start: card(price='£8,000 pcm') + '<a href="'+start+'/page-2">2</a>',
             start+'/page-2': card('2222') + '<a href="'+start+'/page-3">Next</a>',
             start+'/page-3': card('2222') + card('3333') + '<a href="'+start+'/page-2">Previous</a>'}
    fetch = AsyncMock(side_effect=lambda url: pages[url])
    report = await agency.search_report('Bayswater', False, SearchFilters(max_price=3500, min_beds=2, max_beds=2),
        max_pages=5, fetch_html=fetch, fetch_html_js=fetch, fetch_json=AsyncMock())
    assert [hit.id for hit in report.hits] == ['2222', '3333']
    assert len(report.pages_visited) == 3
    assert not report.pending_urls
    assert report.stop_reason == 'no_observed_next_link'


async def test_page_cap_returns_continuation_instead_of_exhaustive_claim():
    agency = REGISTRY['dexters']
    start = agency.search_url('Bayswater', False)
    fetch = AsyncMock(return_value=card() + '<a href="'+start+'/page-2">Next</a>')
    report = await agency.search_report('Bayswater', False, SearchFilters(), max_pages=1,
        fetch_html=fetch, fetch_json=AsyncMock())
    assert report.stop_reason == 'page_limit'
    assert report.pending_urls == [start+'/page-2']


async def test_failed_later_page_retains_hits_and_reports_gap():
    agency = REGISTRY['dexters']
    start = agency.search_url('Bayswater', False)
    fetch = AsyncMock(side_effect=[card() + '<a href="'+start+'/page-2">Next</a>', TimeoutError()])
    report = await agency.search_report('Bayswater', False, SearchFilters(), max_pages=5,
        fetch_html=fetch, fetch_json=AsyncMock())
    assert report.hits and report.warnings and report.pending_urls
    assert report.stop_reason == 'page_failed'


async def test_first_page_failure_is_unavailable_not_empty():
    dispatch = make_search_dispatch(AsyncMock(side_effect=RuntimeError('HTTP 403')), AsyncMock())
    with pytest.raises(AgencySearchUnavailable):
        await dispatch('dexters', 'Bayswater', False, SearchFilters())


async def test_cancellation_is_not_swallowed():
    dispatch = make_search_dispatch(AsyncMock(side_effect=asyncio.CancelledError()), AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await dispatch('dexters', 'Bayswater', False, SearchFilters())


async def test_external_pagination_and_detail_links_are_not_followed():
    agency = REGISTRY['dexters']
    fetch = AsyncMock(return_value=card() + '<a href="https://evil.example/?page=2">Next</a>'
        '<a href="/property-for-rent/other-w2/3333?page=2">Next</a>')
    report = await agency.search_report('Bayswater', False, SearchFilters(), max_pages=5,
        fetch_html=fetch, fetch_json=AsyncMock())
    assert len(report.pages_visited) == 1 and not report.pending_urls


def test_pagination_stays_within_search_route_and_filters():
    agency = REGISTRY['dexters']
    start = agency.search_url('Bayswater', False)
    html = ('<a href="/news?page=2">Next</a>'
            '<a href="'+start+'/page-2?location=chelsea">Next</a>'
            '<a href="'+start+'/page-2">Next</a>')
    assert agency._pagination(start, BeautifulSoup(html, 'html.parser')) == [start+'/page-2']


def test_redirected_search_can_follow_its_canonical_pagination():
    agency = REGISTRY['hamptons']
    start = agency.search_url('Bayswater', False)
    canonical = 'https://www.hamptons.co.uk/london/bayswater/lettings'
    html = '<link rel="canonical" href="'+canonical+'"><a href="'+canonical+'/page-2">2</a>'
    assert agency._pagination(start, BeautifulSoup(html, 'html.parser')) == [canonical+'/page-2']


def test_load_more_link_is_followed_as_pagination():
    agency = REGISTRY['chestertons']
    start = agency.search_url('Bayswater', False)
    html = '<a href="'+start+'?page=2">Load More Properties</a>'
    assert agency._pagination(start, BeautifulSoup(html, 'html.parser')) == [start+'?page=2']


def test_malformed_urls_do_not_discard_valid_cards():
    html = card() + '<a href="http://[invalid">Next</a><link rel="canonical" href="http://[invalid">'
    agency = REGISTRY['dexters']
    assert len(agency._hits(html)) == 1
    assert agency._pagination(agency.search_url('Bayswater', False), BeautifulSoup(html, 'html.parser')) == []


async def test_dynamic_next_button_is_explicit_partial_coverage():
    fetch = AsyncMock(return_value=card() + '<button>Load more</button>')
    report = await REGISTRY['dexters'].search_report('Bayswater', False, SearchFilters(), max_pages=5,
        fetch_html=fetch, fetch_json=AsyncMock())
    assert report.warnings


def test_price_cannot_be_borrowed_from_sibling_card_or_footer():
    html = card(price='') + card('2222', price='£9,000 pcm') + '<footer>From £500 pcm</footer>'
    hits = REGISTRY['dexters']._hits(html, SearchFilters(max_price=3500))
    assert len(hits) == 1 and hits[0].id == '1111' and hits[0].price == ''


def test_html_entities_and_reversed_bed_labels_are_decoded():
    html = card(price='&#163;3,400 ( pcm )', extra='<span>Bedrooms</span><span>2</span>')
    hit = REGISTRY['dexters']._hits(html)[0]
    assert hit.price == '£3,400 pcm' and hit.beds == 2


def test_mixed_listing_container_does_not_assign_shared_price():
    html = '<article><a href="/property-for-rent/flat-w2/1111">A</a><a href="/property-for-rent/flat-w2/2222">B</a>£3,000 pcm</article>'
    assert all(hit.price == '' for hit in REGISTRY['dexters']._hits(html))


def test_monthly_price_beats_weekly_rounding_and_sale_is_not_pcm():
    assert _price_pcm('£808p/w (£3,500 pcm)') == 3500
    hit = REGISTRY['dexters']._hits(card(price='£500,000'), for_sale=True)[0]
    assert hit.price == '£500,000'


def test_let_agreed_image_alt_and_studios_are_not_two_bed_candidates():
    html = card(extra='<img alt="Agreement Signed">2 Bedrooms')
    assert not REGISTRY['dexters']._hits(html)
    html = card(extra='', slug='studio-flat-w2')
    assert not REGISTRY['dexters']._hits(html, SearchFilters(min_beds=2))


def test_external_listing_link_is_ignored():
    assert REGISTRY['dexters']._hits('<a href="https://evil.example/property-for-rent/flat-w2/1111">Property</a>') == []


def test_postcode_digits_are_never_bedroom_counts():
    hit = REGISTRY['dexters']._hits(card(extra='<img alt="London W2"><img alt="Bedroom icon">'))[0]
    assert hit.beds is None


def test_word_bedroom_description_is_supported():
    assert REGISTRY['dexters']._hits(card(extra='A two-bedroom apartment'))[0].beds == 2


def test_kfh_prices_are_read_from_individual_sales_wrap_cards():
    html = '<div class="sales-wrap"><a href="/property-to-rent/2-bedroom-flat-in-example-w2-1001/">Example Gardens</a>£3,200 pcm</div>'
    hit = REGISTRY['kfh']._hits(html)[0]
    assert hit.price == '£3,200 pcm' and hit.beds == 2


async def test_tool_exposes_partial_failure_without_inventing_zero_inventory():
    report = SearchReport(pages_visited=['https://example.com/1'], pending_urls=['https://example.com/2'],
                          warnings=['Second page failed'], stop_reason='page_failed')
    tool = AgencySearchTool(AsyncMock(return_value=report))
    result = await tool(AgencySearchInput(agency='dexters', location='Bayswater'), None)
    payload = json.loads(result.text)
    assert payload['status'] == 'partial'
    assert payload['coverage'] == 'observed_pages_only'
    assert payload['pending_urls'] and payload['warnings']
