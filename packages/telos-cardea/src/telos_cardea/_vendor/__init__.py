"""Verbatim copies of Telos's pure HTML parsers.

The package must not import the ``telos`` app, so these modules are copied
from it. Only import lines differ (see ``SOURCES``); ``tests/test_vendor_sync.py``
fails when a copy drifts from its source in the same checkout. Re-copy rather than editing here.
"""

# vendored module -> (source path relative to the repo root, import rewrites)
SOURCES: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "rightmove.py": ("telos/transport/browser/rightmove.py", ()),
    "onthemarket.py": ("telos/transport/browser/onthemarket.py", ()),
    "zoopla.py": ("telos/transport/browser/zoopla.py", ()),
    "agency_types.py": ("telos/domain/agencies.py", ()),
    "agencies.py": (
        "telos/transport/browser/agencies.py",
        (("from telos.domain.agencies import", "from telos_cardea._vendor.agency_types import"),),
    ),
}
