"""Client for Telos's internal API (``/internal/cardea/v1/*``).

Telos serves the agent's routine context and property ledger on a private
listener. Every call carries:

- ``Authorization: Bearer <JWT-SVID>``, read from ``telos.token_file`` on each
  call because a spiffe-helper rewrites the file before the token expires;
- ``X-Cardea-Namespace`` / ``X-Cardea-Agent`` from the operation context, which
  Telos maps to its (user, agent) and answers 404 for when that agent has no
  mirrored routine;
- ``X-Cardea-Slot-Time`` when the run was admitted by a trigger slot.

Scope comes only from the context, never from tool arguments, so an agent
cannot read or write another agent's ledger by naming it.
"""

from pathlib import Path
from typing import Any

import httpx

from telos_cardea._cardea import OperationContext, OperationRejected, agent_id, slot_time
from telos_cardea.config import Config

API_PREFIX = "/internal/cardea/v1"
TIMEOUT_SECONDS = 30
# Telos's 404 detail when the (namespace, agent) has no mirrored routine, as opposed to a missing resource.
SCOPE_NOT_FOUND = "No mirrored routine for this agent"


def _token(config: Config) -> str:
    if not config.telos_token_file:
        raise OperationRejected("Telos internal token file is not configured (telos.token_file / TELOS_INTERNAL_TOKEN_FILE)")
    try:
        token = Path(config.telos_token_file).read_text().strip()
    except OSError as error:
        raise OperationRejected(f"Telos internal token file is unreadable: {error.strerror or error}") from error
    if not token:
        raise OperationRejected("Telos internal token file is empty")
    return token


def scope_headers(context: OperationContext) -> dict[str, str]:
    """``X-Cardea-*`` headers derived from the operation context alone."""
    agent = agent_id(context)
    if not agent:
        raise OperationRejected("Telos operations need an agent run: the operation context has no agent_id")
    headers = {"X-Cardea-Namespace": context.namespace, "X-Cardea-Agent": agent}
    if slot := slot_time(context):
        headers["X-Cardea-Slot-Time"] = slot
    return headers


async def request(config: Config, context: OperationContext, method: str, path: str, *, params: dict[str, Any] | None = None,
                  body: Any = None, headers: dict[str, str] | None = None, allow_not_found: bool = False,
                  client: httpx.AsyncClient | None = None) -> httpx.Response:
    """One authenticated call. Refusals Telos is certain about raise ``OperationRejected``.

    - missing configuration, an unreachable listener, 400 (missing scope headers), 401/403, 404,
      409 (a business rule) and 422 (validation, including moving an assessment out of ``applied``)
      are certain to have had no effect, so they raise ``OperationRejected``;
    - timeouts, dropped connections and 5xx (including 503 while Telos has no SPIRE bundle)
      propagate, so Cardea leaves an effect uncertain for its reconciler.

    ``allow_not_found`` returns a resource 404 to the caller instead (the reconciler's by-key
    lookup); an unmapped-scope 404 always raises.
    """
    if not config.telos_url:
        raise OperationRejected("Telos internal URL is not configured (telos.url / TELOS_INTERNAL_URL)")
    all_headers = {"Authorization": f"Bearer {_token(config)}", "Accept": "application/json", **scope_headers(context), **(headers or {})}
    url = config.telos_url.rstrip("/") + API_PREFIX + path
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as own:
                response = await own.request(method, url, params=params, json=body, headers=all_headers)
        else:
            response = await client.request(method, url, params=params, json=body, headers=all_headers)
    except httpx.ConnectError as error:
        # Nothing reached Telos.
        raise OperationRejected(f"Telos internal API is unreachable: {error}") from error
    status = response.status_code
    if status in (401, 403):
        raise OperationRejected(f"Telos refused the workload credential ({status}: {detail(response)})")
    if status == 404:
        if detail(response) == SCOPE_NOT_FOUND:
            raise OperationRejected(
                f"Telos has no mirrored routine for namespace {context.namespace!r} and agent {agent_id(context)!r}")
        if allow_not_found:
            return response
        raise OperationRejected(f"Telos found nothing at {path}: {detail(response)}")
    if status in (400, 409, 422):
        raise OperationRejected(f"Telos rejected the request ({status}): {detail(response)}")
    response.raise_for_status()
    return response


def json_object(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Telos returned a non-object response for {response.request.url.path}")
    return data


def detail(response: httpx.Response) -> str:
    """FastAPI's ``detail``: a string, or a list of pydantic errors rendered as ``loc: msg``."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:1000]
    value = data.get("detail", data) if isinstance(data, dict) else data
    if isinstance(value, list):
        parts = []
        for error in value:
            if isinstance(error, dict) and "msg" in error:
                loc = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
                parts.append(f"{loc}: {error['msg']}" if loc else str(error["msg"]))
            else:
                parts.append(str(error))
        value = "; ".join(parts)
    return str(value)[:1000]
