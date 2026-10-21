"""The persistent BrowserSession must wall-clock bound both the Chromium
launch and each per-call page operation.

Before this was hardened, ``_ensure_started`` launched Chromium with no
``timeout=`` kwarg and no ``asyncio.wait_for``, so a degraded backend hit
patchright's 180000ms default and wedged the whole run (prod 2026-10-16/17,
'BrowserType.launch: Timeout 180000ms exceeded'). Nothing bounded the
per-call work either, so one wedged page could burn the entire run budget.
"""

import asyncio

import pytest

from telos import browser as browser_mod
from telos.browser import BrowserSession


class _HangingChromium:
    async def launch(self, **kwargs):  # noqa: ANN003 - mirrors patchright signature
        # A wedged backend: launch never returns (patchright's own default
        # would only give up after 180000ms).
        await asyncio.sleep(3600)


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _HangingChromium()

    async def stop(self) -> None:
        return None


async def _fake_start_driver(factory=None):
    return _FakePlaywright()


async def test_open_is_bounded_when_launch_wedges(monkeypatch):
    """open() must return an error quickly instead of hanging on a stuck launch."""
    monkeypatch.setattr(browser_mod, "start_driver", _fake_start_driver)
    monkeypatch.setattr(browser_mod, "_BROWSER_LAUNCH_S", 0.2)
    monkeypatch.setattr(browser_mod, "_BROWSER_LAUNCH_MS", 100)

    session = BrowserSession()
    # Without the fix the launch sleeps forever and open() never returns, so
    # this outer bound would raise TimeoutError and fail the test.
    result = await asyncio.wait_for(session.open("https://example.invalid"), timeout=5)

    assert result["ok"] is False
    assert "open failed" in result["error"]
    assert session._crashed is True
    await session.close()


async def test_bounded_times_out_and_marks_session_crashed(monkeypatch):
    """A wedged per-call operation is cancelled and the session marked crashed
    so the next call rebuilds it, rather than starving the rest of the run."""
    monkeypatch.setattr(browser_mod, "_BROWSER_WORK_S", 0.1)

    session = BrowserSession()

    async def _hang() -> dict:
        await asyncio.sleep(3600)
        return {}

    with pytest.raises(TimeoutError):
        await session._bounded(_hang())
    assert session._crashed is True
