"""Bounded browser state for authenticated MCP turns, independent of provider."""
import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from telos.browser import BrowserSession


@dataclass
class _Entry:
    browser: Any
    touched: float
    users: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserSessions:
    def __init__(self, factory: Callable = BrowserSession, *, idle_seconds: float = 1800, max_sessions: int = 8):
        self._factory = factory
        self._idle_seconds = idle_seconds
        self._max_sessions = max_sessions
        self._entries: dict[tuple[str, str, str], _Entry] = {}

    @asynccontextmanager
    async def session(self, key: tuple[str, str, str]):
        entry = self._entries.get(key)
        if entry is None:
            await self.reap()
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self._max_sessions:
                    raise RuntimeError("Browser capacity reached; retry after another turn finishes")
                # No await between the second lookup and claiming the entry.
                entry = _Entry(self._factory(), time.monotonic())
                self._entries[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield entry.browser
        finally:
            entry.users -= 1
            entry.touched = time.monotonic()

    async def close(self, key: tuple[str, str, str]):
        entry = self._entries.pop(key, None)
        if entry is not None:
            async with entry.lock:
                await entry.browser.close()

    async def reap(self):
        now = time.monotonic()
        for key, entry in list(self._entries.items()):
            if not entry.users and now - entry.touched >= self._idle_seconds:
                await self.close(key)

    async def reap_forever(self):
        while True:
            await asyncio.sleep(30)
            await self.reap()

    async def close_all(self):
        for key in list(self._entries):
            await self.close(key)
