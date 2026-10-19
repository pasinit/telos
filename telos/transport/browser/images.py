"""HTML→text and image extraction helpers shared by the agent and BrowserSession."""

import asyncio
import base64
import ipaddress
import logging
import re
import socket
from io import BytesIO
from urllib.parse import urljoin, urlparse

import html2text
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from PIL import Image

from telos.transport.browser import onthemarket

log = logging.getLogger("telos.transport.browser.images")

_ICON_PATTERNS = re.compile(
    r"favicon|logo|sprite|pixel|track|spacer|badge|icon|1x1|blank\.gif",
    re.IGNORECASE,
)


def html_to_text(html_content: str, max_chars: int = 12000) -> str:
    """Extract content before applying a visible, bounded truncation."""
    soup = BeautifulSoup(html_content, "html.parser")
    for node in soup.select('script, style, noscript, nav, footer, select, input, [role="navigation"], [role="banner"], [role="contentinfo"]'):
        node.decompose()
    # Some sites use an empty <main> as a modal backdrop outside the listing.
    main = next((node for node in soup.select('main, [role="main"]')
                 if not node.has_attr('hidden') and node.get('aria-hidden', '').lower() != 'true'
                 and node.get_text(strip=True)), None)
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True  # Images are returned separately, not repeated as long Markdown URLs.
    converter.ignore_links = False
    text = converter.handle(str(main if main is not None else soup))
    text = re.sub(r"([.!?])([A-Z])", r"\1 \2", text)
    if len(text) > max_chars:
        return text[:max_chars] + "\n[Content truncated; missing fields are not evidence of absence.]"
    return text


def is_private_url(url: str) -> bool:
    """Return True if the URL resolves to a private, loopback, or link-local IP."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if not hostname:
            return True
        addrs = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _family, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except Exception:
        return True  # block on resolution failure
    return False


def extract_images_from_html(html_content: str, base_url: str) -> list[str]:
    """Extract top meaningful image URLs from HTML."""
    site_images = onthemarket.image_urls(base_url, html_content)
    if site_images is not None:
        return site_images
    soup = BeautifulSoup(html_content, "html.parser")
    candidates: list[tuple[int, str]] = []

    for img in soup.find_all("img", src=True):
        src = img["src"]
        width = img.get("width", "")
        height = img.get("height", "")
        try:
            w = int(re.sub(r"[^0-9]", "", str(width))) if width else 0
            h = int(re.sub(r"[^0-9]", "", str(height))) if height else 0
        except ValueError:
            w, h = 0, 0
        if (w and w < 100) or (h and h < 100):
            continue
        if _ICON_PATTERNS.search(src):
            continue
        if src.startswith("data:") and len(src) < 500:
            continue

        if not src.startswith(("http://", "https://", "data:")):
            src = urljoin(base_url, src)

        area = w * h if w and h else 0
        candidates.append((area, src))

    candidates.sort(key=lambda x: -x[0])
    return [url for _, url in candidates[:5]]


async def download_and_resize_image(url: str) -> str | None:
    """Download an image, resize to max 512px, return as base64 data URI."""
    try:
        if url.startswith("data:"):
            _header, b64data = url.split(",", 1)
            img_bytes = base64.b64decode(b64data)
        else:
            if is_private_url(url):
                return None
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: curl_requests.get(url, impersonate="chrome", timeout=5, allow_redirects=True),
            )
            if resp.status_code != 200:
                return None
            img_bytes = resp.content

        img = Image.open(BytesIO(img_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        max_side = max(img.size)
        if max_side > 512:
            scale = 512 / max_side
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        log.debug("image download/resize failed for %s: %s", url[:100], e)
        return None
