"""Page fetching for the listing operations, without a browser by default.

Telos's ``telos.transport.browser.fetcher`` races curl_cffi against a patchright
Chromium on every call. Launching Chromium per call is what exhausts memory on
the small host, so here the order is sequential and the browser is opt-in:

1. curl_cffi with Chrome impersonation, rejecting bot pages and JS shells
   (the same heuristics as Telos's fetcher);
2. the configured unblocker proxy (``TELOS_UNBLOCKER_URL``), if any;
3. a headless patchright render, only when ``browser_fallback`` is enabled and
   the ``browser`` extra is installed (confined-worker profile).
"""

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from telos_cardea.config import Config

log = logging.getLogger(__name__)

_EMPTY_MOUNT_RE = re.compile(
    r"""<(?:div|main)\s+[^>]*\bid\s*=\s*["'](?:root|__next|__nuxt|app|svelte)["'][^>]*>\s*</(?:div|main)>""",
    re.IGNORECASE,
)
_JS_HYDRATION_MARKERS = ("__NEXT_DATA__", "window.__NUXT__", "window.__INITIAL_STATE__", "window.__APOLLO_STATE__", "ng-version=", "data-reactroot")
_DATA_BLOB_PATTERNS = (
    re.compile(r"__NEXT_DATA__[^>]*>[^<]{2000,}</script", re.DOTALL),
    re.compile(r"window\.__NUXT__\s*=\s*\{[^<]{2000,}", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*\{[^<]{2000,}", re.DOTALL),
    re.compile(r"window\.__APOLLO_STATE__\s*=\s*\{[^<]{2000,}", re.DOTALL),
    re.compile(r"(?:window\.)?(?:PAGE_MODEL|jsonModel)\s*=\s*\{[^<]{2000,}", re.DOTALL),
)
_STRONG_BOT = ("cf-browser-verification", "datadome", "_imperva_", "px-captcha", "complete the security check",
               "checking your browser before", "incapsula incident")
_WEAK_BOT = ("captcha", "please verify", "bot detection", "access denied", "enable javascript", "checking your browser",
             "just a moment", "ray id")


class FetchError(RuntimeError):
    """No usable page could be fetched; not evidence of empty results."""


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style, noscript"):
        node.decompose()
    return soup.get_text(" ", strip=True)


def looks_like_bot_page(text: str) -> bool:
    lower = text.lower()
    if len(text.strip()) < 200:
        return True
    if any(s in lower for s in _STRONG_BOT):
        return True
    return sum(1 for s in _WEAK_BOT if s in lower) >= 2


def looks_like_js_shell(html: str, text: str) -> bool:
    if _EMPTY_MOUNT_RE.search(html):
        return True
    if len(text.strip()) >= 1500:
        return False
    return any(m in html for m in _JS_HYDRATION_MARKERS)


def usable(html: str) -> bool:
    text = visible_text(html)
    if looks_like_bot_page(text):
        return False
    if looks_like_js_shell(html, text):
        # Hydration blobs carry the data without rendering JS.
        return any(p.search(html) for p in _DATA_BLOB_PATTERNS)
    return True


class Fetcher:
    """``fetch_html`` / ``fetch_json`` / ``fetch_html_js`` for the vendored parsers."""

    def __init__(self, config: Config) -> None:
        self._config = config

    async def fetch_html(self, url: str) -> str:
        reasons: list[str] = []
        try:
            html, status = await asyncio.to_thread(self._curl, url, 15)
            if status < 400 and usable(html):
                return html
            reasons.append(f"curl HTTP {status}" if status >= 400 else "curl returned a bot page or JS shell")
        except Exception as error:
            reasons.append(f"curl {type(error).__name__}")
        unblocked = await self._unblocker(url)
        if unblocked is not None:
            return unblocked
        if self._config.unblocker_url:
            reasons.append("unblocker failed")
        if self._config.browser_fallback:
            try:
                return await self.fetch_html_js(url)
            except Exception as error:
                reasons.append(f"browser {type(error).__name__}")
        raise FetchError("; ".join(reasons))

    async def fetch_json(self, url: str) -> object:
        body = await self.fetch_html(url)
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            match = re.search(r"(\{.*\}|\[.*\])", body, re.DOTALL)
            if not match:
                return None
            try:
                return json.loads(match.group(1))
            except (ValueError, TypeError):
                return None

    async def fetch_html_js(self, url: str) -> str:
        from telos_cardea.browser import render

        return await render(url)

    @staticmethod
    def _curl(url: str, timeout: int) -> tuple[str, int]:
        from curl_cffi import requests as curl_requests

        response: Any = curl_requests.get(url, impersonate="chrome", timeout=timeout, allow_redirects=True)
        return str(response.text), int(response.status_code)

    async def _unblocker(self, url: str) -> str | None:
        template = self._config.unblocker_url.strip()
        if "{url}" not in template:
            return None
        proxied = template.replace("{url}", quote(url, safe=""))
        try:
            html, status = await asyncio.to_thread(self._curl, proxied, self._config.unblocker_timeout)
        except Exception as error:
            log.warning("unblocker request failed: %s", type(error).__name__)
            return None
        if status >= 400 or not html or looks_like_bot_page(visible_text(html)):
            return None
        return html
