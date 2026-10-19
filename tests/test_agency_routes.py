from unittest.mock import AsyncMock

import pytest

from telos.domain.agencies import SearchFilters
from telos.transport.browser.agencies import REGISTRY


@pytest.mark.parametrize('area', ['Marylebone', 'Bayswater'])
async def test_kfh_renders_area_search_instead_of_using_generic_server_shell(area):
    html = '<a href="/property-to-rent/2-bedroom-flat-bayswater-w2-12345/">Example</a>'
    static = AsyncMock(return_value='<html>London search shell</html>')
    rendered = AsyncMock(return_value=html)
    hits = await REGISTRY['kfh'].search(area, False, SearchFilters(), fetch_html=static,
                                      fetch_html_js=rendered, fetch_json=AsyncMock())
    assert len(hits) == 1
    static.assert_not_called()
    rendered.assert_awaited_once_with(f'https://www.kfh.co.uk/property/to-rent/in-{area.lower()}/')


@pytest.mark.parametrize('agency,path,expected', [
    ('openrent', '/property-to-rent/london/2-bed-flat-corringham-w2/3045096', '3045096'),
    ('marshandparsons', '/properties-to-rent/london/property/CSG220682/kensington-gardens-square/', 'CSG220682'),
])
def test_observed_detail_paths_preserve_full_reference(agency, path, expected):
    impl = REGISTRY[agency]
    hits = impl._hits(f'<a href="{path}">Property</a>')
    assert hits[0].id == expected
    assert hits[0].url == f'https://www.{impl.host}{path}'
    assert impl.enrich_detail(f'https://www.{impl.host}{path}', '<meta property="og:title" content="2 bedroom flat W2">')['data']['id'] == expected


def test_area_links_are_not_property_ids():
    for agency in ('openrent', 'marshandparsons'):
        impl = REGISTRY[agency]
        assert impl._hits(f'<a href="{impl.search_url("Bayswater", False)}">Search</a>') == []
