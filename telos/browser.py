"""Stateful browser session for the agent.

A `BrowserSession` is created at the start of an agent turn and torn down at
the end. While it lives it holds a single patchright Chromium + page open,
so the agent can sequence `browser_open` → `browser_click` → `browser_read`
calls and have each one operate on the same DOM, with cookies and any
DataDome challenge tokens preserved across clicks.

Why per-turn: a turn usually contains multiple tool calls, so the agent
benefits from a stable browser. Across turns we'd have to manage idle
cleanup, memory pressure, and stale-state recovery — those costs aren't
worth it for the typical use case (paginate, click into listings, summarise
within one user message).

All methods return a uniform dict:
  {ok: bool, url: str, title: str, content: str, interactive: list,
   images: list[str], error: str}

The agent sees `content` (rendered text, capped) and `interactive` (top-N
links and buttons) and uses the visible text as the click/fill target. The
matcher tries get_by_role → get_by_text → CSS selector, in that order.
``images`` carries up to a few base64 data URIs extracted from the page so
the model can see photos alongside the text.
"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from patchright.async_api import Download, Error, Page, Response, async_playwright

from telos.transport.browser import agencies, onthemarket, rightmove, zoopla
from telos.transport.browser.documents import MAX_DOCUMENT_CHARS, extract_document
from telos.transport.browser.driver import start_driver
from telos.transport.browser.images import (
    download_and_resize_image,
    extract_images_from_html,
    html_to_text,
)

log = logging.getLogger("telos.browser")

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_MAX_CONTENT_CHARS = 12000
_MAX_INTERACTIVE_ITEMS = 30
_NAV_TIMEOUT_MS = 30000
_NETWORKIDLE_TIMEOUT_MS = 6000
_TAIL_WAIT_MS = 1500

# Wall-clock bounds on the persistent browser session. Patchright's default
# launch timeout is 180000ms, and neither the launch nor the per-call page work
# (goto / content / title / evaluate / image-download) was bounded end-to-end —
# so a degraded backend let a single browser_* call hang for minutes and starve
# the whole run (prod 2026-10-16/17). ``_BROWSER_LAUNCH_MS`` is patchright's own
# launch timeout (milliseconds); the asyncio bounds guard the awaits themselves.
# Mirrors the hardening already on the sibling fetch() path in
# telos/transport/browser/fetcher.py.
_BROWSER_LAUNCH_MS = 30_000
_BROWSER_LAUNCH_S = 35.0
_BROWSER_WORK_S = 75.0


class BrowserSession:
    """One Chromium instance + page that survives across tool calls in a turn."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._crashed = False
        self._lock = asyncio.Lock()
        self._parents = []
        self._snapshot = None
        self._document = None

    async def _ensure_started(self) -> None:
        if self._crashed or (self._page is not None and self._page.is_closed()):
            await self.close()
        if self._page is not None:
            return

        self._pw = await start_driver(async_playwright)

        async def _boot() -> None:
            # Pass patchright an explicit launch timeout (its default is
            # 180000ms — the exact '180000ms exceeded' wedge seen in prod) and
            # bound the whole startup (launch, context, first page) so a
            # degraded backend can't hang the run before any navigation begins.
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                timeout=_BROWSER_LAUNCH_MS,
            )
            self._context = await self._browser.new_context(
                user_agent=_UA,
                viewport={"width": 1280, "height": 800},
                locale="en-GB",
                timezone_id="Europe/London",
                java_script_enabled=True,
            )
            self._page = await self._context.new_page()
            self._page.on("crash", self._on_page_crash)

        await asyncio.wait_for(_boot(), timeout=_BROWSER_LAUNCH_S)
        log.info("browser session: started")

    def _on_page_crash(self, page: Any) -> None:
        self._crashed = True

    async def open(self, url: str) -> dict[str, Any]:
        async with self._lock:
            try:
                await self._ensure_started()
                return await self._bounded(self._navigate(lambda: self._page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)))
            except Exception as e:
                self._crashed = True
                log.warning("browser_open failed for %s: %s", url, e)
                return self._error(f"open failed: {e}")

    async def click(self, target: str) -> dict[str, Any]:
        async with self._lock:
            if self._page is None:
                return self._error("no page open — call browser_open first")
            try:
                await self._dismiss_site_overlay()
                locator = await self._resolve_clickable(target)
                if locator is None:
                    return self._error(f"could not find clickable element matching {target!r}")
                return await self._bounded(self._navigate(lambda: locator.click(timeout=10000)))
            except Exception as e:
                log.warning("browser_click failed for %r: %s", target, e)
                return self._error(f"click failed: {e}")

    async def fill(self, target: str, value: str) -> dict[str, Any]:
        async with self._lock:
            if self._page is None:
                return self._error("no page open — call browser_open first")
            try:
                locator = await self._resolve_input(target)
                if locator is None:
                    return self._error(f"could not find input matching {target!r}")
                try:
                    await locator.fill(value, timeout=5000)
                except Exception as e:
                    return self._error(f"fill failed: {e}")
                await self._settle()
                self._document = None
                return await self._bounded(self._capture())
            except Exception as e:
                log.warning("browser_fill failed for %r: %s", target, e)
                return self._error(f"fill failed: {e}")

    async def read(self, offset: int = 0) -> dict[str, Any]:
        async with self._lock:
            if self._page is None:
                return self._error("no page open — call browser_open first")
            try:
                if offset < 0:
                    return self._error("offset must be nonnegative")
                if self._document is not None or (offset and self._snapshot is not None):
                    return self._content_page(offset)
                await self._settle(short=True)
                await self._bounded(self._capture())
                return self._content_page(offset)
            except Exception as e:
                return self._error(f"read failed: {e}")

    async def back(self) -> dict[str, Any]:
        async with self._lock:
            if self._page is None:
                return self._error("no page open — call browser_open first")
            try:
                if self._document is not None:
                    self._document = None
                    return await self._bounded(self._capture())
                return await self._bounded(self._navigate(self._go_back))
            except Exception as e:
                return self._error(f"back failed: {e}")

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await asyncio.wait_for(self._browser.close(), timeout=10)
        except Exception:
            pass
        try:
            if self._pw is not None:
                await asyncio.wait_for(self._pw.stop(), timeout=10)
        except Exception:
            pass
        self._page = self._context = self._browser = self._pw = None
        self._crashed = False
        self._parents.clear()
        self._snapshot = self._document = None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _bounded(self, coro: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        """Wall-clock bound one browser operation's page work.

        goto/content/title/evaluate/image-download each carry their own
        per-step timeouts, but nothing bounded a call end-to-end — so a
        degraded backend could wedge a single browser_* call for minutes and
        burn the whole run budget. A timed-out op leaves the session in an
        unknown state, so mark it crashed and let the next call rebuild.
        """
        try:
            return await asyncio.wait_for(coro, timeout=_BROWSER_WORK_S)
        except TimeoutError:
            self._crashed = True
            raise TimeoutError(f"operation exceeded {_BROWSER_WORK_S:.0f}s wall-clock budget") from None

    async def _go_back(self) -> Response | None:
        if self._parents:
            # go_back also returns None for same-document history entries.
            # Inspect the actual index before deciding to close a child tab.
            session = await self._context.new_cdp_session(self._page)
            try:
                history = await session.send("Page.getNavigationHistory")
            finally:
                await session.detach()
            if history["currentIndex"] == 0:
                await self._page.close()
                self._page = self._parents.pop()
                return None
        return await self._page.go_back(wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)

    async def _read_download(self, download: Download) -> dict[str, Any]:
        try:
            path = await asyncio.wait_for(download.path(), timeout=30)
            if path is None:
                raise ValueError("Download did not produce a document")
            text = await extract_document(Path(path))
            self._document = {"ok": True, "url": download.url, "title": download.suggested_filename,
                              "content": text, "interactive": [], "images": [], "error": ""}
            self._snapshot = self._document
            return self._content_page(0)
        finally:
            with suppress(Error):
                await download.cancel()
                await download.delete()

    async def _navigate(self, action: Callable[[], Awaitable[Response | None]]) -> dict[str, Any]:
        """Observe navigation, popup and download events for this action only."""
        origin = self._page
        downloads = []
        download_ready = asyncio.Event()
        popups = []
        responses = []
        self._document = self._snapshot = None

        def on_download(download: Download):
            downloads.append(download)
            download_ready.set()

        def on_response(response: Response):
            responses.append(response)

        def on_popup(page: Page):
            popups.append(page)
            page.on("download", on_download)

        origin.on("download", on_download)
        origin.on("popup", on_popup)
        self._context.on("response", on_response)
        try:
            try:
                response = await action()
            except Exception as action_error:
                # A download aborts Page.goto; its download event supplies the result.
                try:
                    await asyncio.wait_for(download_ready.wait(), timeout=1)
                except TimeoutError:
                    raise action_error from None
                response = None
            if response is not None and response.status >= 400:
                return self._error(f"HTTP {response.status} opening {response.url}; access denied or unavailable, not empty results")
            if not downloads:
                await self._settle()
            if popups:
                popup = popups[-1]
                if not popup.is_closed() and not downloads:
                    self._parents.append(origin)
                    self._page = popup
                    popup.on("crash", self._on_page_crash)
                    await popup.wait_for_load_state("domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                    await self._settle()
            if downloads:
                return await self._read_download(downloads[-1])
            for navigation in responses:
                if (navigation.request.is_navigation_request() and navigation.frame == self._page.main_frame
                        and navigation.status >= 400):
                    return self._error(f"HTTP {navigation.status} opening {navigation.url}; access denied or unavailable")
            await self._dismiss_site_overlay()
            return await self._capture()
        finally:
            origin.remove_listener("download", on_download)
            origin.remove_listener("popup", on_popup)
            self._context.remove_listener("response", on_response)
            for popup in popups:
                popup.remove_listener("download", on_download)
                if popup != self._page and not popup.is_closed():
                    await popup.close()

    def _content_page(self, offset: int) -> dict[str, Any]:
        snapshot = self._snapshot
        text = snapshot["content"]
        if offset > len(text):
            return self._error(f"offset exceeds content length {len(text)}")
        end = min(offset + _MAX_CONTENT_CHARS, len(text))
        return {**snapshot, "content": text[offset:end], "images": snapshot["images"] if offset == 0 else [],
                "pagination": {"offset": offset, "next_offset": end if end < len(text) else None,
                               "total_chars": len(text), "source_truncated": "[Content truncated;" in text}}

    async def _dismiss_site_overlay(self) -> None:
        """Use OTM's explicit essential-only control, never accept optional cookies."""
        if not onthemarket.matches(self._page.url):
            return
        button = self._page.locator('#ccc').get_by_role('button', name='Essential cookies only', exact=True)
        if await button.is_visible():
            await button.click(timeout=5000)
            await self._settle(short=True)

    async def _settle(self, short: bool = False) -> None:
        try:
            await self._page.wait_for_load_state(
                "networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS
            )
        except Exception:
            pass
        if not short:
            await self._page.wait_for_timeout(_TAIL_WAIT_MS)

    async def _capture(self) -> dict[str, Any]:
        url = self._page.url
        title = await self._page.title()
        html = await self._page.content()
        content = html_to_text(html, max_chars=MAX_DOCUMENT_CHARS)
        interactive = await self._collect_interactive()
        # Site-specific enrichment. Same payload
        # the fetcher's web_fetch path appends, so browser flows see the
        # structured projection too.
        try:
            result = _enrich_known_site(url, html)
            if result is not None:
                payload = json.dumps(result["data"], indent=2, ensure_ascii=False)
                content = f"[{result['kind']}] {result['summary']}\n{payload}\n\n---\n{content}"
        except Exception as e:
            log.debug("adapter enrich in BrowserSession failed: %s", e)
        # Site-specific image selection keeps floorplans ahead of gallery photos.
        images: list[str] = []
        try:
            image_urls = extract_images_from_html(html, url)
            if image_urls:
                results = await asyncio.gather(
                    *[download_and_resize_image(u) for u in image_urls[:3]],
                    return_exceptions=True,
                )
                images = [r for r in results if isinstance(r, str) and r]
        except Exception as e:
            log.debug("image extraction in BrowserSession failed: %s", e)
        self._snapshot = {
            "ok": True,
            "url": url,
            "title": title,
            "content": content,
            "interactive": interactive,
            "images": images,
            "error": "",
        }
        return self._content_page(0)

    async def _collect_interactive(self) -> list[dict[str, str]]:
        """Top-N links and buttons with their visible text."""
        try:
            items = await self._page.evaluate(
                """
                () => {
                  const out = [];
                  const seen = new Set();
                  const push = (el, kind) => {
                    if (!el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) || el.disabled || el.closest('[inert]')) return;
                    let text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') ||
                                Array.from(el.labels || []).map(label => label.innerText).join(' ') || '').trim();
                    text = text.replace(/\\s+/g, ' ').slice(0, 120);
                    if (!text) return;
                    const href = kind === 'link' ? el.href : undefined;
                    const key = kind + '|' + text.toLowerCase() + '|' + (href || '');
                    if (seen.has(key)) return;
                    seen.add(key);
                    out.push({kind, text, ...(href ? {href} : {})});
                  };
                  const main = document.querySelector('main, [role="main"], #maincontent');
                  if (main) main.querySelectorAll('a[href]').forEach(el => push(el, 'link'));
                  document.querySelectorAll('button, input[type="button"], input[type="submit"]').forEach(el => push(el, 'button'));
                  document.querySelectorAll('a[href]').forEach(el => push(el, 'link'));
                  document.querySelectorAll('input[type="text"], input[type="search"], input[type="email"], input:not([type]), textarea').forEach(el => push(el, 'input'));
                  return out;
                }
                """
            )
        except Exception as e:
            log.debug("collect_interactive failed: %s", e)
            return []
        return items[:_MAX_INTERACTIVE_ITEMS]

    async def _resolve_clickable(self, target: str):
        """Best-effort locator for a click target. Tries text-by-role,
        text-anywhere, then CSS selector."""
        page = self._page
        # 1. Visible role + name
        for role in ("link", "button", "menuitem", "tab"):
            try:
                loc = await self._visible(page.get_by_role(role, name=target, exact=False))
                if loc is not None:
                    return loc
            except Exception:
                pass
        # 2. Any element with this visible text
        try:
            loc = await self._visible(page.get_by_text(target, exact=False))
            if loc is not None:
                return loc
        except Exception:
            pass
        # 3. Treat target as a CSS selector
        try:
            loc = await self._visible(page.locator(target))
            if loc is not None:
                return loc
        except Exception:
            pass
        return None

    async def _resolve_input(self, target: str):
        page = self._page
        for fn in ("get_by_label", "get_by_placeholder"):
            try:
                loc = await self._visible(getattr(page, fn)(target, exact=False))
                if loc is not None:
                    return loc
            except Exception:
                pass
        try:
            loc = await self._visible(page.locator(target))
            if loc is not None:
                return loc
        except Exception:
            pass
        return None

    @staticmethod
    async def _visible(locator):
        for candidate in await locator.all():
            if await candidate.is_visible() and await candidate.is_enabled():
                if await candidate.evaluate("el => el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) && !el.closest('[inert]')"):
                    return candidate
        return None

    def _error(self, msg: str) -> dict[str, Any]:
        url = self._page.url if self._page is not None else ""
        return {
            "ok": False,
            "url": url,
            "title": "",
            "content": "",
            "interactive": [],
            "images": [],
            "error": msg,
        }


def _enrich_known_site(url: str, html: str) -> dict[str, Any] | None:
    for site in (rightmove, zoopla, onthemarket, agencies):
        result = site.enrich(url, html)
        if result is not None:
            return result
    return None
