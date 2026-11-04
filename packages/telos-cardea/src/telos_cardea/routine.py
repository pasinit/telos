"""``telos.routine_context`` read operation: what Telos injects into its own routine runs.

Returns the invocation date and timezone, the delivery history, the ledger summary,
the reserved recall text and (given the trigger's ``routine_id``) the routine's own
recent results for the calling agent, so a Cardea run starts from the same context a
Telos run would. Cardea calls it as a context operation before the agent's first turn.
The routine id comes from the Telos-authored trigger template; Telos still only answers
for a routine of the agent named by the operation context.
"""

import httpx

from telos_cardea import telos_api
from telos_cardea._cardea import Arguments, OperationContext, Output
from telos_cardea.bounded import fit
from telos_cardea.config import Config, load_config

TEXT_BLOCKS = ("delivery_history", "ledger_summary", "reserved_recall", "recent_runs")
# Telos waits up to 45 s (TELOS_INTERNAL_RECALL_TIMEOUT) for the reserved recall
# before disclosing it as unavailable, so the call needs that plus a margin; it
# stays below the descriptor's timeout_seconds (60) so Cardea sees our error.
TIMEOUT_SECONDS = 55


async def routine_context(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                          client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``telos.routine_context``."""
    routine_id = arguments.get("routine_id")
    params = {"routine_id": routine_id} if isinstance(routine_id, str) and routine_id else None
    response = await telos_api.request(config or load_config(), context, "GET", "/routine-context", params=params, client=client,
                                      timeout=TIMEOUT_SECONDS)
    body = telos_api.json_object(response)
    # Over Cardea's result cap, the largest block is cut first.
    largest = sorted((k for k in TEXT_BLOCKS if isinstance(body.get(k), str)), key=lambda k: len(body[k]), reverse=True)
    out, trimmed = fit(body, *(("text", key) for key in largest))
    if trimmed:
        out["truncated"] = trimmed
    return out
