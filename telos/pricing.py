"""Per-token pricing for LLM cost tracking.

Mirrors the price table in
``Recordatio/src/tpasini/recordatio/proxy/server.py`` so that Telos and Recordatio
agree on cost numbers when Recordatio doesn't already return ``cost_usd``.

Prices are USD per 1M tokens — ``(input_per_m, output_per_m)``.
"""
import logging
from decimal import Decimal

log = logging.getLogger(__name__)

# Per-token prices in USD. (input, output) per 1M tokens.
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4-6":              (15.0, 75.0),
    "claude-sonnet-4-6":            (3.0,  15.0),
    "claude-sonnet-4-6-20251001":   (3.0,  15.0),
    "claude-haiku-4-5":             (0.80, 4.0),
    "claude-haiku-4-5-20251001":    (0.80, 4.0),
    # OpenAI
    "gpt-4o":                       (2.50, 10.0),
    "gpt-4o-mini":                  (0.15, 0.60),
    "gpt-4.1":                      (2.0,  8.0),
    "gpt-4.1-mini":                 (0.40, 1.60),
    "gpt-4.1-nano":                 (0.10, 0.40),
    "o3":                           (2.0,  8.0),
    "o3-mini":                      (1.10, 4.40),
    "o4-mini":                      (1.10, 4.40),
    # Gemini
    "gemini-2.5-flash":             (0.15, 0.60),
    "gemini-2.5-pro":               (1.25, 10.0),
    "gemini-2.0-flash":             (0.10, 0.40),
}


class PricingTable:
    """Owns the warn-once set so unknown-model warnings don't spam logs.

    A single module-level default instance (``_default_table``) backs the
    public ``cost_for`` function. Tests can construct an isolated instance
    or call ``reset_warnings`` on the default.
    """

    def __init__(self) -> None:
        self._warned: set[str] = set()

    def cost_for(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Return estimated cost in USD for the given model + token counts.

        Returns 0.0 for unknown models — better to undercount than crash.
        """
        if not model:
            return 0.0
        # Strip Recordatio's `provider/model` routing prefix when present
        # (e.g. `anthropic/claude-sonnet-4-6` → `claude-sonnet-4-6`).
        bare = model.split("/", 1)[1] if "/" in model else model
        prices = _PRICE_TABLE.get(bare)
        if prices is None:
            # Try a stripped/dated variant: some callers pass `claude-haiku-4-5`,
            # some pass `claude-haiku-4-5-20251001`.
            # Attempt one fallback by trimming a trailing `-YYYYMMDD` suffix.
            if "-" in bare:
                prices = _PRICE_TABLE.get(bare.rsplit("-", 1)[0])
            if prices is None:
                if model not in self._warned:
                    self._warned.add(model)
                    log.warning(
                        "no pricing entry for model %r — recording cost as $0.00. "
                        "Add it to telos/pricing.py:_PRICE_TABLE.",
                        model,
                    )
                return 0.0
        input_price, output_price = prices
        cost = (
            max(int(input_tokens), 0) * input_price
            + max(int(output_tokens), 0) * output_price
        ) / 1_000_000
        return float(Decimal(str(cost)).quantize(Decimal("0.000001")))

    def reset_warnings(self) -> None:
        """Drop the warn-once dedup set (for tests)."""
        self._warned.clear()


# Module-level default — backs the thin ``cost_for`` wrapper so callers
# don't need to know about the class.
_default_table = PricingTable()


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated cost in USD for the given model + token counts.

    Thin wrapper around ``PricingTable.cost_for`` on the module-level
    default instance.
    """
    return _default_table.cost_for(model, input_tokens, output_tokens)
