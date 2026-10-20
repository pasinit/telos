"""``telos.routine_context`` read operation: what Telos injects into its own routine runs.

Returns the invocation date and timezone, the delivery history, the ledger summary
and the reserved recall text for the calling agent, so a Cardea run starts from the
same context a Telos run would. Cardea calls it as a context operation before the
agent's first turn.
"""

import httpx

from telos_cardea import telos_api
from telos_cardea._cardea import Arguments, OperationContext, Output
from telos_cardea.config import Config, load_config


async def routine_context(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                          client: httpx.AsyncClient | None = None) -> Output:
    """Entry point ``telos.routine_context``."""
    response = await telos_api.request(config or load_config(), context, "GET", "/routine-context", client=client)
    return telos_api.json_object(response)
