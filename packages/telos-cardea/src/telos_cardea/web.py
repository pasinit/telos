"""``web.search`` read operation, following ``telos.transport.search``.

Engines: ``serper`` and ``serpapi`` (API keys from config), ``duckduckgo`` (the
free DDGS path over Bing, Brave, DuckDuckGo and Yahoo) and ``bing`` (DDGS
Bing-only, no fallback). A failed paid engine falls back to the free path, as in
Telos. Telos's ``gemini`` grounding engine is not packaged; it is treated as
``duckduckgo``. DDGS needs the ``search`` extra.
"""

import asyncio
import logging
import re
from typing import Any
from urllib.parse import SplitResult, urlsplit

import httpx

from telos_cardea._cardea import Arguments, OperationContext, Output
from telos_cardea.config import Config, load_config

log = logging.getLogger(__name__)

FREE_BACKENDS = ("bing", "brave", "duckduckgo", "yahoo")
MAX_RESULTS = 10


class SearchUnavailable(RuntimeError):
    pass


def _hit(title: Any, url: Any, snippet: Any) -> dict[str, str]:
    return {"title": str(title or ""), "url": str(url or ""), "snippet": str(snippet or "")}


async def _serper(query: str, config: Config, client: httpx.AsyncClient) -> list[dict[str, str]]:
    if not config.serper_key:
        raise SearchUnavailable("SERPER_KEY not set")
    response = await client.post("https://google.serper.dev/search", json={"q": query, "num": MAX_RESULTS},
                                 headers={"X-API-KEY": config.serper_key})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("organic"), list):
        raise SearchUnavailable("Serper returned an unrecognised response")
    return [_hit(r.get("title"), r.get("link"), r.get("snippet")) for r in data["organic"] if isinstance(r, dict)]


async def _serpapi(query: str, config: Config, client: httpx.AsyncClient) -> list[dict[str, str]]:
    if not config.serpapi_key:
        raise SearchUnavailable("SERPAPI_KEY not set")
    response = await client.get("https://serpapi.com/search",
                                params={"q": query, "engine": "google", "num": MAX_RESULTS, "api_key": config.serpapi_key})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("organic_results"), list):
        raise SearchUnavailable("SerpAPI returned an unrecognised response")
    return [_hit(r.get("title"), r.get("link"), r.get("snippet")) for r in data["organic_results"] if isinstance(r, dict)]


def _ddgs(query: str, backend: str) -> list[dict[str, Any]]:
    from ddgs import DDGS

    return list(DDGS(timeout=8).text(query, max_results=MAX_RESULTS, backend=backend))


async def _free(query: str, backends: tuple[str, ...]) -> list[dict[str, str]]:
    """Try each backend in turn; an empty answer is not a failure."""
    failures = 0
    for backend in backends:
        try:
            raw = await asyncio.to_thread(_ddgs, query, backend)
        except ImportError as error:
            raise SearchUnavailable("free search needs the telos-cardea[search] extra") from error
        except Exception as error:
            if str(error) != "No results found.":
                failures += 1
                log.warning("search backend %s unavailable: %s", backend, type(error).__name__)
            continue
        usable = filter_results(query, raw)
        if usable:
            return [_hit(r.get("title"), r.get("href"), r.get("body")) for r in usable]
    if failures == len(backends):
        raise SearchUnavailable("every free backend failed")
    return []


def filter_results(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop ad redirects and enforce explicit ``site:``/``-site:`` restrictions (from Telos)."""
    restrictions = re.findall(r'(?<![\w:-])(-?)site:([^\s)]+)', query, re.IGNORECASE)
    sites = [(neg, urlsplit("https://" + s.removeprefix("https://").removeprefix("http://").strip("\"'"))) for neg, s in restrictions]
    filtered, seen = [], set()
    for result in results:
        href = result.get("href", "") if isinstance(result, dict) else ""
        if not isinstance(href, str):
            continue
        try:
            url = urlsplit(href)
            host = (url.hostname or "").lower()
        except ValueError:
            continue
        if url.scheme not in ("https", "http") or not host or url.username or href in seen:
            continue
        if ((host == "bing.com" or host.endswith(".bing.com")) and url.path.startswith("/aclick")
                or (host == "google.com" or host.endswith(".google.com")) and url.path.startswith("/aclk")
                or host == "duckduckgo.com" and url.path.startswith("/y.js")):
            continue
        positive = [site for neg, site in sites if not neg]
        if positive and not any(_matches_site(url, site) for site in positive):
            continue
        if any(_matches_site(url, site) for neg, site in sites if neg):
            continue
        seen.add(href)
        filtered.append(result)
    return filtered


def _matches_site(url: SplitResult, site: SplitResult) -> bool:
    host = (url.hostname or "").lower()
    domain = (site.hostname or "").lower()
    return (host == domain or host.endswith("." + domain)) and url.path.startswith(site.path)


async def search(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                 client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``web.search``."""
    config = config or load_config()
    query = str(arguments["query"])
    engine = config.search_engine if config.search_engine in {"serper", "serpapi", "bing"} else "duckduckgo"
    used = engine
    try:
        if engine in {"serper", "serpapi"}:
            try:
                async with _client(client) as http:
                    hits = await (_serper if engine == "serper" else _serpapi)(query, config, http)
            except Exception as error:
                log.warning("search engine %s failed: %s", engine, type(error).__name__)
                used = "duckduckgo"
                hits = await _free(query, FREE_BACKENDS)
        else:
            hits = await _free(query, ("bing",) if engine == "bing" else FREE_BACKENDS)
    except SearchUnavailable as error:
        return {"engine": used, "status": "unavailable", "reason": str(error), "results": [],
                "caveat": "Search coverage is unavailable; this is not evidence of absence."}
    return {"engine": used, "status": "results" if hits else "no_results", "results": hits[:MAX_RESULTS]}


class _client:
    """Use the injected client, or own one for the call."""

    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self._given = client
        self._own: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._given is not None:
            return self._given
        self._own = httpx.AsyncClient(timeout=30)
        return self._own

    async def __aexit__(self, *exc: object) -> None:
        if self._own is not None:
            await self._own.aclose()
