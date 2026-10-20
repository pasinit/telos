"""browser.* inside Cardea's confined worker: no DNS, proxy egress, read-only network (fake Chromium)."""

import socket
import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from telos_cardea import browser
from telos_cardea._cardea import OperationContext, OperationRejected

from conftest import assert_output

MakeContext = Callable[..., OperationContext]
PROXY = "http://127.0.0.2:3128"


@pytest.fixture
def confined(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker environment: a proxy and no resolver."""
    monkeypatch.setenv("HTTPS_PROXY", PROXY)

    def no_dns(*args: object, **kwargs: object) -> Any:
        raise AssertionError("no DNS inside the sandbox")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)


@pytest.mark.usefixtures("confined")
def test_public_names_pass_without_dns_behind_proxy() -> None:
    assert not browser.is_private_url("https://www.rightmove.co.uk/property-to-rent/Brixton.html")
    assert not browser.is_private_url("http://93.184.216.34/")


@pytest.mark.usefixtures("confined")
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.1/", "http://2130706433/", "http://0x7f.0.0.1/", "http://[::1]/",
    "http://[::ffff:10.0.0.1]/", "http://10.0.0.5:8080/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
    "http://0.0.0.0/", "http://localhost:8787/", "http://api.localhost/", "http://printer.local/", "http://intranet/",
    "file:///etc/passwd", "ftp://example.com/", "https:///nohost",
])
def test_private_targets_are_refused_syntactically(url: str) -> None:
    assert browser.is_private_url(url)


def test_without_proxy_names_are_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, None, None, "", ("10.1.2.3", 0))])
    assert browser.is_private_url("https://rebinding.example.com/")


@pytest.mark.parametrize(("method", "url", "allowed"), [
    ("GET", "https://www.zoopla.co.uk/to-rent/property/brixton/", True),
    ("HEAD", "https://www.zoopla.co.uk/", True),
    ("get", "https://www.zoopla.co.uk/", True),
    ("POST", "https://www.zoopla.co.uk/api/enquiry", False),
    ("PUT", "https://www.zoopla.co.uk/x", False),
    ("PATCH", "https://www.zoopla.co.uk/x", False),
    ("DELETE", "https://www.zoopla.co.uk/x", False),
    ("POST", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/flow/ov1/0", True),
    ("POST", "https://challenges.cloudflare.com/turnstile/v0/api.js", True),
    ("POST", "https://www.zoopla.co.uk/cdn-cgi/challenge-platform/h/g/flow/ov1/0", True),
    ("POST", "https://www.zoopla.co.uk/forms/cdn-cgi/challenge-platform/", False),
    ("POST", "https://evil.example/?next=challenges.cloudflare.com", False),
    ("GET", "http://169.254.169.254/latest/meta-data", False),
    ("GET", "http://localhost:8787/admin", False),
    ("GET", "data:image/png;base64,AAAA", True),
    ("GET", "file:///etc/passwd", False),
])
def test_request_guard_allows_only_reads(method: str, url: str, allowed: bool) -> None:
    assert browser.allow_request(method, url) is allowed


@pytest.mark.parametrize(("facts", "post"), [
    ({"tag": "button", "type": "", "form_method": "post", "override": ""}, True),
    ({"tag": "button", "type": "submit", "form_method": "POST", "override": ""}, True),
    ({"tag": "input", "type": "submit", "form_method": "post", "override": ""}, True),
    ({"tag": "input", "type": "image", "form_method": "post", "override": ""}, True),
    ({"tag": "button", "type": "", "form_method": "get", "override": "post"}, True),
    ({"tag": "button", "type": "", "form_method": "post", "override": "get"}, False),
    ({"tag": "button", "type": "button", "form_method": "post", "override": ""}, False),
    ({"tag": "button", "type": "", "form_method": "get", "override": ""}, False),
    ({"tag": "button", "type": "", "form_method": None, "override": ""}, False),
    ({"tag": "a", "type": "", "form_method": "post", "override": ""}, False),
])
def test_post_submit_detection(facts: dict[str, Any], post: bool) -> None:
    assert browser.is_post_submit(facts) is post


def test_challenge_detection() -> None:
    assert browser.is_challenge(403, {"cf-mitigated": "challenge", "server": "cloudflare"})
    assert browser.is_challenge(503, {"CF-Mitigated": "Challenge"})
    assert not browser.is_challenge(403, {"server": "cloudflare"})
    assert not browser.is_challenge(200, {"cf-mitigated": "challenge"})


# ── fake patchright: records how Chromium is launched and guarded ──────


class FakeRoute:
    def __init__(self, method: str, url: str) -> None:
        self.request = SimpleNamespace(method=method, url=url)
        self.outcome = ""

    async def continue_(self) -> None:
        self.outcome = "continued"

    async def abort(self, error_code: str) -> None:
        self.outcome = f"aborted:{error_code}"


class FakeWebSocketRoute:
    closed = False

    async def close(self) -> None:
        self.closed = True


class FakeElement:
    def __init__(self, page: "FakePage", facts: dict[str, Any]) -> None:
        self.page, self.facts = page, facts

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def evaluate(self, script: str) -> dict[str, Any]:
        assert script == browser._SUBMIT_FACTS_JS
        return self.facts

    async def click(self, timeout: int) -> None:
        self.page.clicked.append(self.facts["tag"])


class FakeLocator:
    def __init__(self, elements: list[FakeElement]) -> None:
        self.elements = elements

    async def all(self) -> list[FakeElement]:
        return self.elements


class FakePage:
    def __init__(self, world: "World") -> None:
        self.world = world
        self.url = ""
        self.clicked: list[str] = []

    async def goto(self, url: str, wait_until: str, timeout: int) -> Any:
        if self.world.goto_error:
            raise RuntimeError(self.world.goto_error)
        self.url = url
        return SimpleNamespace(status=self.world.status, headers=self.world.headers)

    async def content(self) -> str:
        return "<html><main><p>" + "Two bedroom flat. " * 20 + "</p></main></html>"

    async def title(self) -> str:
        return "Listing"

    async def evaluate(self, script: str) -> Any:
        if script == browser._CHALLENGE_DONE_JS:
            return self.world.challenge_clears
        return []

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    def get_by_role(self, role: str, name: str, exact: bool) -> FakeLocator:
        return FakeLocator([FakeElement(self, facts) for facts in self.world.buttons] if role == "button" else [])

    def get_by_text(self, text: str, exact: bool) -> FakeLocator:
        return FakeLocator([])

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator([])


class FakeContext:
    def __init__(self, world: "World") -> None:
        self.world = world

    async def route(self, pattern: str, handler: Any) -> None:
        self.world.routes.append((pattern, handler))

    async def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.world.ws_routes.append((pattern, handler))

    async def new_page(self) -> FakePage:
        self.world.page = FakePage(self.world)
        return self.world.page


class FakeBrowser:
    def __init__(self, world: "World") -> None:
        self.world = world

    async def new_context(self, **options: Any) -> FakeContext:
        self.world.context_options = options
        return FakeContext(self.world)

    async def close(self) -> None:
        self.world.closed = True


class World:
    def __init__(self) -> None:
        self.launch_options: dict[str, Any] = {}
        self.context_options: dict[str, Any] = {}
        self.routes: list[tuple[str, Any]] = []
        self.ws_routes: list[tuple[str, Any]] = []
        self.page: FakePage | None = None
        self.closed = False
        self.status = 200
        self.headers: dict[str, str] = {}
        self.goto_error = ""
        self.challenge_clears = True
        self.buttons: list[dict[str, Any]] = []

    def async_playwright(self) -> Any:
        world = self

        class Manager:
            async def __aenter__(self) -> Any:
                async def launch(**options: Any) -> FakeBrowser:
                    world.launch_options = options
                    return FakeBrowser(world)

                return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

            async def __aexit__(self, *exc: object) -> None:
                return None

        return Manager()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, confined: None) -> World:
    world = World()
    package, api = ModuleType("patchright"), ModuleType("patchright.async_api")
    api.async_playwright = world.async_playwright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "patchright", package)
    monkeypatch.setitem(sys.modules, "patchright.async_api", api)
    monkeypatch.setattr(browser, "CHALLENGE_POLL_S", 0.0)
    return world


async def test_open_launches_through_proxy_with_read_only_context(world: World, make_context: MakeContext) -> None:
    output = await browser.open_page({"url": "https://www.onthemarket.com/to-rent/property/brixton/"}, make_context())
    assert_output("browser.open", output)
    assert world.launch_options["proxy"] == {"server": PROXY}
    assert "--no-sandbox" in world.launch_options["args"]
    assert world.context_options["service_workers"] == "block"
    assert [pattern for pattern, _ in world.routes] == ["**/*"] and [p for p, _ in world.ws_routes] == ["**/*"]
    assert world.closed

    guard = world.routes[0][1]
    post, get = FakeRoute("POST", "https://www.onthemarket.com/enquiry"), FakeRoute("GET", "https://www.onthemarket.com/")
    challenge = FakeRoute("POST", "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/flow")
    for route in (post, get, challenge):
        await guard(route)
    assert (post.outcome, get.outcome, challenge.outcome) == ("aborted:blockedbyclient", "continued", "continued")

    socket_route = FakeWebSocketRoute()
    await world.ws_routes[0][1](socket_route)
    assert socket_route.closed


async def test_no_proxy_option_outside_the_sandbox(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.setattr(browser, "is_private_url", lambda url: False)
    await browser.render("https://www.example.com/")
    assert "proxy" not in world.launch_options and world.context_options["service_workers"] == "block"


async def test_render_is_guarded_too(world: World) -> None:
    await browser.render("https://www.dexters.co.uk/property-lettings/properties-to-rent-in-bayswater")
    assert world.routes and world.context_options["service_workers"] == "block"


async def test_click_refuses_post_form_submit(world: World, make_context: MakeContext) -> None:
    world.buttons = [{"tag": "button", "type": "", "form_method": "post", "override": ""}]
    with pytest.raises(OperationRejected, match="POST form"):
        await browser.click({"url": "https://www.openrent.co.uk/x", "target": "Send enquiry"}, make_context())
    assert world.page is not None and world.page.clicked == []


async def test_click_allows_get_form_button(world: World, make_context: MakeContext) -> None:
    world.buttons = [{"tag": "button", "type": "submit", "form_method": "get", "override": ""}]
    output = await browser.click({"url": "https://www.openrent.co.uk/x", "target": "Search"}, make_context())
    assert world.page is not None and world.page.clicked == ["button"]
    assert_output("browser.click", output)


async def test_host_outside_allow_list_is_rejected_not_crashed(world: World, make_context: MakeContext) -> None:
    world.goto_error = "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://elsewhere.example/\nCall log: ..."
    with pytest.raises(OperationRejected, match="ERR_TUNNEL_CONNECTION_FAILED"):
        await browser.open_page({"url": "https://elsewhere.example/"}, make_context())


async def test_cloudflare_challenge_that_clears_is_read(world: World, make_context: MakeContext) -> None:
    world.status, world.headers = 403, {"cf-mitigated": "challenge"}
    output = await browser.open_page({"url": "https://www.zoopla.co.uk/to-rent/property/brixton/"}, make_context())
    assert output["title"] == "Listing"


async def test_cloudflare_challenge_that_stays_is_rejected(world: World, make_context: MakeContext, monkeypatch: pytest.MonkeyPatch) -> None:
    world.status, world.headers, world.challenge_clears = 403, {"cf-mitigated": "challenge"}, False
    monkeypatch.setattr(browser, "CHALLENGE_WAIT_S", 0.01)
    with pytest.raises(OperationRejected, match="challenge"):
        await browser.open_page({"url": "https://www.zoopla.co.uk/to-rent/property/brixton/"}, make_context())


async def test_plain_403_is_still_rejected(world: World, make_context: MakeContext) -> None:
    world.status, world.headers = 403, {"server": "cloudflare"}
    with pytest.raises(OperationRejected, match="HTTP 403"):
        await browser.open_page({"url": "https://www.zoopla.co.uk/"}, make_context())
