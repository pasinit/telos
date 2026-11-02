"""BrowserSession must flag an empty/JS-unrendered capture, not return it as clean empty content.

Client-rendered career portals (notably BFI's bfijobsandopportunities.bfi.org.uk) load but
render ~0 chars of visible text; the agent then reported the source as checked with a negative
finding ('no vacancies visible'), overstating what an empty render established (prod 2026-10-28/
10-30/11-01). _capture now distinguishes an unhydrated SPA shell from a genuinely sparse page.
"""

from telos.browser import BrowserSession, looks_client_rendered


class _FakePage:
    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self._html = html

    async def title(self) -> str:
        return "BFI jobs and opportunities"

    async def content(self) -> str:
        return self._html

    async def evaluate(self, script: str) -> list:
        return []


def test_looks_client_rendered_detects_spa_shells_not_text_pages() -> None:
    assert looks_client_rendered('<html><body><div id="__next"></div></body></html>')
    assert looks_client_rendered('<html ng-app="portal"><body></body></html>')
    assert looks_client_rendered("<html><head><script>" + "x();" * 600 + "</script></head><body></body></html>")
    assert not looks_client_rendered("<html><body><main><p>" + "Current vacancy. " * 100 + "</p></main></body></html>")


async def test_capture_flags_unhydrated_spa_shell_as_not_empty() -> None:
    shell = "<html><head><script>" + "const x = 1;" * 400 + "</script></head><body><div id=\"__next\"></div></body></html>"
    session = BrowserSession()
    session._page = _FakePage("https://bfijobsandopportunities.bfi.org.uk/", shell)
    out = await session._capture()
    assert out["rendered_empty"] is True
    assert "client-rendered" in out["content"]
    assert "NOT verified as empty" in out["content"]


async def test_capture_does_not_flag_a_text_page() -> None:
    html = "<html><body><main><p>" + "Current vacancy: Curator. " * 50 + "</p></main></body></html>"
    session = BrowserSession()
    session._page = _FakePage("https://example.com/jobs", html)
    out = await session._capture()
    assert out["rendered_empty"] is False
