"""Gemini Grounding search engine."""

import asyncio
import logging
import os
import time

from google import genai
from google.genai import types

log = logging.getLogger(__name__)


class GeminiSearch:
    """Search using Gemini Grounding with Google Search.

    Tracks backoff state as instance state so
    multiple instances or tests don't share backoff state.
    """

    def __init__(self) -> None:
        # Timestamp after which Gemini is available again (0 = always available)
        self._backoff_until: float = 0

    def is_backing_off(self) -> bool:
        """Return True if we are currently in a rate-limit backoff window."""
        return time.time() < self._backoff_until

    def record_rate_limit(self, backoff_seconds: float = 60) -> None:
        """Record a rate-limit hit; backoff for *backoff_seconds*."""
        self._backoff_until = time.time() + backoff_seconds
        log.warning("GeminiSearch: rate-limited — backing off for %.0fs", backoff_seconds)

    async def search(self, query: str) -> tuple[str, bool]:
        """Search using Gemini Grounding with Google Search."""
        return await asyncio.to_thread(self._search_sync, query)

    def _search_sync(self, query: str) -> tuple[str, bool]:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY not set")

        with genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000)) as client:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=query,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                ),
            )

        if not response.candidates or not response.text:
            raise RuntimeError("Gemini returned no usable search response")
        result = response.text
        metadata = getattr(response.candidates[0], "grounding_metadata", None)
        if metadata and hasattr(metadata, "grounding_chunks"):
            urls = set()
            for chunk in (metadata.grounding_chunks or []):
                web = getattr(chunk, "web", None)
                if web:
                    urls.add(f"{getattr(web, 'title', '')} — {getattr(web, 'uri', '')}")
            if urls:
                result += "\n\nSources:\n" + "\n".join(f"- {u}" for u in urls)

        return result, False
