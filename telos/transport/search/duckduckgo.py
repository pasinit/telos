"""DDGS-backed search, with explicit backend selection and no API key."""

import asyncio
import logging
import re
import time
from urllib.parse import SplitResult, urlsplit

from ddgs import DDGS
from ddgs.exceptions import DDGSException

log = logging.getLogger(__name__)


class DuckDuckGoSearch:
    """Legacy duckduckgo provider name; DDGS can query multiple engines."""

    def __init__(self, *, backends: tuple[str, ...] = ("bing", "brave", "duckduckgo", "yahoo")) -> None:
        self._backends = backends
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cooldowns: dict[str, float] = {}

    async def search(self, query: str) -> tuple[str, bool]:
        """Try independent engines, without DDGS auto's parallel failure race.

        Serialize the shared fallback instance and pace requests. A failed or
        filtered response is missing coverage, never an empty inventory claim.
        """
        async with self._lock:
            empty = False
            for backend in self._backends:
                if self._cooldowns.get(backend, 0) > time.monotonic():
                    continue
                await asyncio.sleep(max(0, 1.0 - (time.monotonic() - self._last_request)))
                self._last_request = time.monotonic()
                try:
                    results = await asyncio.to_thread(self._search_sync, query, backend)
                except Exception as exc:
                    # DDGS uses this generic exception for a query with no
                    # extracted hits. It is not a provider-wide rate limit.
                    if isinstance(exc, DDGSException) and str(exc) == "No results found.":
                        continue
                    self._cooldowns[backend] = time.monotonic() + 60
                    log.warning("search backend %s unavailable: %s", backend, type(exc).__name__)
                    continue
                if not results:
                    empty = True
                    continue
                usable = filter_results(query, results)
                if usable:
                    return "\n\n".join(f"{r.get('title', '')}\n{r['href']}\n{r.get('body', '')}" for r in usable[:5]), False
            if empty:
                return "No usable results returned by the checked search backends; coverage may be incomplete.", False
            return "Web search unavailable: backends failed, are cooling down, or returned only unusable results. This is not empty inventory.", True

    @staticmethod
    def _search_sync(query: str, backend: str) -> list[dict]:
        return DDGS(timeout=8).text(query, max_results=10, backend=backend)


def filter_results(query: str, results: list[dict]) -> list[dict]:
    """Enforce explicit site restrictions on organic URLs, not ad redirects."""
    restrictions = re.findall(r'(?<![\w:-])(-?)site:([^\s)]+)', query, re.IGNORECASE)
    sites = [(negative, urlsplit('https://' + site.removeprefix('https://').removeprefix('http://').strip('"\'')))
             for negative, site in restrictions]
    filtered = []
    seen = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        href = result.get('href', '')
        if not isinstance(href, str):
            continue
        try:
            url = urlsplit(href)
            host = (url.hostname or '').lower()
        except ValueError:
            continue
        if url.scheme not in ('https', 'http') or not host or url.username or href in seen:
            continue
        if ((host == 'bing.com' or host.endswith('.bing.com')) and url.path.startswith('/aclick')
                or (host == 'google.com' or host.endswith('.google.com')) and url.path.startswith('/aclk')
                or host == 'duckduckgo.com' and url.path.startswith('/y.js')):
            continue
        positive = [site for negative, site in sites if not negative]
        if positive and not any(_matches_site(url, site) for site in positive):
            continue
        if any(_matches_site(url, site) for negative, site in sites if negative):
            continue
        seen.add(href)
        filtered.append(result)
    return filtered


def _matches_site(url: SplitResult, site: SplitResult) -> bool:
    host = (url.hostname or '').lower()
    domain = (site.hostname or '').lower()
    return (host == domain or host.endswith('.' + domain)) and url.path.startswith(site.path)
