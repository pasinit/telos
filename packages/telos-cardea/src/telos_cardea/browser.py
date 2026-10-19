"""``browser.open`` / ``browser.click`` read operations (headless patchright Chromium).

NEEDS THE CONFINED-WORKER PROFILE. Each call launches its own Chromium
(hundreds of MB); Telos's browser tools OOM-killed services on the 1.9 GB host.
Requires the ``browser`` extra plus ``patchright install chromium``.

Telos's ``BrowserSession`` keeps one page alive across a turn. Cardea operations
are independent receipted calls, so these are stateless: ``browser.click``
re-opens ``url`` and then clicks ``target`` on it. Cookies, form state, popups
and downloads (PDF extraction) do not carry over and are not supported.
"""

import asyncio
import contextlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from telos_cardea._cardea import Arguments, OperationContext, OperationRejected, Output
from telos_cardea._vendor import agencies, rightmove, zoopla

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_CONTENT_CHARS = 12000
MAX_INTERACTIVE = 30
NAV_TIMEOUT_MS = 30000
LAUNCH_S = 35.0
WORK_S = 60.0
CLOSE_S = 10.0

_COLLECT_JS = """
() => {
  const out = []; const seen = new Set();
  const push = (el, kind) => {
    if (!el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) || el.disabled || el.closest('[inert]')) return;
    let text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
    text = text.replace(/\\s+/g, ' ').slice(0, 120);
    if (!text) return;
    const href = kind === 'link' ? el.href : undefined;
    const key = kind + '|' + text.toLowerCase() + '|' + (href || '');
    if (seen.has(key)) return; seen.add(key);
    out.push({kind, text, ...(href ? {href} : {})});
  };
  const main = document.querySelector('main, [role="main"], #maincontent');
  if (main) main.querySelectorAll('a[href]').forEach(el => push(el, 'link'));
  document.querySelectorAll('button, input[type="button"], input[type="submit"]').forEach(el => push(el, 'button'));
  document.querySelectorAll('a[href]').forEach(el => push(el, 'link'));
  return out;
}
"""


def is_private_url(url: str) -> bool:
    """True if the URL is not http(s) or resolves to a private, loopback, link-local or reserved IP (from Telos)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return True
        for _family, _, _, _, sockaddr in socket.getaddrinfo(parsed.hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except Exception:
        return True
    return False


def _structured(url: str, html: str) -> dict[str, Any] | None:
    for site in (rightmove, zoopla, agencies):
        try:
            result = site.enrich(url, html)
        except Exception:
            continue
        if result is not None:
            return dict(result)
    return None


def _text(html: str) -> str:
    from telos_cardea.fetch import visible_text

    return visible_text(html)


async def _with_page(url: str, action: Any) -> Any:
    try:
        from patchright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError("browser operations need the telos-cardea[browser] extra") from error
    async with async_playwright() as pw:
        browser = await asyncio.wait_for(
            pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"], timeout=int(LAUNCH_S * 1000) - 5000),
            timeout=LAUNCH_S,
        )
        try:
            async def work() -> Any:
                context = await browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800}, locale="en-GB",
                                                    timezone_id="Europe/London")
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                if response is not None and response.status >= 400:
                    raise OperationRejected(f"HTTP {response.status} opening {url}; access denied or unavailable, not empty results")
                await _settle(page)
                return await action(page)

            return await asyncio.wait_for(work(), timeout=WORK_S)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=CLOSE_S)


async def _settle(page: Any) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=6000)
    await page.wait_for_timeout(1500)


async def _capture(page: Any, offset: int = 0) -> Output:
    html = await page.content()
    content = _text(html)
    structured = _structured(page.url, html)
    try:
        interactive = (await page.evaluate(_COLLECT_JS))[:MAX_INTERACTIVE]
    except Exception:
        interactive = []
    end = min(offset + MAX_CONTENT_CHARS, len(content))
    out: Output = {
        "url": page.url,
        "title": await page.title(),
        "content": content[offset:end],
        "interactive": [{k: str(v) for k, v in item.items() if k in ("kind", "text", "href")} for item in interactive],
        "total_chars": len(content),
        "next_offset": end if end < len(content) else None,
    }
    if structured is not None:
        out["structured"] = structured
    return out


async def render(url: str) -> str:
    """Rendered HTML of ``url`` (the listing fetcher's opt-in browser fallback)."""
    if is_private_url(url):
        raise ValueError("Blocked: private/internal URL")

    async def html(page: Any) -> str:
        return str(await page.content())

    return str(await _with_page(url, html))


async def open_page(arguments: Arguments, context: OperationContext) -> Output:
    """Entry point ``browser.open``."""
    url = str(arguments["url"])
    offset = int(arguments.get("offset", 0))
    if is_private_url(url):
        raise OperationRejected("Blocked: cannot open private/internal URLs")

    async def capture(page: Any) -> Output:
        return await _capture(page, offset)

    result: Output = await _with_page(url, capture)
    return result


async def click(arguments: Arguments, context: OperationContext) -> Output:
    """Entry point ``browser.click``: open ``url``, click ``target``, return the resulting page."""
    url = str(arguments["url"])
    target = str(arguments["target"])
    if is_private_url(url):
        raise OperationRejected("Blocked: cannot open private/internal URLs")

    async def do_click(page: Any) -> Output:
        locator = await _resolve_clickable(page, target)
        if locator is None:
            raise OperationRejected(f"could not find clickable element matching {target!r}")
        await locator.click(timeout=10000)
        await _settle(page)
        if is_private_url(page.url):
            raise OperationRejected("Blocked: click led to a private/internal URL")
        return await _capture(page)

    result: Output = await _with_page(url, do_click)
    return result


async def _resolve_clickable(page: Any, target: str) -> Any:
    """Role + name, then visible text, then CSS selector (Telos's matcher order)."""
    candidates = [page.get_by_role(role, name=target, exact=False) for role in ("link", "button", "menuitem", "tab")]
    candidates.append(page.get_by_text(target, exact=False))
    candidates.append(page.locator(target))
    for locator in candidates:
        with contextlib.suppress(Exception):
            for candidate in await locator.all():
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
    return None
