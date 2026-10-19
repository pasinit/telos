"""Deploy step: PUT a new system prompt to Recordatio's AgentConfig API.

Pre-Phase-5 the deploy step mutated the local in-process ``Agent``
directly (``Agent.approve()`` swapped ``self._system_prompt`` and wrote
to disk). Post-Phase-5 the authoritative ``AgentConfig`` lives in
Recordatio (``GET/PUT /v1/agents/{user_id}/{slug}/config``), so the deploy
step is now an HTTP round-trip.

The PUT carries an optimistic-concurrency ``version``; Recordatio returns
``409 Conflict`` if the stored row has been updated since we read it.
The plan calls for one refetch+retry on 409, then abort and log if it
still conflicts. That race window is small in practice (the only other
writer is the improver itself, and improver runs are serialised
per-agent by ``MessageCountTrigger``), but the retry exists to absorb
any out-of-band PUTs from administrative tooling.

See MIGRATION_PLAN.md §"Phase 5 — Improver migration":

    Deploy step: replace in-process agent mutation with
    PUT /v1/agents/{user_id}/{slug}/config carrying the version read at
    the start of the improvement cycle. On 409, refetch and retry the
    diff.
"""

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


# Reasonable timeouts for the small JSON payloads we exchange. The
# Recordatio side is local SQLite — anything longer than a few seconds
# means the box is in trouble and we'd rather fail fast and retry on the
# next trigger fire than block a scheduler thread.
DEFAULT_TIMEOUT_SECONDS: float = 15.0


class ConfigDeployFailed(RuntimeError):
    """Raised by :func:`deploy_candidate_to_recordatio` on terminal failure.

    Covers: network errors that exhausted retries, ``404`` (agent not
    configured in Recordatio — needs a first-time seed), and the
    second-409 case where even a refetch + retry hits a version race.
    The improver swallows this and logs; the next trigger cycle will
    re-run the pipeline against the latest config.
    """


class RecordatioConfigClient:
    """Minimal HTTP client for Recordatio's AgentConfig endpoints.

    Kept narrow on purpose: ``get`` returns the JSON payload (the
    server already round-trips it through ``AgentConfig.to_dict``) and
    ``put`` returns ``(payload_or_None, status_code)`` so the caller can
    branch on ``409`` without parsing the body twice. We don't model
    ``AgentConfig`` as a typed dataclass here — Recordatio's schema may
    grow new optional fields, and a passthrough dict means a newer
    Recordatio doesn't break an older telos.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        # Trim trailing slash so callers can pass either form. We build
        # absolute paths internally.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        # When the caller supplies an httpx.Client (tests, or a long-lived
        # production client) we don't close it on teardown — its lifetime
        # is owned upstream.
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def __enter__(self) -> "RecordatioConfigClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── headers ─────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            # Recordatio's ``_api_key_from_request`` accepts either an
            # ``x-api-key`` header or a Bearer token; we use Bearer for
            # consistency with the chat path.
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # ── operations ──────────────────────────────────────────────────

    def get(self, user_id: str, slug: str) -> dict[str, Any] | None:
        """Read the AgentConfig for ``(user_id, slug)``.

        Returns ``None`` if Recordatio responds with 404 (agent not yet
        configured). Any other non-2xx status raises.
        """
        url = f"{self._base_url}/v1/agents/{user_id}/{slug}/config"
        resp = self._client.get(url, headers=self._headers(), timeout=self._timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise ConfigDeployFailed(
                f"GET {url} returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def put(
        self, user_id: str, slug: str, config: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, int]:
        """Update the AgentConfig.

        Returns ``(body, status_code)`` where ``body`` is the JSON
        payload of the response (or None on parse error). The caller is
        expected to branch on ``status_code``: ``200`` is success,
        ``409`` is a version mismatch (retry path), anything else is a
        terminal error and should be raised.
        """
        url = f"{self._base_url}/v1/agents/{user_id}/{slug}/config"
        headers = self._headers() | {"Content-Type": "application/json"}
        resp = self._client.put(
            url, headers=headers, json=config, timeout=self._timeout,
        )
        try:
            body = resp.json()
        except Exception:
            body = None
        return body, resp.status_code


def deploy_candidate_to_recordatio(
    *,
    client: RecordatioConfigClient,
    user_id: str,
    slug: str,
    new_system_prompt: str,
    initial_config: dict[str, Any],
) -> dict[str, Any]:
    """PUT the new ``system_prompt`` to Recordatio with one 409 retry.

    Args:
        client:             Connected :class:`RecordatioConfigClient`.
        user_id, slug:      The target (user, agent).
        new_system_prompt:  The improver's winning candidate prompt.
        initial_config:     The AgentConfig dict the caller read at the
                            *start* of the improvement cycle. We
                            preserve every field except ``system_prompt``
                            and carry forward its ``version`` on the
                            first PUT.

    Returns:
        The AgentConfig dict Recordatio returns on success (includes the
        bumped ``version``).

    Raises:
        ConfigDeployFailed: on 404, on non-409 / non-200 status, or
            on a second 409 after the refetch + retry.

    The "version read at the start of the cycle" is the contract from
    MIGRATION_PLAN.md §"Phase 5". We pass it through the initial_config
    rather than re-reading inside this helper so the cycle's view of
    "current" stays consistent with whatever the improver was working
    against.
    """
    # 1) First attempt — use whatever ``initial_config`` carried.
    proposed = dict(initial_config)
    proposed["system_prompt"] = new_system_prompt
    body, status = client.put(user_id, slug, proposed)
    if status == 200:
        log.info(
            "improver deploy: PUT succeeded for %s/%s (new version=%s)",
            user_id, slug, (body or {}).get("version"),
        )
        return body or {}

    if status == 404:
        # Agent not yet configured in Recordatio. Improvement deploy can't
        # bootstrap — that's a first-class seed problem, not a transient
        # race. Bail out cleanly so the next trigger fire isn't a busy
        # loop on the same agent.
        raise ConfigDeployFailed(
            f"agent config not found in Recordatio for {user_id}/{slug}; "
            f"seed it before the improver can deploy",
        )

    if status != 409:
        raise ConfigDeployFailed(
            f"PUT failed for {user_id}/{slug}: {status} {body!r}",
        )

    # 2) 409 → version mismatch. Refetch and re-apply the prompt against
    #    the fresh row, then PUT exactly once more.
    log.info(
        "improver deploy: 409 on first PUT for %s/%s — refetching and retrying",
        user_id, slug,
    )
    refetched = client.get(user_id, slug)
    if refetched is None:
        raise ConfigDeployFailed(
            f"agent config disappeared between 409 and refetch for {user_id}/{slug}",
        )

    proposed = dict(refetched)
    proposed["system_prompt"] = new_system_prompt
    body, status = client.put(user_id, slug, proposed)
    if status == 200:
        log.info(
            "improver deploy: PUT succeeded on retry for %s/%s (new version=%s)",
            user_id, slug, (body or {}).get("version"),
        )
        return body or {}

    # 3) Still conflicting (or any other failure). Per the plan: abort
    #    that cycle and log; the next trigger fire will redo the diff
    #    against the new config.
    raise ConfigDeployFailed(
        f"PUT for {user_id}/{slug} still failing after refetch retry: "
        f"{status} {body!r}",
    )
