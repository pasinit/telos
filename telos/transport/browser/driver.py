"""Start patchright drivers that never outlive a cancelled caller.

``async_playwright().start()`` spawns a Node driver process before it
returns. Cancelling the await while it is in flight (``web_fetch`` cancels
its browser task whenever curl wins the race) drops the driver with
nothing left to stop it, and the process lives until Telos restarts. On
the prod droplet those orphans accumulated to ~650 MB within three days
and got Recordatio OOM-killed mid-routine (2026-10-17).
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from patchright.async_api import Playwright, async_playwright

log = logging.getLogger(__name__)

_STOP_S = 10.0

# Strong references: the event loop only keeps weak ones to running tasks.
_late_stops: set[asyncio.Task] = set()


async def start_driver(factory: Callable[[], Any] | None = None) -> Playwright:
    """Start a driver; if the caller is cancelled mid-start, stop it once it is up."""
    starting = asyncio.ensure_future((factory or async_playwright)().start())
    try:
        return await asyncio.shield(starting)
    except asyncio.CancelledError:
        starting.add_done_callback(_stop_late)
        raise


async def stop_driver(playwright: Playwright) -> None:
    """Stop a driver, bounded so a wedged teardown never blocks the caller."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(playwright.stop(), timeout=_STOP_S)


def _stop_late(starting: asyncio.Future) -> None:
    if starting.cancelled() or starting.exception() is not None:
        return
    log.info("browser driver: stopping one started for a cancelled caller")
    task = asyncio.ensure_future(stop_driver(starting.result()))
    _late_stops.add(task)
    task.add_done_callback(_late_stops.discard)
