"""``memory.recall`` read operation over Recordatio's memory-search API.

Same request as Telos's ``RecordatioFeedbackClient.memory_search``:
``GET {RECORDATIO_URL}/v1/users/{user_id}/memory-search?query=…&mode=production&slug=…&k=…``
with ``Authorization: Bearer {CHAT_API_KEY}``.

Telos reads ``user_id`` and ``slug`` from the calling agent's context. Cardea has
no Telos user, so they come from configuration keyed by the task's Cardea
namespace (``[recordatio.scopes."<namespace>"]``), never from arguments: an agent
cannot read another user's memory by naming it.
"""

from typing import Any

import httpx

from telos_cardea._cardea import Arguments, OperationContext, OperationRejected, Output
from telos_cardea.config import Config, load_config

_RESULT_KEYS = ("results", "hits", "memories", "units", "matches")


async def recall(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                 client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.recall``."""
    config = config or load_config()
    scope = config.recordatio_scopes.get(context.namespace)
    if scope is None:
        raise OperationRejected(f"no Recordatio scope configured for namespace {context.namespace!r}")
    query = str(arguments["query"])
    k = int(arguments.get("k", 10))
    params: dict[str, Any] = {"query": query, "mode": "production", "slug": scope.slug, "k": k}
    # Telos falls back to the literal "nightly" key when no chat key is set.
    headers = {"Authorization": f"Bearer {config.recordatio_api_key or 'nightly'}"}
    path = f"/v1/users/{scope.user_id}/memory-search"
    if client is None:
        async with httpx.AsyncClient(base_url=config.recordatio_url.rstrip("/"), timeout=60) as own:
            response = await own.get(path, params=params, headers=headers)
    else:
        response = await client.get(config.recordatio_url.rstrip("/") + path, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Recordatio returned an unrecognised memory-search response")
    hits: list[Any] = []
    for key in _RESULT_KEYS:
        if isinstance(data.get(key), list):
            hits = data[key]
            break
    # The payload shape is Recordatio's; pass it through so no recalled detail is lost.
    return {"query": query, "count": len(hits), "recordatio": data}
