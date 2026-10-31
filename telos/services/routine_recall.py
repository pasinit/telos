"""Priority retrieval for routines with explicitly configured reference needs."""
import json
from collections.abc import Callable
from datetime import UTC, datetime

# A broad routine probe (e.g. "submitted job applications subsequent status
# updates") returns a swarm of low-information "submitted"/"no response"
# summaries that differ only by date, so they are NOT collapsed by the
# exact-duplicate dedup below. Two mechanisms keep a status-event needle from
# being buried under that swarm:
#   * PER_QUERY_K pulls a deep enough candidate pool that a tail record ranked
#     beyond the old k=10 window still enters consideration (the memory-search
#     endpoint caps k at 100);
#   * STATUS_EVENT_TERMS floats records that carry a concrete status event to
#     the front of each query's candidate list so they are admitted into the
#     reserved char-budget share before the summary swarm can consume it.
PER_QUERY_K = 25
STATUS_EVENT_TERMS = ("interview", "shortlist", "offer", "reject")


def _is_status_event(normalized_content: str, terms: tuple[str, ...] = STATUS_EVENT_TERMS) -> bool:
    return any(term in normalized_content for term in terms)


class RoutineRecall:
    def __init__(self, make_client: Callable, queries: dict[str, list[str]], *, max_chars: int = 12000,
                 per_query_k: int = PER_QUERY_K, max_queries: int | None = None,
                 status_terms: dict[str, list[str]] | None = None):
        self._make_client = make_client
        self._queries = queries
        self._max_chars = max_chars
        self._per_query_k = per_query_k
        # A routine's reference queries are entirely operator-configured, so the
        # whole configured list runs by default. ``max_queries`` is an optional
        # cost ceiling, not a silent truncation: a routine can probe
        # application-status AND profile/competency evidence in one run.
        self._max_queries = max_queries
        # Which records float to the front of a query's candidate list (see the
        # sort below). Per-routine (keyed ``user_id/slug``) so a routine can
        # float its own evidence terms instead of only the global job-status
        # defaults; falls back to ``STATUS_EVENT_TERMS`` when unset.
        self._status_terms = status_terms or {}

    async def __call__(self, user_id: str, slug: str) -> dict | None:
        key = f"{user_id}/{slug}"
        queries = self._queries.get(key, [])
        if self._max_queries is not None:
            queries = queries[:self._max_queries]
        if not queries:
            return None
        configured_terms = self._status_terms.get(key)
        status_terms = tuple(term.casefold() for term in configured_terms) if configured_terms else STATUS_EVENT_TERMS
        as_of = datetime.now(UTC).isoformat()
        evidence = {"as_of": as_of, "queries": queries, "records": [], "errors": [], "truncated": False}
        batches = []
        evidence["query_counts"] = []
        async with self._make_client() as client:
            for query in queries:
                units = []
                try:
                    result = await client.memory_search(user_id, query, slug=slug, as_of=as_of,
                                                        mode="production", k=self._per_query_k)
                    units = result.get("results", [])[:self._per_query_k]
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
            # Stable sort: records carrying a status event (interview, offer,
            # rejection, …) are admitted into this query's reserved share before
            # the swarm of low-information dated summaries. Rank order is
            # preserved within each group, so this only re-prioritises when a
            # status event would otherwise be evicted by the swarm.
            records.sort(key=lambda kr: 0 if _is_status_event(kr[0][0], status_terms) else 1)
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
