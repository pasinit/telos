"""Patchright drivers must not outlive a cancelled caller (prod OOM 2026-10-17)."""

import asyncio

import pytest

from telos.transport.browser import driver, fetcher


class FakeDriver:
    def __init__(self, start_s: float) -> None:
        self.start_s = start_s
        self.started = 0
        self.stopped = 0

    def __call__(self) -> "FakeDriver":
        return self

    async def start(self) -> "FakeDriver":
        # Like patchright: the Node process exists before start() returns.
        self.started += 1
        await asyncio.sleep(self.start_s)
        return self

    async def stop(self) -> None:
        self.stopped += 1

    @property
    def running(self) -> int:
        return self.started - self.stopped


async def test_driver_cancelled_mid_start_is_stopped_once_up() -> None:
    fake = FakeDriver(start_s=0.05)
    caller = asyncio.ensure_future(driver.start_driver(fake))
    await asyncio.sleep(0.01)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0.1)
    assert fake.started == 1
    assert fake.running == 0


async def test_driver_that_fails_to_start_needs_no_stop() -> None:
    class Broken(FakeDriver):
        async def start(self) -> "FakeDriver":
            await asyncio.sleep(0.02)
            raise RuntimeError("no node")

    fake = Broken(start_s=0)
    caller = asyncio.ensure_future(driver.start_driver(fake))
    await asyncio.sleep(0.01)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.sleep(0.05)
    assert fake.stopped == 0


async def test_curl_winning_the_race_leaves_no_driver_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # On the 2 GB droplet the driver takes seconds to start and curl answers
    # first: every web_fetch cancelled the browser task mid-start and leaked one.
    fake = FakeDriver(start_s=0.05)
    monkeypatch.setattr(driver, "async_playwright", fake)

    async def fast_curl(url: str) -> tuple[str, int, str]:
        return "<html>" + "listing " * 500 + "</html>", 200, url

    monkeypatch.setattr(fetcher, "_fetch_with_curl", fast_curl)
    for _ in range(5):
        html, status, _ = await fetcher._race_curl_vs_playwright("https://example.invalid")
        assert status == 200 and "listing" in html
    await asyncio.sleep(0.2)
    assert fake.started == 5
    assert fake.running == 0
