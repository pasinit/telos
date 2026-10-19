"""``fetch(url)`` races curl_cffi against Playwright for fast, bot-resistant fetching.

The race gives static pages a ~10× speedup over Playwright-only (curl returns
in <1 s on static pages; Playwright takes 8–20 s on JS-heavy SPAs due to the
networkidle wait + tail wait).  The persistent BrowserSession in
``telos/browser.py`` is separate and backs the multi-step ``browser_*`` tools.

Design note (Q5 resolution, 2026-05-12): this race was almost dropped in favour
of Playwright-only but was kept after live-debugging slow ``browser_open`` calls
on Rightmove.  Do not collapse to Playwright-only without profiling first.
"""

import asyncio
import contextlib
import json as _json
import logging
import os
import re
from typing import NotRequired, TypedDict
from urllib.parse import quote

from curl_cffi import requests as curl_requests

from telos.transport.browser import agencies, onthemarket, rightmove, zoopla
from telos.transport.browser.driver import start_driver, stop_driver
from telos.transport.browser.images import (
    download_and_resize_image,
    extract_images_from_html,
    html_to_text,
)

log = logging.getLogger(__name__)


class FetchResult(TypedDict):
    text: str
    images: list[str]
    status_code: int
    final_url: str
    structured: NotRequired[dict]


# ---------------------------------------------------------------------------
# Bot-page / JS-shell heuristics
# ---------------------------------------------------------------------------

_EMPTY_MOUNT_RE = re.compile(
    r"""<(?:div|main)\s+[^>]*\bid\s*=\s*["'](?:root|__next|__nuxt|app|svelte)["'][^>]*>\s*</(?:div|main)>""",
    re.IGNORECASE,
)

_JS_HYDRATION_MARKERS = (
    "__NEXT_DATA__",           # Next.js hydration blob
    "window.__NUXT__",         # Nuxt
    "window.__INITIAL_STATE__",  # Vue / generic SPA hydration
    "window.__APOLLO_STATE__",   # Apollo / GraphQL SPAs
    "ng-version=",             # Angular
    "data-reactroot",          # React DOM root marker
)

# Some pages look like JS shells but carry the data in a serialised hydration
# blob inside the wire HTML.  When we detect one of these blobs with a
# substantial payload we accept the response without rendering JS.
_DATA_BLOB_PATTERNS = (
    re.compile(r"__NEXT_DATA__[^>]*>[^<]{2000,}</script", re.DOTALL),
    re.compile(r"window\.__NUXT__\s*=\s*\{[^<]{2000,}", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*\{[^<]{2000,}", re.DOTALL),
    re.compile(r"window\.__APOLLO_STATE__\s*=\s*\{[^<]{2000,}", re.DOTALL),
    re.compile(r"(?:window\.)?(?:PAGE_MODEL|jsonModel)\s*=\s*\{[^<]{2000,}", re.DOTALL),
)

_PLAYWRIGHT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Wall-clock bounds on the browser fetch path. Individual page ops (goto,
# networkidle) already carry timeouts, but launch/close/content did not — an
# unbounded await there wedged the event loop for days (2026-08-26). These make
# every phase fail fast instead. ``_BROWSER_LAUNCH_MS`` is patchright's own
# launch timeout (milliseconds); the asyncio bounds guard the awaits themselves.
_BROWSER_LAUNCH_MS = 30_000
_BROWSER_LAUNCH_S = 35.0
_BROWSER_WORK_S = 60.0
_BROWSER_CLOSE_S = 10.0


def _looks_like_bot_page(text: str) -> bool:
    """Detect bot-detection pages, CAPTCHAs, or empty JS-rendered shells."""
    lower = text.lower()
    if len(text.strip()) < 200:
        return True
    strong = [
        "cf-browser-verification",
        "datadome",
        "_imperva_",
        "px-captcha",
        "complete the security check",
        "checking your browser before",
        "incapsula incident",
    ]
    if any(s in lower for s in strong):
        return True
    weak = ["captcha", "please verify", "bot detection", "access denied",
            "enable javascript", "checking your browser", "just a moment",
            "ray id"]
    return sum(1 for s in weak if s in lower) >= 2


# Anti-bot challenge fingerprints that live in <script>/<iframe> markup and so
# survive into the *raw* HTML but get stripped by ``html_to_text`` — meaning a
# shell page can slip past ``_looks_like_bot_page`` (which only sees the visible
# text). Used to reject a rendered challenge page so the fetch falls through to
# the unblocker. Kept tight (vendor-specific) to avoid false positives.
_CHALLENGE_FINGERPRINTS = (
    "token.awswaf.com", "awswaf.com", "/mp_verify",              # AWS WAF
    "challenges.cloudflare.com", "/cdn-cgi/challenge-platform",  # Cloudflare managed/turnstile
    "captcha-delivery.com", "datadome",                          # DataDome
    "_imperva_", "incapsula",                                    # Imperva
    "px-captcha", "perimeterx",                                  # PerimeterX
)


def _has_embedded_data(html: str) -> bool:
    """True if the HTML carries a substantial SPA hydration / state blob."""
    return any(p.search(html) for p in _DATA_BLOB_PATTERNS)


def _looks_like_challenge(raw_html: str) -> bool:
    """True if the *raw* HTML carries a bot-challenge fingerprint (AWS WAF,
    Cloudflare, DataDome, …). These sit in scripts stripped by
    ``html_to_text``, so they need a raw-HTML scan the visible-text
    heuristic can't do."""
    low = raw_html.lower()
    return any(fp in low for fp in _CHALLENGE_FINGERPRINTS)


def _looks_like_js_shell(html: str, text: str) -> bool:
    """Detect a response whose visible content needs JS to render."""
    if _EMPTY_MOUNT_RE.search(html):
        return True
    if len(text.strip()) >= 1500:
        return False
    return any(m in html for m in _JS_HYDRATION_MARKERS)


# ---------------------------------------------------------------------------
# Low-level fetch helpers
# ---------------------------------------------------------------------------

async def _render_page(browser: object, url: str) -> str:
    """Open ``url`` in a fresh context and return its rendered HTML.

    Split out of :func:`_fetch_with_playwright` so the whole post-launch page
    lifecycle can be wrapped in a single wall-clock bound; a wedged
    ``new_context``/``new_page``/``content`` await would otherwise hang forever.
    """
    context = await browser.new_context(  # type: ignore[attr-defined]
        user_agent=_PLAYWRIGHT_UA,
        viewport={"width": 1280, "height": 800},
        locale="en-GB",
        timezone_id="Europe/London",
        java_script_enabled=True,
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log.warning("playwright goto soft-failed for %s: %s", url, e)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(2500)
    return await page.content()


async def _fetch_with_playwright(url: str) -> str:
    """Fetch a page using patchright (CDP-evasion drop-in for Playwright).

    patchright is an API-compatible fork of Playwright that runs page scripts
    in isolated ExecutionContexts instead of calling Runtime.enable, which is
    the CDP signal DataDome and Cloudflare key on. It also strips
    --enable-automation at launch and patches navigator.webdriver / plugins /
    chrome runtime at the C++ level — so the manual add_init_script we used
    to ship would have *forced* patchright back into Runtime.enable mode,
    defeating the whole point. Don't add one back.

    Every phase (launch, render, close) is wall-clock bounded: under memory
    pressure on the prod droplet an unbounded ``chromium.launch()`` / ``close()``
    await wedged forever and froze the event loop (and with it the API + the
    routine scheduler) for days on 2026-08-26. A stalled phase now raises
    ``TimeoutError`` instead — a leaked chromium is far cheaper than a dead loop.
    """
    try:
        p = await start_driver()
        try:
            browser = await asyncio.wait_for(
                p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                    timeout=_BROWSER_LAUNCH_MS,
                ),
                timeout=_BROWSER_LAUNCH_S,
            )
            try:
                return await asyncio.wait_for(_render_page(browser, url), timeout=_BROWSER_WORK_S)
            finally:
                # close() can itself hang on a wedged browser — bound it so a
                # stuck teardown never blocks the caller.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(browser.close(), timeout=_BROWSER_CLOSE_S)
        finally:
            await stop_driver(p)
    except Exception as e:
        log.warning("playwright fetch failed for %s: %s", url, e)
        raise


async def _fetch_with_curl(url: str) -> tuple[str, int, str]:
    """Fetch with curl_cffi. Returns (raw_html, status_code, final_url)."""
    loop = asyncio.get_event_loop()
    resp = await loop.run_in_executor(
        None, lambda: curl_requests.get(url, impersonate="chrome", timeout=15, allow_redirects=True)
    )
    resp.raise_for_status()
    text = html_to_text(resp.text)
    if _looks_like_bot_page(text):
        raise ValueError("bot page detected")
    if _looks_like_js_shell(resp.text, text):
        if _has_embedded_data(resp.text):
            log.info(
                "web_fetch: accepting js-shell HTML with embedded data blob (%d bytes)",
                len(resp.text),
            )
        else:
            raise ValueError("js shell detected")
    return resp.text, resp.status_code, str(resp.url)


# ---------------------------------------------------------------------------
# fetch() — top-level orchestration, broken into focused helpers
# ---------------------------------------------------------------------------


def _unwrap_task_result(result: object, fallback_url: str) -> tuple[str, int, str]:
    """Normalise a curl/playwright task result into ``(html, status, final_url)``."""
    if isinstance(result, tuple):
        html, status, final = result
        return html, status, final
    return str(result), 200, fallback_url  # playwright path doesn't expose status


def _accept_or_log(
    html: str, source: str, where: str = ""
) -> bool:
    """Return True if ``html`` is a usable page; log otherwise."""
    text = html_to_text(html) if html else ""
    # A challenge shell (AWS WAF, Cloudflare, …) has real markup but almost no
    # visible text — reject it so the caller can fall through to the unblocker.
    challenged = bool(html) and _looks_like_challenge(html) and len(text.strip()) < 1500
    if html and text and not _looks_like_bot_page(text) and not challenged:
        log.info("web_fetch: %s won%s (%d bytes)", source, f" {where}" if where else "", len(html))
        return True
    reason = "challenge shell" if challenged else "bot/empty page"
    log.info(
        "web_fetch: %s returned but looked like %s%s (rendered=%d)",
        source, reason, f" {where}" if where else "", len(text),
    )
    return False


async def fetch_via_unblocker(url: str) -> str | None:
    """Last-resort fetch through a configured render-and-solve proxy.

    Only reached after both curl and the browser hit a bot wall, so it stays
    free and a no-op until configured. Set ``TELOS_UNBLOCKER_URL`` to a
    provider template containing a literal ``{url}`` placeholder — the target
    is URL-encoded and substituted in, and the provider's own API key lives
    in the template. Works with any GET-style unblocker (ScrapingBee,
    ScraperAPI, ZenRows, ScrapingAnt, …), e.g.::

        TELOS_UNBLOCKER_URL='https://app.scrapingbee.com/api/v1/?api_key=KEY&render_js=true&premium_proxy=true&url={url}'

    Returns rendered HTML, or ``None`` when unconfigured or the proxy itself
    returns an error / bot page.
    """
    template = os.environ.get("TELOS_UNBLOCKER_URL", "").strip()
    if "{url}" not in template:
        return None
    try:
        timeout = int(os.environ.get("TELOS_UNBLOCKER_TIMEOUT", "75"))
    except ValueError:
        timeout = 75
    proxied = template.replace("{url}", quote(url, safe=""))
    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None, lambda: curl_requests.get(proxied, timeout=timeout, allow_redirects=True)
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("web_fetch: unblocker request failed for %s: %s", url, e)
        return None
    html = resp.text
    if not html or _looks_like_bot_page(html_to_text(html)):
        log.info("web_fetch: unblocker returned bot/empty page for %s (%d bytes)", url, len(html or ""))
        return None
    log.info("web_fetch: unblocker won for %s (%d bytes)", url, len(html))
    return html


async def _race_curl_vs_playwright(url: str) -> tuple[str, int, str]:
    """Run both fetchers concurrently; return the first usable result.

    Cancels the loser as soon as a usable result is in hand. Falls back to
    the second runner if the first finishes but produces a bot/empty page.
    Raises ``RuntimeError`` if both fail.
    """
    curl_task = asyncio.create_task(_fetch_with_curl(url), name="curl")
    playwright_task = asyncio.create_task(_fetch_with_playwright(url), name="playwright")

    done, pending = await asyncio.wait(
        [curl_task, playwright_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # First completed: accept if usable, otherwise fall through to ``pending``.
    for task in done:
        try:
            html, status, final = _unwrap_task_result(task.result(), url)
        except Exception as e:
            log.info("web_fetch: %s failed: %s", task.get_name(), e)
            continue
        if _accept_or_log(html, task.get_name()):
            for p in pending:
                p.cancel()
            return html, status, final

    # First task didn't yield a usable result — wait for the other.
    for task in pending:
        try:
            html, status, final = _unwrap_task_result(await task, url)
        except Exception as e:
            log.info("web_fetch: %s fallback failed: %s", task.get_name(), e)
            continue
        if _accept_or_log(html, task.get_name(), where="on fallback"):
            return html, status, final

    # Both free paths hit a wall — try the configured unblocker (no-op if unset).
    unblocked = await fetch_via_unblocker(url)
    if unblocked is not None:
        return unblocked, 200, url

    raise RuntimeError(f"Could not fetch {url} (both curl and browser failed)")


def _enrich_known_site(url: str, html: str) -> dict | None:
    for site in (rightmove, zoopla, onthemarket, agencies):
        result = site.enrich(url, html)
        if result is not None:
            return result
    return None


async def _collect_images(html: str, base_url: str, *, limit: int = 3) -> list[str]:
    """Download up to ``limit`` images from ``html`` and return them as data-URIs."""
    image_urls = extract_images_from_html(html, base_url)
    if not image_urls:
        return []
    results = await asyncio.gather(
        *(download_and_resize_image(u) for u in image_urls[:limit]),
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, str) and r]


async def fetch(url: str) -> FetchResult:
    """Fetch ``url`` and return its rendered text, inline images, and HTTP metadata."""
    log.info("web_fetch start: %s", url)
    html, status_code, final_url = await _race_curl_vs_playwright(url)
    try:
        structured = _enrich_known_site(final_url, html)
    except Exception:
        log.exception("web_fetch: structured extraction failed for %s", final_url)
        structured = None
    text = html_to_text(html)
    if structured is not None:
        text = f"[{structured['kind']}] {structured['summary']}\n{_json.dumps(structured['data'], ensure_ascii=False)}\n\n---\n{text}"
    images = await _collect_images(html, final_url)
    result: FetchResult = {
        "text": text,
        "images": images,
        "status_code": status_code,
        "final_url": final_url,
    }
    if structured is not None:
        result["structured"] = structured
    return result


async def fetch_raw(url: str) -> str:
    """Rendered HTML only (no enrichment/images) — for the agency search
    implementations, which do their own parsing."""
    html, _status, _final = await _race_curl_vs_playwright(url)
    return html


async def fetch_rendered(url: str) -> str:
    """JS-rendered HTML only — no curl race. For pages whose *server* HTML
    is valid but incomplete (e.g. Dexters search cards get their prices
    injected client-side): the race would accept curl's price-less HTML, so
    callers that need the hydrated DOM must force the browser path."""
    return await _fetch_with_playwright(url)


async def fetch_json(url: str) -> object:
    """GET a JSON endpoint (an agency's internal API), rendering via the
    browser so anti-bot 403s are bypassed. Handles both raw JSON and the
    ``<pre>``-wrapped body Playwright returns for a JSON response."""
    body, _status, _final = await _race_curl_vs_playwright(url)
    try:
        return _json.loads(body)
    except (ValueError, TypeError):
        m = re.search(r"(\{.*\}|\[.*\])", body, re.DOTALL)
        if not m:
            return None
        try:
            return _json.loads(m.group(1))
        except (ValueError, TypeError):
            return None
