import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import telos.browser as browser_module
from telos.browser import BrowserSession, _enrich_known_site
from telos.core.delegation import DelegationGraph
from telos.core.run_context import RunContext
from telos.core.tool_context import ToolContext
from telos.domain.agencies import AgencySearchUnavailable, SearchFilters
from telos.domain.tools.agency_search import AgencySearchInput, AgencySearchTool
from telos.domain.tools.rightmove import RightmoveSearchInput, RightmoveSearchTool, _build_search_url
from telos.domain.tools.web import _shape_browser_result
from telos.transport.browser.agencies import KfhSearch, make_search_dispatch
from telos.transport.browser.images import html_to_text
from telos.transport.browser.rightmove import enrich


def context(tmp_path: Path) -> ToolContext:
    return ToolContext(RunContext("u", DelegationGraph()), "homey", tmp_path, tmp_path)


def test_navigation_does_not_hide_property_details():
    html = '<nav>' + ('Buy Rent Mortgage ' * 1000) + '</nav><main><h1>Example flat</h1><p>70 sqm; 2 bedrooms</p></main>'
    text = html_to_text(html, max_chars=200)
    assert '70 sqm' in text
    assert 'Mortgage' not in text


def test_empty_modal_main_does_not_discard_listing_body():
    html = ('<nav>Buy Rent Mortgage</nav>'
            '<main aria-hidden="true" class="MuiBackdrop-root"></main>'
            '<div><h1>Example Place</h1><p>Two bedrooms, 757 sqft, period building.</p></div>')
    text = html_to_text(html)
    assert 'Example Place' in text
    assert '757 sqft' in text
    assert 'Mortgage' not in text


@pytest.mark.parametrize('placeholder', [
    '<main></main>',
    '<main><script>loading()</script></main>',
    '<main aria-hidden="true">Hidden modal placeholder</main>',
    '<main hidden>Hidden modal placeholder</main>',
])
def test_first_usable_main_is_selected(placeholder):
    html = placeholder + '<div role="main"><h1>Listing</h1><p>70 sqm</p></div><aside>Unrelated</aside>'
    text = html_to_text(html)
    assert '70 sqm' in text
    assert 'placeholder' not in text
    assert 'Unrelated' not in text


def test_truncation_is_explicit_and_conversion_does_not_leak_state():
    assert '[Content truncated' in html_to_text('<main>' + 'x' * 300 + '</main>', max_chars=100)
    assert 'old' not in html_to_text('<p>new</p>')


def test_rightmove_uses_area_page_and_exact_bed_filters():
    url = _build_search_url('Marylebone', 2, 3500, False, 2)
    assert '/property-to-rent/Marylebone.html?' in url
    assert 'maxBedrooms=2' in url
    assert 'includeLetAgreed=false' in url
    assert 'searchLocation' not in url


def test_rightmove_empty_results_are_distinct_from_unknown_shape():
    url = 'https://www.rightmove.co.uk/property-to-rent/Marylebone.html'
    data = {'props': {'pageProps': {'searchResults': {'properties': [], 'resultCount': 0}}}}
    html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
    assert enrich(url, html)['data']['count'] == 0
    assert enrich(url, '<main>No parsed search data</main>') is None


async def test_rightmove_navigation_only_is_an_error(tmp_path):
    async def fetch(url):
        return {'text': 'Buy Rent Mortgage', 'status_code': 200}
    result = await RightmoveSearchTool(fetch)(RightmoveSearchInput(location='Marylebone'), context(tmp_path))
    assert result.is_error
    assert 'not evidence of zero inventory' in result.text


async def test_agency_unrecognised_page_is_not_an_empty_success(tmp_path):
    async def html(url):
        return '<html><nav>Search properties</nav></html>'
    async def api(url):
        return {'error': 'blocked'}
    tool = AgencySearchTool(make_search_dispatch(html, api))
    result = await tool(AgencySearchInput(agency='kfh', location='Marylebone'), context(tmp_path))
    assert result.is_error
    assert json.loads(result.text)['status'] == 'unusable'
    with pytest.raises(AgencySearchUnavailable):
        await make_search_dispatch(html, api)('foxtons', 'Marylebone', False, SearchFilters())


def test_known_out_of_area_results_are_filtered_without_dropping_unknowns():
    filters = SearchFilters(postcode_districts=('W1', 'W2'))
    assert filters.accepts_location('example-w1h-1234')
    assert filters.accepts_location('example-w2-1234')
    assert not filters.accepts_location('example-w11-1234')
    assert not filters.accepts_location('example-se3-1234')
    assert filters.accepts_location('postcode not supplied')
    html = '<a href="/property-to-rent/2-bedroom-flat-eastbrook-road-se3-9021/">GBP 1800</a>'
    assert KfhSearch()._hits(html, filters) == []


async def test_browser_does_not_report_http_failure_as_success(monkeypatch):
    session = BrowserSession()
    session._page = MagicMock(url='https://example.com/blocked', goto=AsyncMock(return_value=SimpleNamespace(status=403, url='https://example.com/blocked')))
    session._context = MagicMock()
    monkeypatch.setattr(session, '_ensure_started', AsyncMock())
    capture = AsyncMock()
    monkeypatch.setattr(session, '_capture', capture)
    result = await session.open('https://example.com/blocked')
    assert not result['ok']
    assert 'HTTP 403' in result['error']
    capture.assert_not_called()


async def test_failed_navigation_does_not_return_previous_listing(monkeypatch):
    session = BrowserSession()
    session._page = MagicMock(url='https://example.com/old', goto=AsyncMock(side_effect=TimeoutError('navigation failed')))
    session._context = MagicMock()
    monkeypatch.setattr(session, '_ensure_started', AsyncMock())
    result = await session.open('https://example.com/new')
    assert not result['ok']
    assert result['content'] == ''


async def test_crashed_browser_is_recreated_before_next_navigation(monkeypatch):
    session = BrowserSession()
    old_browser = SimpleNamespace(close=AsyncMock())
    old_pw = SimpleNamespace(stop=AsyncMock())
    session._browser, session._pw = old_browser, old_pw
    session._page = SimpleNamespace()
    session._on_page_crash(session._page)
    page = SimpleNamespace(on=MagicMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    monkeypatch.setattr(browser_module, 'async_playwright', lambda: SimpleNamespace(start=AsyncMock(return_value=pw)))
    await session._ensure_started()
    old_browser.close.assert_awaited_once()
    old_pw.stop.assert_awaited_once()
    assert session._page is page
    assert not session._crashed


def test_observed_detail_links_are_exposed_for_recovery():
    result = _shape_browser_result({'ok': True, 'url': 'https://example.com', 'content': 'listing',
        'interactive': [{'kind': 'link', 'text': 'Floorplan', 'href': 'https://example.com/plan.png'}]})
    assert 'https://example.com/plan.png' in result.text


def test_stateful_browser_enriches_agency_detail_pages():
    html = '<meta property="og:title" content="2 bedroom flat in Example Road W2"><meta property="og:description" content="High ceilings">'
    result = _enrich_known_site('https://www.chestertons.co.uk/properties/22138263/lettings', html)
    assert result['kind'] == 'listing'
    assert result['data']['beds'] == 2
