"""``memory.recall`` and ``memory.briefing`` read operations over Recordatio.

``memory.recall`` sends the same request as Telos's ``RecordatioFeedbackClient.memory_search``:
``GET {RECORDATIO_URL}/v1/users/{user_id}/memory-search?query=…&mode=production&slug=…&k=…``
with ``Authorization: Bearer {CHAT_API_KEY}``. ``memory.briefing`` calls
``GET {RECORDATIO_URL}/v1/users/{user_id}/briefing?slug=…&query=…``, which returns the
briefing the agent hook would inject without recording a briefing event.

Telos reads ``user_id`` and ``slug`` from the calling agent's context. Cardea has
no Telos user, so they come from configuration keyed by the task's Cardea namespace
and agent (``[recordatio.scopes."<namespace>/<agent_id>"]``, falling back to a bare
``"<namespace>"`` key), never from arguments: an agent cannot read another user's
memory by naming it.
"""

from typing import Any

import httpx

from telos_cardea._cardea import Arguments, OperationContext, OperationRejected, Output, agent_id
from telos_cardea.bounded import fit
from telos_cardea.config import Config, RecordatioScope, load_config

_RESULT_KEYS = ("results", "hits", "memories", "units", "matches")


async def recall(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                 client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.recall``."""
    config = config or load_config()
    scope = _scope(config, context)
    query = str(arguments["query"])
    k = int(arguments.get("k", 10))
    params: dict[str, Any] = {"query": query, "mode": "production", "slug": scope.slug, "k": k}
    data = await _get(config, f"/v1/users/{scope.user_id}/memory-search", params, client)
    hits: list[Any] = []
    for key in _RESULT_KEYS:
        if isinstance(data.get(key), list):
            hits = data[key]
            break
    # The payload shape is Recordatio's; pass it through so no recalled detail is lost,
    # except the lowest-ranked hits when they would not fit Cardea's result cap.
    out, trimmed = fit({"query": query, "count": len(hits), "recordatio": data}, *(("list", f"recordatio.{k}") for k in _RESULT_KEYS))
    if trimmed:
        kept = len(out["recordatio"][trimmed[0].split(".", 1)[1]])
        out["recordatio"]["omitted"] = len(hits) - kept
        out["count"] = kept
    return out


async def briefing(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                   client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.briefing``."""
    config = config or load_config()
    scope = _scope(config, context)
    params = {"slug": scope.slug, "query": str(arguments["query"])}
    data = await _get(config, f"/v1/users/{scope.user_id}/briefing", params, client)
    if not isinstance(data.get("briefing"), str):
        raise RuntimeError("Recordatio returned an unrecognised briefing response")
    out, _ = fit({"briefing": data["briefing"], "units": int(data.get("units", 0))}, ("text", "briefing"))
    return out


def _scope(config: Config, context: OperationContext) -> RecordatioScope:
    agent = agent_id(context)
    scope = config.recordatio_scope(context.namespace, agent)
    if scope is None:
        raise OperationRejected(f"no Recordatio scope configured for namespace {context.namespace!r} and agent {agent!r}")
    return scope


async def _get(config: Config, path: str, params: dict[str, Any], client: httpx.AsyncClient | None) -> dict[str, Any]:
    # Telos falls back to the literal "nightly" key when no chat key is set.
    headers = {"Authorization": f"Bearer {config.recordatio_api_key or 'nightly'}"}
    if client is None:
        async with httpx.AsyncClient(base_url=config.recordatio_url.rstrip("/"), timeout=60) as own:
            response = await own.get(path, params=params, headers=headers)
    else:
        response = await client.get(config.recordatio_url.rstrip("/") + path, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Recordatio returned an unrecognised response for {path}")
    return data
