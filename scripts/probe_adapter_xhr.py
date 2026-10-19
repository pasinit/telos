"""Capture an estate agent's internal listings JSON API (XHR/fetch).

Agency search pages don't server-render list-level price/beds — those
hydrate from the site's OWN internal JSON API. To build the "rich search
in one call" path (option 2 — see docs/real_estate_agents_xhr.md), you
first need each agency's API endpoint, params and response shape. This
script renders a search page with patchright and logs every XHR/fetch
JSON response that looks like listings, so you can spot the endpoint.

Run ON THE DEPLOYMENT (needs patchright + a browser):

    docker compose exec -T telos python scripts/probe_adapter_xhr.py \
        "https://www.foxtons.co.uk/properties-to-rent/marylebone"

Then look for the response whose URL is the site's own API (not GTM /
analytics / consent), with a large body containing a results array.
"""
import asyncio
import sys

from patchright.async_api import async_playwright

_LISTING_HINTS = ("bedroom", "price", "listing", "property")
_NOISE_HOSTS = ("varify.io", "googletagmanager", "google-analytics", "doubleclick", "hotjar", "cookielaw")


async def capture(url: str, wait_ms: int = 6000) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []

    async def on_response(resp) -> None:
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            if "json" not in resp.headers.get("content-type", ""):
                return
            if any(h in resp.url for h in _NOISE_HOSTS):
                return
            body = await resp.text()
            if len(body) > 500 and any(k in body.lower() for k in _LISTING_HINTS):
                hits.append((resp.url, len(body), body[:240].replace("\n", " ")))
        except Exception:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await (await browser.new_context()).new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(wait_ms)
        finally:
            await browser.close()
    return hits


async def _main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for url, size, sample in await capture(sys.argv[1]):
        print(f"\nAPI  {url}\n     bytes={size}\n     sample={sample}")


if __name__ == "__main__":
    asyncio.run(_main())
