"""Property ledger operations over Telos's internal API.

``ledger.list`` and ``ledger.history`` replace Telos's ``list_property_candidates``
tool, ``ledger.record`` its ``record_property_assessment``. The assessment body is
Telos's ``PropertyAssessmentInput``; the descriptors carry its JSON schema
(``tests/test_ledger.py`` fails when it drifts), and Telos applies the cross-field
rules the schema cannot express, answering 422.

``ledger.record`` sends Cardea's ``effect_key`` as the ``Idempotency-Key``. Telos
stores it with the assessment, so a retry records nothing new and the reconciler
can look the assessment up by key.
"""

from typing import Any

import httpx

from telos_cardea import telos_api
from telos_cardea._cardea import Arguments, OperationContext, Output, Reconciliation
from telos_cardea.config import Config, load_config

MAX_PAGE = 200
MAX_HISTORY_PAGE = 20
# Telos's by-key 404 (code, and the detail text of servers without codes); any other 404 leaves the effect uncertain.
KEY_NOT_FOUND_CODE = "assessment_not_found"
KEY_NOT_FOUND = "No assessment recorded under this key"


async def list_(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``ledger.list`` (read): one page of the agent's latest assessments."""
    params = {"offset": int(arguments.get("offset", 0)), "limit": min(int(arguments.get("limit", 50)), MAX_PAGE)}
    response = await telos_api.request(config or load_config(), context, "GET", "/ledger", params=params, client=client)
    return telos_api.json_object(response)


async def history(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                  client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``ledger.history`` (read): recorded assessments for one listing URL, newest first."""
    params = {"url": str(arguments["url"]), "offset": int(arguments.get("offset", 0)),
              "limit": min(int(arguments.get("limit", MAX_HISTORY_PAGE)), MAX_HISTORY_PAGE)}
    response = await telos_api.request(config or load_config(), context, "GET", "/ledger/history", params=params, client=client)
    return telos_api.json_object(response)


async def record(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                 client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``ledger.record`` (effect): persist one assessment, at most once per effect key."""
    data = await _post(arguments, context, config or load_config(), client, dry_run=False)
    return {"recorded": True, "assessment_id": str(data["assessment_id"]), "duplicate": bool(data.get("duplicate", False))}


async def record_preview(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                         client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``ledger.record_preview`` (read): Telos validates the assessment and stores nothing.

    Shadow mode exposes this instead of ``ledger.record``; an invalid assessment
    fails the same way it would live.
    """
    data = await _post(arguments, context, config or load_config(), client, dry_run=True)
    return {"preview": True, "recorded": False, "valid": bool(data.get("valid", True)), "url": str(arguments["url"])}


async def reconcile_record(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                           client: httpx.AsyncClient | None = None) -> Reconciliation:
    """Entry point ``ledger.record.reconcile``: look the assessment up by its idempotency key.

    - 200: applied, with the stored assessment id;
    - 404 ``assessment_not_found`` (or, without a code, "No assessment recorded under this key"): not applied (Telos commits the key with the
      assessment in the agent's own ledger, so no key means no record);
    - anything else, including an unmapped-scope 404, raises, leaving the effect uncertain.
    """
    response = await telos_api.request(config or load_config(), context, "GET", f"/ledger/assessments/by-key/{context.effect_key}",
                                       allow_not_found=True, client=client)
    if response.status_code == 404:
        if not telos_api.is_error(response, KEY_NOT_FOUND_CODE, KEY_NOT_FOUND):
            raise RuntimeError(f"Telos by-key lookup returned an unexpected 404: {telos_api.detail(response)}")
        return Reconciliation(applied=False)
    data = telos_api.json_object(response)
    return Reconciliation(applied=True, output={"recorded": True, "assessment_id": str(data["assessment_id"]), "duplicate": True})


async def _post(arguments: Arguments, context: OperationContext, config: Config, client: httpx.AsyncClient | None,
                *, dry_run: bool) -> dict[str, Any]:
    params = {"dry_run": "1"} if dry_run else None
    response = await telos_api.request(config, context, "POST", "/ledger/assessments", params=params, body=dict(arguments),
                                       headers={"Idempotency-Key": context.effect_key}, client=client)
    return telos_api.json_object(response)
