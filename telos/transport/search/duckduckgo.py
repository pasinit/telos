"""DDGS-backed search, with explicit backend selection and no API key."""

import asyncio
import logging
import random
import re
import time
from urllib.parse import SplitResult, urlsplit

from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException

log = logging.getLogger(__name__)

# A genuine rate-limit / block benches a backend for a full minute. A transient
# timeout or flaky response is retried once first, and then benched only
# briefly (jittered) — one slow reply used to remove a backend for 60s, so a
# single flaky response inside a multi-query run could bench all four backends
# together and collapse the whole run into "backends failed, cooling down...".
_RATELIMIT_COOLDOWN = 60.0
_TRANSIENT_COOLDOWN = 8.0
# Base backoff before the single retry; jittered to avoid synchronized retries.
_RETRY_BACKOFF = 0.4


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
                results, outcome = await self._query_backend(query, backend)
                if outcome in ("no_results", "benched"):
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

    async def _query_backend(self, query: str, backend: str) -> tuple[list[dict] | None, str]:
        """Query one backend, retrying once on a transient error before benching.

        Returns ``(results, outcome)``:
        - ``("ok")`` — ``results`` is the raw list (possibly empty).
        - ``("no_results")`` — DDGS reported no extracted hits; not a failure,
          not benched (same semantics as before).
        - ``("benched")`` — the backend failed; a cooldown was set. A genuine
          rate-limit/block gets the full 60s; a transient timeout/flaky
          response is retried once and then benched only briefly.
        """
        last_exc: Exception | None = None
        for attempt in range(2):  # initial try + one retry
            if attempt == 0:
                await asyncio.sleep(max(0, 1.0 - (time.monotonic() - self._last_request)))
            else:
                await asyncio.sleep(_RETRY_BACKOFF * (1 + random.random()))
            self._last_request = time.monotonic()
            try:
                return await asyncio.to_thread(self._search_sync, query, backend), "ok"
            except RatelimitException as exc:
                # A rate-limit won't clear on an immediate retry — bench it now.
                last_exc = exc
                break
            except DDGSException as exc:
                # DDGS uses this generic exception for a query with no extracted
                # hits. It is not a provider-wide failure, so don't retry/bench.
                if str(exc) == "No results found.":
                    return [], "no_results"
                last_exc = exc
            except Exception as exc:
                last_exc = exc
        rate_limited = isinstance(last_exc, RatelimitException)
        cooldown = _RATELIMIT_COOLDOWN if rate_limited else _TRANSIENT_COOLDOWN * (1 + random.random())
        self._cooldowns[backend] = time.monotonic() + cooldown
        log.warning("search backend %s unavailable (%s); cooling down %.0fs",
                    backend, type(last_exc).__name__ if last_exc else "unknown", cooldown)
        return None, "benched"

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
