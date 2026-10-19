"""SerpAPI search engine."""

import os

import httpx


class SerpApiSearch:
    """Search using SerpAPI."""

    async def search(self, query: str) -> tuple[str, bool]:
        """Search using SerpAPI."""
        key = os.getenv("SERPAPI_KEY")
        if not key:
            raise RuntimeError("SERPAPI_KEY not set")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://serpapi.com/search",
                params={"q": query, "engine": "google", "num": 5, "api_key": key},
            )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("error"):
            raise RuntimeError("SerpAPI returned a search error")
        if not isinstance(data.get("organic_results"), list):
            raise RuntimeError("SerpAPI returned an unrecognised search response")
        results = data.get("organic_results", [])
        lines = [
            f"{r.get('title', '')}\n{r.get('link', '')}\n{r.get('snippet', '')}"
            for r in results
        ]
        return "\n\n".join(lines) or "No results found.", False
