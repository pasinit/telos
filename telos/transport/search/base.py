"""SearchEngine protocol, engine factory, and the high-level ``search()`` helper.

``search(query)`` resolves the configured engine via :data:`_ENGINES`, caches
the instance so backoff state (e.g. Gemini's rate-limit window) survives
across calls, and falls back to DuckDuckGo on transient failures.
"""

import logging
import os
from collections.abc import Callable
from functools import cache, partial
from typing import Protocol, runtime_checkable

from telos.transport.search.duckduckgo import DuckDuckGoSearch
from telos.transport.search.gemini import GeminiSearch
from telos.transport.search.serpapi import SerpApiSearch
from telos.transport.search.serper import SerperSearch

log = logging.getLogger(__name__)


@runtime_checkable
class SearchEngine(Protocol):
    """Protocol for search engine implementations."""

    async def search(self, query: str) -> tuple[str, bool]:
        """Run a search query.

        Returns (result_text, is_error).
        """
        ...


_ENGINES: dict[str, Callable[[], SearchEngine]] = {
    "gemini": GeminiSearch,
    "serper": SerperSearch,
    "serpapi": SerpApiSearch,
    "duckduckgo": DuckDuckGoSearch,
    "bing": partial(DuckDuckGoSearch, backends=("bing",)),
}


def get_engine(name: str | None = None) -> SearchEngine:
    """Return a ``SearchEngine`` instance for the given engine name.

    Falls back to the ``SEARCH_ENGINE`` env var, then to ``gemini``.
    Unknown names fall back to Gemini.
    """
    if name is None:
        name = os.getenv("SEARCH_ENGINE", "gemini")
    return _engine_for_name(name)


@cache
def _engine_for_name(name: str) -> SearchEngine:
    """Cache one engine instance per configured engine name."""
    return _ENGINES.get(name, GeminiSearch)()


async def search(query: str) -> tuple[str, bool]:
    """Run a web search via the configured engine.

    Returns ``(result_text, is_error)``. Engine selection follows the
    ``SEARCH_ENGINE`` env var. On Gemini rate-limit (429 / RESOURCE_EXHAUSTED)
    the engine's backoff clock is bumped and the next call falls through to
    DuckDuckGo until the window expires.
    Explicit Bing selection stays Bing-only, including when it fails.
    """
    engine_name = os.getenv("SEARCH_ENGINE", "gemini")
    engine = get_engine(engine_name)

    try:
        if isinstance(engine, (GeminiSearch, SerperSearch)) and engine.is_backing_off():
            return await _engine_for_name("duckduckgo").search(query)
        result, is_error = await engine.search(query)
        if is_error and engine_name not in ("duckduckgo", "bing"):
            return await _engine_for_name("duckduckgo").search(query)
        return result, is_error
    except Exception as e:
        err_str = str(e)
        if isinstance(engine, GeminiSearch) and ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str):
            engine.record_rate_limit(60)
            log.warning("search engine gemini rate limited — skipping for 60s")
        else:
            # HTTP exception strings may include API keys in request URLs.
            log.warning("search engine %s failed: %s", engine_name, type(e).__name__)
        # A selected free backend must not silently switch to other engines.
        if engine_name not in ("duckduckgo", "bing"):
            try:
                return await _engine_for_name("duckduckgo").search(query)
            except Exception as e2:
                return f"Search failed: {type(e2).__name__}. Search coverage is unavailable.", True
        return f"Search failed: {type(e).__name__}. Search coverage is unavailable.", True
