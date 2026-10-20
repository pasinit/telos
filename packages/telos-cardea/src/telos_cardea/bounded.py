"""Keep handler outputs under Cardea's per-result cap.

Cardea records each call's result in its receipt and caps a result at 64 KiB; every
descriptor here declares ``max_output_bytes`` of at most :data:`MAX_OUTPUT_BYTES`, and a
handler whose output could exceed it trims its own output with :func:`fit` first, so an
oversize page or search answers with less data (flagged) instead of failing the call.
"""

import json
from typing import Any

from telos_cardea._cardea import Output

# Every descriptor's max_output_bytes is at most this (Cardea caps a receipt result at 64 KiB).
MAX_OUTPUT_BYTES = 61440
# Room for Cardea's canonical (RFC 8785) encoding differing from ours.
_MARGIN = 1024
TRUNCATION_MARK = "\n[... truncated to fit the result size limit]"


def output_bytes(output: object) -> int:
    return len(json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())


def _shrink(result: Output, container: dict[str, Any], key: str, value: list[Any] | str, budget: int) -> int:
    """Cut ``container[key]`` (originally ``value``) to its longest prefix keeping ``result`` within ``budget``."""

    def cut(n: int) -> list[Any] | str:
        return value[:n] if isinstance(value, list) else value[:n] + TRUNCATION_MARK

    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        container[key] = cut(middle)
        if output_bytes(result) <= budget:
            low = middle
        else:
            high = middle - 1
    container[key] = cut(low)
    return low


def _apply(result: Output, kind: str, path: str, budget: int, *, alone: bool) -> bool:
    """Trim ``path`` by ``kind``; with ``alone``, only when that by itself brings ``result`` within budget."""
    *parents, key = path.split(".")
    container: Any = result
    for parent in parents:
        container = container.get(parent) if isinstance(container, dict) else None
    if not isinstance(container, dict) or key not in container:
        return False
    value = container[key]
    if kind == "drop" or (kind, type(value)) in {("list", list), ("text", str)}:
        if alone:
            # The smallest this rule can make the field: removed, emptied or just the mark.
            if kind == "drop":
                del container[key]
            else:
                container[key] = [] if kind == "list" else TRUNCATION_MARK
            enough = output_bytes(result) <= budget
            container[key] = value
            if not enough:
                return False
        if kind == "drop":
            del container[key]
            return True
        if _shrink(result, container, key, value, budget) < len(value):
            return True
        container[key] = value
    return False


def fit(output: Output, *rules: tuple[str, str], limit: int = MAX_OUTPUT_BYTES) -> tuple[Output, list[str]]:
    """``output`` trimmed to ``limit`` bytes by ``rules``, until it fits.

    Each rule is ``("list", path)`` (drop trailing items), ``("text", path)`` (cut the string
    and append :data:`TRUNCATION_MARK`) or ``("drop", path)`` (remove the key); ``path`` is a
    dotted key path into nested objects. The first rule (in order) that can bring the output
    within ``limit`` on its own is the only one applied, so fields that are not the problem stay
    whole; when none can, every rule is applied in order. Returns the new output (``output`` is
    not mutated) and the paths that were trimmed; it can still exceed ``limit`` when the rules run out.
    """
    budget = limit - _MARGIN
    result: Output = json.loads(json.dumps(output))
    if output_bytes(result) <= budget:
        return result, []
    for kind, path in rules:
        if _apply(result, kind, path, budget, alone=True):
            return result, [path]
    trimmed: list[str] = []
    for kind, path in rules:
        if output_bytes(result) <= budget:
            break
        if _apply(result, kind, path, budget, alone=False):
            trimmed.append(path)
    return result, trimmed


__all__ = ["MAX_OUTPUT_BYTES", "TRUNCATION_MARK", "fit", "output_bytes"]
