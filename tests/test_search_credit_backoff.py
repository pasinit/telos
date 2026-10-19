from unittest.mock import AsyncMock

import httpx

from telos.transport.search import base
from telos.transport.search.serper import SerperSearch


async def test_exhausted_credit_backoff_uses_fallback_without_repeated_provider_calls(monkeypatch):
    response = httpx.Response(400, json={"message": "Not enough credits"}, request=httpx.Request("POST", "https://google.serper.dev/search"))
    post = AsyncMock(return_value=response)
    monkeypatch.setenv("SERPER_KEY", "test")
    monkeypatch.setenv("SEARCH_ENGINE", "serper")
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    engine = SerperSearch()
    monkeypatch.setattr(base, "get_engine", lambda name: engine)
    fallback = AsyncMock(return_value=("fallback result", False))
    monkeypatch.setattr(base.DuckDuckGoSearch, "search", fallback)
    assert await base.search("first") == ("fallback result", False)
    assert await base.search("second") == ("fallback result", False)
    assert post.await_count == 1 and fallback.await_count == 2
    engine._blocked_until = 0
    await base.search("after cooldown")
    assert post.await_count == 2
