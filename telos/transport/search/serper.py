"""Serper.dev search engine (Google results, fast and cheap)."""

import os
import time

import httpx


class SerperSearch:
    """Search using Serper.dev (Google results, fast and cheap)."""

    def __init__(self):
        self._blocked_until = 0.0

    def is_backing_off(self) -> bool:
        return time.monotonic() < self._blocked_until

    async def search(self, query: str) -> tuple[str, bool]:
        """Search using Serper.dev."""
        key = os.getenv("SERPER_KEY")
        if not key:
            raise RuntimeError("SERPER_KEY not set")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": 5},
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
            )
        if resp.status_code == 400:
            try:
                exhausted = "not enough credits" in str(resp.json().get("message", "")).lower()
            except (ValueError, AttributeError):
                exhausted = False
            if exhausted:
                self._blocked_until = time.monotonic() + 3600
                raise RuntimeError("Serper credits exhausted; using fallback for one hour")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("organic"), list):
            raise RuntimeError("Serper returned an error or unrecognised search response")
        results = data.get("organic", [])
        lines = [
            f"{r.get('title', '')}\n{r.get('link', '')}\n{r.get('snippet', '')}"
            for r in results
        ]
        return "\n\n".join(lines) or "No results found.", False
