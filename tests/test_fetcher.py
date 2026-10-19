"""Tests for the web fetcher's unblocker fallback and challenge detection."""
import pytest

from telos.transport.browser import fetcher


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


@pytest.fixture
def fake_curl(monkeypatch):
    """Record the URL curl_cffi is asked for and return a canned body."""
    calls: list[str] = []

    def _make(body: str):
        def _get(url, *a, **k):
            calls.append(url)
            return _FakeResp(body)

        monkeypatch.setattr(fetcher.curl_requests, "get", _get)

    return calls, _make


def test_challenge_fingerprints_detected():
    assert fetcher._looks_like_challenge('<script src="https://x.token.awswaf.com/y/mp_verify"></script>')
    assert fetcher._looks_like_challenge('<div id="cf">/cdn-cgi/challenge-platform/h/b/orchestrate</div>')
    assert not fetcher._looks_like_challenge("<html><body>3 bedroom flat in W2, £3,000 pcm</body></html>")


async def test_unblocker_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELOS_UNBLOCKER_URL", raising=False)
    assert await fetcher.fetch_via_unblocker("https://www.openrent.co.uk/x") is None


async def test_unblocker_substitutes_url_and_returns_html(monkeypatch, fake_curl):
    calls, make = fake_curl
    make(
        "<html><body>"
        "<h1>Properties to rent in W2</h1>"
        "<article>3 bedroom flat in Bayswater, London W2 — £3,000 pcm. "
        "Spacious period conversion with two bathrooms, a large reception room, "
        "and a private garden. Available now, unfurnished, minimum 12-month tenancy.</article>"
        "</body></html>"
    )
    monkeypatch.setenv(
        "TELOS_UNBLOCKER_URL",
        "https://api.example.com/?api_key=SECRET&render_js=true&url={url}",
    )
    out = await fetcher.fetch_via_unblocker("https://www.openrent.co.uk/properties-to-rent/w2?a=1")
    assert out is not None and "3 bedroom flat" in out
    # target URL-encoded and substituted into the template (no bare '{url}' left)
    assert len(calls) == 1
    assert "{url}" not in calls[0]
    assert "https%3A%2F%2Fwww.openrent.co.uk%2Fproperties-to-rent%2Fw2%3Fa%3D1" in calls[0]
    assert "api_key=SECRET" in calls[0]


async def test_unblocker_rejects_returned_bot_page(monkeypatch, fake_curl):
    _calls, make = fake_curl
    make("<html><body>Just a moment... please enable JavaScript to continue.</body></html>")
    monkeypatch.setenv("TELOS_UNBLOCKER_URL", "https://api.example.com/?url={url}")
    assert await fetcher.fetch_via_unblocker("https://www.marshandparsons.co.uk/x") is None


async def test_unblocker_returns_none_on_request_error(monkeypatch):
    def _boom(url, *a, **k):
        raise RuntimeError("proxy 500")

    monkeypatch.setattr(fetcher.curl_requests, "get", _boom)
    monkeypatch.setenv("TELOS_UNBLOCKER_URL", "https://api.example.com/?url={url}")
    assert await fetcher.fetch_via_unblocker("https://www.openrent.co.uk/x") is None
