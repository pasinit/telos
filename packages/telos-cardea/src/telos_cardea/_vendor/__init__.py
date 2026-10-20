"""Verbatim copies of Telos's pure HTML parsers.

The package must not import the ``telos`` app, so these modules are copied
from it. Only the rewrites in ``SOURCES`` differ: import lines, and one fix in
``onthemarket.py`` (live pages nest the Next.js state under ``pageProps``) until
it lands in Telos. ``tests/test_vendor_sync.py`` fails when a copy drifts from
its source in the same checkout. Re-copy rather than editing here.
"""

# Live OnTheMarket pages (checked 2026-10-20) put the Redux state at
# props.pageProps.initialReduxState; Telos's parser reads props.initialReduxState.
_OTM_LOOP = '    for key in ("props", "initialReduxState"):\n'
_OTM_PAGE_PROPS = (
    _OTM_LOOP,
    '    if isinstance(data, dict) and isinstance(data.get("props"), dict) and "pageProps" in data["props"]:\n'
    '        data = {"props": data["props"]["pageProps"]}  # telos-cardea: Next.js pageProps nesting\n' + _OTM_LOOP,
)

# vendored module -> (source path relative to the repo root, rewrites)
SOURCES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "rightmove.py": ("telos/transport/browser/rightmove.py", ()),
    "onthemarket.py": ("telos/transport/browser/onthemarket.py", (_OTM_PAGE_PROPS,)),
    "zoopla.py": ("telos/transport/browser/zoopla.py", ()),
    "agency_types.py": ("telos/domain/agencies.py", ()),
    "agencies.py": (
        "telos/transport/browser/agencies.py",
        (("from telos.domain.agencies import", "from telos_cardea._vendor.agency_types import"),),
    ),
}
