"""``memory.reconcile_*`` operations: the nightly memory reconcile, judged by the agent.

Recordatio finds candidate pairs of memories and verifies verdicts; the agent run
is the judge. Its loop is candidates → one apply per pair → finish:

- ``memory.reconcile_candidates`` (read): ``GET /v1/users/{user}/memory-reconcile/candidates``.
- ``memory.reconcile_apply`` (effect, scope ``["/pair_token"]``): ``POST …/verdicts``.
  Recordatio verifies the verdict against the live store and deletes the dropped
  memory only if every check passes. It is idempotent per pair token, so a retry
  returns the first outcome. The reconciler looks the token up
  (``GET …/verdicts/{token}``).
- ``memory.reconcile_finish`` (effect, scope ``[]``: once per run): ``POST …/runs/{run}/finish``.
  It runs the deterministic clean-up, settles the memory-feedback ops, VACUUMs and
  returns the run summary. The reconciler reads ``GET …/runs/{run}``.
- ``memory.reconcile_apply_preview`` / ``memory.reconcile_finish_preview`` (read): the
  same calls with ``dry_run``, so shadow runs verify without deleting anything.

The Recordatio user comes from ``recordatio.scopes`` (keyed by the task's namespace
and agent, like ``memory.recall``), never from arguments. The run id is derived from
the task's idempotency context (the trigger slot), so every call of one nightly run
shares a run id and its budget, and a retried slot resumes the same run. Requests
carry the reconcile key (``recordatio.reconcile_key``), which Recordatio lets call
only these routes.
"""

import hashlib
from typing import Any

import httpx

from telos_cardea._cardea import Arguments, OperationContext, OperationRejected, Output, Reconciliation, agent_id
from telos_cardea.bounded import fit
from telos_cardea.config import Config, load_config

VERDICT_NOT_FOUND = "verdict_not_found"
RUN_NOT_FOUND = "run_not_found"
_SUMMARY_KEYS = ("deleted", "by_relation", "by_source", "verdicts", "kept_reasons", "ops_applied", "ops_dismissed",
                 "ops_pending", "legacy_unclassified_kept", "open_pairs", "vacuumed", "replayed")


def run_id(context: OperationContext) -> str:
    """One run id per (namespace, agent, idempotency context): stable across retries of a slot."""
    slot = getattr(context, "idempotency_context", None) or getattr(context, "slot_time", None) or context.task_id
    digest = hashlib.sha256(f"{context.namespace}|{agent_id(context) or ''}|{slot}".encode()).hexdigest()[:32]
    return f"cardea-{digest}"


async def candidates(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                     client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.reconcile_candidates`` (read): the next batch of pairs to judge."""
    config = config or load_config()
    limit = max(1, min(int(arguments.get("limit", config.reconcile_batch_size)), config.reconcile_batch_size))
    run = run_id(context)
    response = await _request(config, context, "GET", "/candidates", params={"run_id": run, "limit": limit}, client=client)
    data = _json(response)
    out = {
        "run_id": run,
        "pairs": data.get("pairs", []),
        "budget_left": int(data.get("budget_left", 0)),
        "more": bool(data.get("more", False)),
        "finished": bool(data.get("finished", False)),
    }
    result, trimmed = fit(out, ("list", "pairs"))
    if trimmed:
        result["more"] = True  # the pairs left out come back in the next batch
    return result


async def apply(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.reconcile_apply`` (effect): submit one verdict."""
    return await _verdict(arguments, context, config or load_config(), client, dry_run=False)


async def apply_preview(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                        client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.reconcile_apply_preview`` (read): verify one verdict, delete nothing."""
    return await _verdict(arguments, context, config or load_config(), client, dry_run=True)


async def reconcile_apply(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                          client: httpx.AsyncClient | None = None) -> Reconciliation:
    """Entry point ``memory.reconcile_apply.reconcile``: look the pair token's outcome up.

    - 200: applied, with the recorded outcome;
    - 404 ``verdict_not_found``: not applied (Recordatio records the outcome with the deletion);
    - anything else raises, leaving the effect uncertain.
    """
    config = config or load_config()
    token = str(arguments["pair_token"])
    response = await _request(config, context, "GET", f"/verdicts/{token}", allow_not_found=True, client=client)
    if response.status_code == 404:
        if _code(response) != VERDICT_NOT_FOUND:
            raise RuntimeError(f"Recordatio verdict lookup returned an unexpected 404: {response.text[:200]}")
        return Reconciliation(applied=False)
    return Reconciliation(applied=True, output=_outcome(_json(response), dry_run=False, replayed=True))


async def finish(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                 client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.reconcile_finish`` (effect): close the run and return its summary."""
    return await _finish(context, config or load_config(), client, dry_run=False)


async def finish_preview(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                         client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``memory.reconcile_finish_preview`` (read): what finish would do."""
    return await _finish(context, config or load_config(), client, dry_run=True)


async def reconcile_finish(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                           client: httpx.AsyncClient | None = None) -> Reconciliation:
    """Entry point ``memory.reconcile_finish.reconcile``: applied once the run has a finish summary."""
    config = config or load_config()
    run = run_id(context)
    response = await _request(config, context, "GET", f"/runs/{run}", allow_not_found=True, client=client)
    if response.status_code == 404:
        if _code(response) != RUN_NOT_FOUND:
            raise RuntimeError(f"Recordatio run lookup returned an unexpected 404: {response.text[:200]}")
        return Reconciliation(applied=False)
    data = _json(response)
    if not data.get("finished_at") or not isinstance(data.get("summary"), dict):
        return Reconciliation(applied=False)
    return Reconciliation(applied=True, output=_summary(run, {**data["summary"], "replayed": True}, dry_run=False))


async def _verdict(arguments: Arguments, context: OperationContext, config: Config, client: httpx.AsyncClient | None,
                   *, dry_run: bool) -> Output:
    body = {"run_id": run_id(context), "pair_token": str(arguments["pair_token"]), "verdict": arguments["verdict"],
            "dry_run": dry_run}
    headers = {} if dry_run else {"Idempotency-Key": context.effect_key}
    response = await _request(config, context, "POST", "/verdicts", body=body, headers=headers, client=client)
    return _outcome(_json(response), dry_run=dry_run, replayed=None)


async def _finish(context: OperationContext, config: Config, client: httpx.AsyncClient | None, *, dry_run: bool) -> Output:
    run = run_id(context)
    params = {"dry_run": "1"} if dry_run else None
    response = await _request(config, context, "POST", f"/runs/{run}/finish", params=params, client=client)
    data = _json(response)
    if dry_run:
        out = {
            "run_id": run,
            "dry_run": True,
            "would_delete": len(data.get("would_delete", [])),
            "ops_settled": len(data.get("ops", [])),
            "ops_pending": int(data.get("ops_pending", 0)),
            "open_pairs": int(data.get("open_pairs", 0)),
        }
        return out
    return _summary(run, data, dry_run=False)


def _summary(run: str, data: dict[str, Any], *, dry_run: bool) -> Output:
    return {"run_id": run, "dry_run": dry_run, **{k: data[k] for k in _SUMMARY_KEYS if k in data}}


def _outcome(data: dict[str, Any], *, dry_run: bool, replayed: bool | None) -> Output:
    out: Output = {
        "outcome": str(data["outcome"]),
        "reason": str(data.get("reason") or ""),
        "dry_run": dry_run,
        "replayed": bool(data.get("replayed", False)) if replayed is None else replayed,
    }
    for key in ("kept_unit_id", "dropped_unit_id"):
        if data.get(key):
            out[key] = str(data[key])
    return out


def _user(config: Config, context: OperationContext) -> str:
    agent = agent_id(context)
    scope = config.recordatio_scope(context.namespace, agent)
    if scope is None:
        raise OperationRejected(f"no Recordatio scope configured for namespace {context.namespace!r} and agent {agent!r}")
    return scope.user_id


async def _request(config: Config, context: OperationContext, method: str, path: str, *,
                   params: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
                   headers: dict[str, str] | None = None, allow_not_found: bool = False,
                   client: httpx.AsyncClient | None = None) -> httpx.Response:
    """One call to ``/v1/users/{user}/memory-reconcile{path}``.

    422 (malformed verdict or token) and 409 (run already finished) are
    ``OperationRejected``: Recordatio refused before any effect. 401/403 too
    (a missing or wrong reconcile key). Other errors propagate.
    """
    url = f"{config.recordatio_url.rstrip('/')}/v1/users/{_user(config, context)}/memory-reconcile{path}"
    key = config.recordatio_reconcile_key or config.recordatio_api_key or "nightly"
    all_headers = {"Authorization": f"Bearer {key}", **(headers or {})}
    if client is None:
        async with httpx.AsyncClient(timeout=60) as own:
            response = await own.request(method, url, params=params, json=body, headers=all_headers)
    else:
        response = await client.request(method, url, params=params, json=body, headers=all_headers)
    if response.status_code in (401, 403, 409, 422):
        raise OperationRejected(f"Recordatio refused {method} {path}: {response.status_code} {response.text[:300]}")
    if response.status_code == 404 and allow_not_found:
        return response
    response.raise_for_status()
    return response


def _json(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Recordatio returned an unrecognised reconcile response")
    return data


def _code(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data.get("code") if isinstance(data, dict) else None
