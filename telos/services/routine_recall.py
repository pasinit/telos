"""Priority retrieval for routines with explicitly configured reference needs."""
import json
from collections.abc import Callable
from datetime import UTC, datetime


class RoutineRecall:
    def __init__(self, make_client: Callable, queries: dict[str, list[str]], *, max_chars: int = 12000):
        self._make_client = make_client
        self._queries = queries
        self._max_chars = max_chars

    async def __call__(self, user_id: str, slug: str) -> dict | None:
        queries = self._queries.get(f"{user_id}/{slug}", [])[:3]
        if not queries:
            return None
        as_of = datetime.now(UTC).isoformat()
        evidence = {"as_of": as_of, "queries": queries, "records": [], "errors": [], "truncated": False}
        batches = []
        evidence["query_counts"] = []
        async with self._make_client() as client:
            for query in queries:
                units = []
                try:
                    result = await client.memory_search(user_id, query, slug=slug, as_of=as_of, mode="production", k=10)
                    units = result.get("results", [])[:10]
                except Exception as exc:
                    evidence["errors"].append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
                batches.append(units)
                # Reserve the maximum counter widths before allocating records.
                evidence["query_counts"].append({"query": query, "retrieved": len(units),
                                                 "included": len(units), "omitted": len(units)})

        candidates = []
        for units in batches:
            records = []
            for unit in units:
                content = str(unit.get("content", ""))
                normalized = " ".join(content.split()).casefold()
                if not normalized:
                    continue
                record = {k: unit.get(k) for k in ("id", "content", "asserted_at", "valid_from", "valid_until")}
                # Repeated summaries are not votes. Different observation dates
                # (and conflicting content) remain separate evidence.
                key = (normalized, *(record[k] for k in ("asserted_at", "valid_from", "valid_until")))
                if len(content) > 4000:
                    record["content"] = content[:4000]
                    record["truncated"] = True
                records.append((key, record))
            candidates.append(records)

        evidence["truncated"] = False
        remaining = max(0, self._max_chars - len(json.dumps(evidence)))
        share = remaining // len(queries)
        seen = set()
        included = [0] * len(queries)

        def include(index, candidate, allowance):
            nonlocal remaining
            key, record = candidate
            if key in seen:
                return 0
            # Two separator characters also conservatively cover the first item.
            cost = len(json.dumps(record)) + 2
            if cost > min(allowance, remaining):
                return 0
            evidence["records"].append(record)
            seen.add(key)
            included[index] += 1
            remaining -= cost
            return cost

        # Each query gets an equal reserved share before any query can use
        # spare capacity. Keep records whole rather than cut away dated context.
        for index, records in enumerate(candidates):
            allowance = share
            for candidate in records:
                allowance -= include(index, candidate, allowance)
        # Redistribute unused shares in rank order, one candidate per query.
        for rank in range(max(map(len, candidates), default=0)):
            for index, records in enumerate(candidates):
                if rank < len(records):
                    include(index, records[rank], remaining)

        for counts, count in zip(evidence["query_counts"], included):
            # Included counts physical records attributed to this query;
            # omitted includes duplicates, blank results and budget exclusions.
            counts["included"] = count
            counts["omitted"] = counts["retrieved"] - count
        evidence["truncated"] = any(
            key not in seen for records in candidates for key, _ in records
        ) or any(record.get("truncated") for record in evidence["records"])
        return evidence
