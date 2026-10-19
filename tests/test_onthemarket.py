import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import telos.browser as browser_module
from telos.browser import BrowserSession, _enrich_known_site
from telos.transport.browser import fetcher, onthemarket
from telos.transport.browser.images import extract_images_from_html

SEARCH_URL = 'https://www.onthemarket.com/to-rent/property/w2/?max-price=3500&min-bedrooms=2&max-bedrooms=2'
DETAIL_URL = 'https://www.onthemarket.com/details/123/'
PLAN = 'https://media.onthemarket.com/properties/123/floor-plan-0.jpg'
PHOTO = 'https://media.onthemarket.com/properties/123/image-0.jpg'


def page(state, body=''):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({
        'props': {'pageProps': {}, 'initialReduxState': state},
    }) + '</script>' + body


@pytest.fixture
def property_data():
    return {'id': 123, 'propertyTitle': '2 bedroom flat to rent', 'displayAddress': 'Example W2',
            'bedrooms': 2, 'bathrooms': 1, 'price': 'GBP 3500 pcm', 'minimumAreaSqFt': 840,
            'minimumAreaSqM': 78, 'description': '<p>Wood flooring</p><p>Modern purpose-built development</p>',
            'features': [{'id': 1, 'feature': 'Fifth floor with lift'}],
            'lettingDetails': {'items': ['Availability date: 25 Nov 2026']},
            'images': [{'largeUrl': PHOTO}], 'floorplans': [{'largeUrl': PLAN}]}


def test_all_search_cards_and_observed_pagination_survive_noise():
    cards = [{'id': str(i), 'details-url': f'/details/{i}/', 'address': f'Example {i} W2',
              'bedrooms': 2, 'price': 'GBP 3500 pcm', 'features': ['Wood flooring']} for i in range(30)]
    html = page({'results': {'list': cards + [cards[0]], 'totalResults': 64}},
                '<nav>' + 'Filters ' * 10000 + '</nav>'
                '<a href="/to-rent/2-bed-property/w2/?max-price=3500&amp;page=2">2</a>'
                '<a href="/to-rent/2-bed-property/w2/?max-price=3500&amp;page=2">Next</a>'
                '<a href="https://example.com/to-rent/?page=9">Advertisement</a>')
    for adapter in (_enrich_known_site, fetcher._enrich_known_site):
        data = adapter(SEARCH_URL, html)['data']
        assert data['count'] == 30
        assert data['total'] == 64
        assert data['coverage'] == 'current_page'
        assert data['results'][-1]['url'] == 'https://www.onthemarket.com/details/29/'
        assert data['pagination'] == ['https://www.onthemarket.com/to-rent/2-bed-property/w2/?max-price=3500&page=2']


@pytest.mark.parametrize('state', [None, [], {}, {'results': {}}, {'results': {'list': [], 'totalResults': 2}},
                                  {'results': {'list': [{'id': 1}], 'totalResults': 1}}])
def test_unknown_or_inconsistent_shape_is_not_zero_inventory(state):
    assert onthemarket.enrich(SEARCH_URL, page(state)) is None


def test_explicit_empty_results_are_supported():
    assert onthemarket.enrich(SEARCH_URL, page({'results': {'list': [], 'totalResults': 0}}))['data']['count'] == 0


def test_malformed_json_and_wrong_host_are_ignored():
    assert onthemarket.enrich(SEARCH_URL, '<script id="__NEXT_DATA__">bad</script>') is None
    assert not onthemarket.matches('https://onthemarket.com.example.org/details/123/')
    assert onthemarket.enrich('https://example.org/details/123/', page({})) is None


def test_detail_retains_evidence_without_inventing_verification(property_data):
    result = onthemarket.enrich(DETAIL_URL, page({'property': property_data}))
    data = result['data']
    assert data['sqm'] == 78
    assert data['sqft'] == 840
    assert 'Modern purpose-built development' in data['description']
    assert data['letting_details'] == ['Availability date: 25 Nov 2026']
    assert data['floorplans'] == [PLAN]
    assert data['features'] == ['Fifth floor with lift']
    assert 'verified' not in data
    del property_data['minimumAreaSqM']
    assert 'sqm' not in onthemarket.enrich(DETAIL_URL, page({'property': property_data}))['data']


def test_detail_id_must_match_the_page(property_data):
    property_data['id'] = 456
    assert onthemarket.enrich(DETAIL_URL, page({'property': property_data})) is None


def test_floorplan_selection_excludes_adverts_and_gallery(property_data):
    html = page({'property': property_data}, '<img src="https://ads.example.com/banner.jpg" width="1000" height="1000">')
    assert extract_images_from_html(html, DETAIL_URL) == [PLAN, PHOTO]
    assert extract_images_from_html(html, DETAIL_URL + '#/floorplans/1') == [PLAN]
    assert extract_images_from_html(html, DETAIL_URL + '#/floorplans/0') == []
    assert extract_images_from_html(html, DETAIL_URL + '#/floorplans/2') == []
    property_data['floorplans'].append({'largeUrl': PLAN.replace('-0', '-1')})
    assert extract_images_from_html(page({'property': property_data}), DETAIL_URL + '#/floorplans/2') == [PLAN.replace('-0', '-1')]


def test_unknown_otm_page_does_not_fall_back_to_adverts():
    assert extract_images_from_html('<img src="https://ads.example.com/banner.jpg">', DETAIL_URL) == []


def test_explicit_photo_selection_is_not_replaced_by_floorplans(property_data):
    html = page({'property': property_data})
    assert extract_images_from_html(html, DETAIL_URL + '#/photos/1') == [PHOTO]
    assert extract_images_from_html(html, DETAIL_URL + '#/photos/2') == []


def test_listing_without_floorplan_does_not_invent_one(property_data):
    property_data['floorplans'] = []
    html = page({'property': property_data})
    assert extract_images_from_html(html, DETAIL_URL) == [PHOTO]
    assert extract_images_from_html(html, DETAIL_URL + '#/floorplans/1') == []


@pytest.mark.parametrize('visible', [True, False])
async def test_consent_uses_only_exact_essential_control(visible, monkeypatch):
    session = BrowserSession()
    button = SimpleNamespace(is_visible=AsyncMock(return_value=visible), click=AsyncMock())
    region = SimpleNamespace(get_by_role=MagicMock(return_value=button))
    session._page = SimpleNamespace(url=DETAIL_URL, locator=MagicMock(return_value=region))
    monkeypatch.setattr(session, '_settle', AsyncMock())
    await session._dismiss_site_overlay()
    session._page.locator.assert_called_once_with('#ccc')
    region.get_by_role.assert_called_once_with('button', name='Essential cookies only', exact=True)
    assert button.click.await_count == int(visible)


async def test_other_sites_are_not_given_cookie_consent():
    session = BrowserSession()
    session._page = SimpleNamespace(url='https://example.com', locator=MagicMock())
    await session._dismiss_site_overlay()
    session._page.locator.assert_not_called()


async def test_click_dismisses_overlay_before_resolving_target(monkeypatch):
    session = BrowserSession()
    session._page = MagicMock()
    session._context = MagicMock()
    calls = []

    async def dismiss():
        calls.append('dismiss')

    async def resolve(target):
        assert calls == ['dismiss']
        assert target == 'Floorplan'
        return SimpleNamespace(click=AsyncMock(return_value=None))

    monkeypatch.setattr(session, '_dismiss_site_overlay', dismiss)
    monkeypatch.setattr(session, '_resolve_clickable', resolve)
    monkeypatch.setattr(session, '_settle', AsyncMock())
    monkeypatch.setattr(session, '_capture', AsyncMock(return_value={'ok': True}))
    assert (await session.click('Floorplan'))['ok']


async def test_browser_capture_delivers_floorplan_image(property_data, monkeypatch):
    session = BrowserSession()
    session._page = SimpleNamespace(url=DETAIL_URL + '#/floorplans/1', title=AsyncMock(return_value='Example'),
                                   content=AsyncMock(return_value=page({'property': property_data})))
    monkeypatch.setattr(session, '_collect_interactive', AsyncMock(return_value=[]))
    download = AsyncMock(return_value='base64-floorplan')
    monkeypatch.setattr(browser_module, 'download_and_resize_image', download)
    result = await session._capture()
    assert result['images'] == ['base64-floorplan']
    assert 'Availability date' in result['content']
    download.assert_awaited_once_with(PLAN)


async def test_fetch_delivers_structured_listing_and_floorplan(property_data, monkeypatch):
    html = page({'property': property_data})
    monkeypatch.setattr(fetcher, '_race_curl_vs_playwright', AsyncMock(return_value=(html, 200, DETAIL_URL)))
    download = AsyncMock(side_effect=lambda url: 'base64:' + url)
    monkeypatch.setattr(fetcher, 'download_and_resize_image', download)
    result = await fetcher.fetch(DETAIL_URL)
    assert result['structured']['data']['floorplans'] == [PLAN]
    assert result['images'][0] == 'base64:' + PLAN
