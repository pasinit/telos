"""``browser.open`` / ``browser.click`` read operations (headless patchright Chromium).

NEEDS THE CONFINED-WORKER PROFILE. Each call launches its own Chromium
(hundreds of MB); Telos's browser tools OOM-killed services on the 1.9 GB host.
Requires the ``browser`` extra plus ``patchright install chromium``.

Telos's ``BrowserSession`` keeps one page alive across a turn. Cardea operations
are independent receipted calls, so these are stateless: ``browser.click``
re-opens ``url`` and then clicks ``target`` on it. Cookies, form state, popups
and downloads (PDF extraction) do not carry over and are not supported.

Read-only at the network level: service workers are blocked, every request other
than GET/HEAD is aborted (except Cloudflare's challenge endpoints, which a visitor
must POST to), WebSockets never reach a server, and ``browser.click`` refuses the
submit controls of POST forms.

Inside Cardea's confined worker there is no resolver: ``HTTPS_PROXY`` names the
host-owner proxy, which resolves names and enforces the hostname allow-list.
There, URLs are checked syntactically and Chromium is pointed at the proxy.
"""

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
from typing import Any
from urllib.parse import urlparse

from telos_cardea._cardea import Arguments, OperationContext, OperationRejected, Output
from telos_cardea._vendor import agencies, rightmove, zoopla

log = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_CONTENT_CHARS = 12000
MAX_INTERACTIVE = 30
NAV_TIMEOUT_MS = 30000
LAUNCH_S = 35.0
WORK_S = 75.0
CHALLENGE_WAIT_S = 25.0
CHALLENGE_POLL_S = 1.0
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

_CHALLENGE_DONE_JS = """
() => !/just a moment|attention required/i.test(document.title)
  && !document.querySelector('#challenge-form, #challenge-running, #challenge-stage, #cf-challenge-running')
"""

_SUBMIT_FACTS_JS = """
el => {
  const form = el.form || el.closest('form');
  return {
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : null,
    override: (el.getAttribute('formmethod') || '').toLowerCase(),
  };
}
"""


# Names that never resolve to a public site.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
READ_METHODS = frozenset({"GET", "HEAD"})
# A Cloudflare managed challenge POSTs its answer to these; nothing else may write.
CHALLENGE_HOSTS = frozenset({"challenges.cloudflare.com"})
CHALLENGE_PATH = "/cdn-cgi/challenge-platform/"


def egress_proxy() -> str | None:
    """The confined worker's egress proxy, when there is one (Cardea sets both spellings)."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    try:  # Shorthand IPv4 that browsers accept: 127.1, 0x7f.0.0.1, 2130706433.
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def _private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def private_host(host: str) -> bool:
    """Syntactic check, no DNS: a literal private IP, localhost, or a local-only name."""
    host = host.lower().rstrip(".")
    ip = _literal_ip(host)
    if ip is not None:
        return _private_ip(ip)
    return host == "localhost" or "." not in host or host.endswith(_PRIVATE_SUFFIXES)


def is_private_url(url: str) -> bool:
    """True if the URL is not http(s) or points at a private, loopback, link-local or reserved address.

    Without a proxy the hostname is resolved, as in Telos. Behind the confined worker's proxy
    there is no resolver and the proxy's allow-list governs names, so only the syntax is checked.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return True
        if private_host(parsed.hostname):
            return True
        if egress_proxy():
            return False
        for _family, _, _, _, sockaddr in socket.getaddrinfo(parsed.hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            if _private_ip(ipaddress.ip_address(sockaddr[0])):
                return True
    except Exception:
        return True
    return False


def allow_request(method: str, url: str) -> bool:
    """Network guard for every request the page makes: reads only, never to a private host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return parsed.scheme in ("data", "blob")
    host = (parsed.hostname or "").lower()
    if not host or private_host(host):
        return False
    if method.upper() in READ_METHODS:
        return True
    return host in CHALLENGE_HOSTS or parsed.path.startswith(CHALLENGE_PATH)


def is_challenge(status: int, headers: dict[str, str]) -> bool:
    """A Cloudflare managed challenge (interstitial), which may clear itself, rather than a refusal."""
    return status in (403, 429, 503) and any(k.lower() == "cf-mitigated" and v.lower() == "challenge" for k, v in headers.items())


def is_post_submit(facts: dict[str, Any]) -> bool:
    """Whether clicking the described element would submit a form by POST (facts from ``_SUBMIT_FACTS_JS``)."""
    tag, kind = str(facts.get("tag", "")), str(facts.get("type", ""))
    submits = (tag == "button" and kind in ("", "submit")) or (tag == "input" and kind in ("submit", "image"))
    if not submits or facts.get("form_method") is None:
        return False
    return str(facts.get("override") or facts["form_method"]).lower() == "post"


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


async def _guard(route: Any) -> None:
    request = route.request
    if allow_request(request.method, request.url):
        await route.continue_()
    else:
        log.info("blocked %s %s", request.method, urlparse(request.url).hostname)
        await route.abort("blockedbyclient")


async def _refuse_websocket(ws: Any) -> None:
    # Not connecting to the server leaves the page talking to nothing.
    await ws.close()


def launch_options() -> dict[str, Any]:
    options: dict[str, Any] = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"], "timeout": int(LAUNCH_S * 1000) - 5000}
    proxy = egress_proxy()
    if proxy:
        options["proxy"] = {"server": proxy}
    return options


async def new_read_only_context(browser: Any) -> Any:
    """A browser context whose network can only read: no service workers, writes or WebSockets."""
    context = await browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800}, locale="en-GB",
                                        timezone_id="Europe/London", service_workers="block")
    await context.route("**/*", _guard)
    route_web_socket = getattr(context, "route_web_socket", None)
    if route_web_socket is not None:
        await route_web_socket("**/*", _refuse_websocket)
    return context


async def _with_page(url: str, action: Any) -> Any:
    try:
        from patchright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError("browser operations need the telos-cardea[browser] extra") from error
    async with async_playwright() as pw:
        browser = await asyncio.wait_for(pw.chromium.launch(**launch_options()), timeout=LAUNCH_S)
        try:
            async def work() -> Any:
                context = await new_read_only_context(browser)
                page = await context.new_page()
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                except Exception as error:
                    # Behind the proxy, a host outside the allow-list fails here (ERR_TUNNEL_CONNECTION_FAILED).
                    detail = str(error).strip().splitlines()[0][:200] if str(error).strip() else type(error).__name__
                    raise OperationRejected(f"could not open {url}: {detail}") from None
                if response is not None and response.status >= 400:
                    if is_challenge(response.status, response.headers):
                        await _pass_challenge(page, url)
                    else:
                        raise OperationRejected(f"HTTP {response.status} opening {url}; access denied or unavailable, not empty results")
                await _settle(page)
                return await action(page)

            return await asyncio.wait_for(work(), timeout=WORK_S)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=CLOSE_S)


async def _pass_challenge(page: Any, url: str) -> None:
    """Give a Cloudflare challenge time to solve and navigate to the page; reject if it does not."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHALLENGE_WAIT_S
    while loop.time() < deadline:
        await asyncio.sleep(CHALLENGE_POLL_S)
        with contextlib.suppress(Exception):  # The challenge navigates, destroying the evaluation context.
            if await page.evaluate(_CHALLENGE_DONE_JS):
                return
    raise OperationRejected(f"Cloudflare challenge at {url} was not passed; access unavailable, not empty results")


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
    """Role + name, then visible text, then CSS selector (Telos's matcher order); never a POST submit."""
    candidates = [page.get_by_role(role, name=target, exact=False) for role in ("link", "button", "menuitem", "tab")]
    candidates.append(page.get_by_text(target, exact=False))
    candidates.append(page.locator(target))
    refused = False
    for locator in candidates:
        with contextlib.suppress(Exception):
            for candidate in await locator.all():
                if not (await candidate.is_visible() and await candidate.is_enabled()):
                    continue
                if is_post_submit(await candidate.evaluate(_SUBMIT_FACTS_JS)):
                    refused = True
                    continue
                return candidate
    if refused:
        raise OperationRejected(f"{target!r} submits a POST form; browser operations are read-only")
    return None
