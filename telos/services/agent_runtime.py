"""``AgentRuntime`` — the only place that constructs :class:`telos.Agent` instances.

Was inlined as ``_get_agent`` / ``_get_housekeeper`` plus a module-level
``_agents`` cache in :mod:`telos.api.server`. Pulling that glue into a service
makes the constructor surface explicit (no more ``agent._registry = …``
setter injection after construction) and lets the cache be unit-tested in
isolation.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from telos.agent import Agent
from telos.core.ports import SandboxRun
from telos.domain import AgentImprovementState, DelegationGraph, RunContext
from telos.goal import Goal
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.mcp import McpToolManager
from telos.routines import RoutineStore
from telos.services.recordatio_agent_seed import build_agent_config_payload, seed_agent_config_if_missing

log = logging.getLogger(__name__)


class AgentConfigSeedError(RuntimeError):
    """Recordatio AgentConfig seed failed for a non-transient reason.

    Raised when Recordatio answered but rejected the request (401 bad
    credentials, 409 version conflict, 5xx) or when the seed payload
    cannot be built at all (``RuntimeError`` for a missing telos MCP
    URL). Retrying without operator intervention won't help, so the
    API layer fails closed with a 503 rather than a raw 500.

    Subclasses ``RuntimeError`` so pre-existing callers/tests that
    caught the bare re-raise keep working.
    """


class RecordatioUnavailableError(RuntimeError):
    """Recordatio was unreachable (or timed out) during the config seed.

    Transient by definition — connect errors / timeouts at cold start
    must not poison anything: the agent is NOT cached, and the API
    layer surfaces a clean retryable 503 so the next request retries
    the seed from scratch.
    """


class _Registry(Protocol):
    """Subset of ``AgentRegistry`` that ``AgentRuntime`` reads from."""

    def get(self, user_id: str, slug: str) -> dict[str, Any] | None: ...
    def list_for_user(self, user_id: str) -> list[dict[str, Any]]: ...


class AgentRuntime:
    """Constructs and caches ``Agent`` instances, one per ``(user_id, slug)``.

    The runtime is the sole owner of ``Agent`` construction — any code that
    needs an agent goes through :meth:`get` or :meth:`get_housekeeper`. Tools
    that need to look up sibling agents (``delegate_to`` / ``coordinate``)
    receive :meth:`get` as their ``get_agent_fn`` callback at construction
    time.

    Args:
        registry:                  Registry holding agent metadata (name,
                                   description, prompt, tools, user-scoped
                                   settings).
        storage_root:              Root directory for per-agent storage.
        recordatio_url:               Recordatio HTTP base URL.
        model:                     Default chat model (forwarded to Recordatio).
        chat_api_key:              Bearer token forwarded to Recordatio.
        improver_api_key:          API key for the Pontifex-backed internal
                                   LLM jobs (improver, classifier, …).
        utility_model:             Cheap/fast model for utility calls.
        max_history_turns:         Conversation history window per agent.
        max_context_tokens:        Max prompt tokens before context trim.
        routine_store:             Shared per-process routine store.
        send_email_fn:             Callback for the ``send_email`` tool.
        google_oauth_client_id:    Deployment-level Google OAuth client id.
        google_oauth_client_secret:Deployment-level Google OAuth client secret.
        on_improvement_ready:      Per-candidate callback (surfaced via the
                                   ``/status`` route).
    """

    def __init__(
        self,
        *,
        registry: _Registry,
        storage_root: Path,
        recordatio_url: str,
        model: str,
        chat_api_key: str | None,
        improver_api_key: str | None,
        utility_model: str,
        max_history_turns: int,
        max_context_tokens: int,
        routine_store: RoutineStore,
        sandbox_run: SandboxRun | None = None,
        send_email_fn: Callable[[str, str, str], Any] | None = None,
        google_oauth_client_id: str = "",
        google_oauth_client_secret: str = "",
        on_improvement_ready: Callable[[Any], None] | None = None,
        delegation_graph: DelegationGraph | None = None,
        telos_mcp_url: str = "",
        telos_mcp_auth_token: str | None = None,
        subscription_provider: str = "claude",
    ) -> None:
        self._subscription_provider = subscription_provider
        self._registry = registry
        self._storage_root = storage_root
        self._recordatio_url = recordatio_url
        self._model = model
        self._chat_api_key = chat_api_key or None
        self._improver_api_key = improver_api_key or None
        self._utility_model = utility_model
        self._max_history_turns = max_history_turns
        self._max_context_tokens = max_context_tokens
        self._routine_store = routine_store
        self._sandbox_run = sandbox_run
        self._send_email_fn = send_email_fn
        self._google_oauth_client_id = google_oauth_client_id
        self._google_oauth_client_secret = google_oauth_client_secret
        self._on_improvement_ready = on_improvement_ready
        # One shared delegation graph per runtime — the ``ContextVar``
        # inside propagates through ``await`` so nested ``delegate_to`` /
        # ``coordinate`` calls see the parent chain. Tests pass an isolated
        # instance.
        self._delegation_graph = delegation_graph or DelegationGraph()
        # Externally-reachable base URL of this telos process — Recordatio
        # uses this to call back into telos's MCP host at
        # ``{telos_mcp_url}/mcp/{user_id}/{slug}``. Empty string means "no
        # seed will succeed": the seed step raises ``RuntimeError`` rather
        # than silently writing an empty ``mcp_servers`` list (which would
        # produce a recordatio-mode agent with zero working tools).
        self._telos_mcp_url = telos_mcp_url
        self._telos_mcp_auth_token = telos_mcp_auth_token
        # Live ``RoutineRunner`` reference — set by the FastAPI lifespan in
        # ``telos.api.app`` AFTER the runner is built (the runner depends on
        # the runtime's executor, so it can't exist at runtime construction
        # time). Agents read this lazily through ``routine_runner_provider``
        # below, so the latest runner is always picked up at tool-call time.
        # ``None`` outside production (tests, one-shot CLI). Replaces the
        # legacy ``telos.routines._active_runner`` module singleton (Phase 5F).
        self.routine_runner: Any = None
        # (user_id, slug) → Agent
        self._cache: dict[tuple[str, str], Agent] = {}

    # ------------------------------------------------------------------
    # Per-call context construction
    # ------------------------------------------------------------------

    def make_context(
        self,
        user_id: str,
        *,
        image_url: str | None = None,
        is_routine: bool = False,
        correlation_id: str | None = None,
    ) -> RunContext:
        """Build a per-call :class:`RunContext` bound to this runtime's
        :class:`DelegationGraph` instance.

        Callers (server routes, routine runner, tools that fan out) use this
        so every turn in a given Telos process shares the same graph and
        cycle detection works across nested ``delegate_to`` calls.
        """
        return RunContext(
            user_id=user_id,
            delegation_graph=self._delegation_graph,
            image_url=image_url,
            is_routine=is_routine,
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------
    # Public lookups
    # ------------------------------------------------------------------

    async def get(self, user_id: str, slug: str) -> Agent:
        """Return (or build and cache) the agent for ``(user_id, slug)``.

        The housekeeper is built through this same path as a specialist —
        its registry row is auto-seeded by ``AgentRegistry.get`` on
        first access (so the ``_resolve_slug`` lookup below always
        succeeds for the reserved slug), and ``is_housekeeper`` is
        derived from the slug inside :meth:`_build_specialist`. There is
        no separate housekeeper code path.

        Raises ``LookupError`` if the slug is unknown for this user.
        """
        slug, meta = self._resolve_slug(user_id, slug)
        key = (user_id, slug)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        agent = await self._build_specialist(user_id, slug, meta)
        self._cache[key] = agent
        return agent

    async def get_housekeeper(self, user_id: str) -> Agent:
        """Compatibility shim — equivalent to ``get(user_id, HOUSEKEEPER_SLUG)``.

        Kept because several callers reach in by name (``ui/app.py``'s
        ``_get_housekeeper`` shim, the ``/`` route, the ``create-agent``
        notification path). The shim routes through ``_build_specialist``
        like any other agent, so the ``routine_runner_provider`` lambda
        and other build-time wiring apply uniformly.
        """
        return await self.get(user_id, HOUSEKEEPER_SLUG)

    # ------------------------------------------------------------------
    # Cache helpers (used by /clear and /delete handlers in server.py)
    # ------------------------------------------------------------------

    def evict(self, user_id: str, slug: str) -> Agent | None:
        """Drop the cached agent for ``(user_id, slug)`` and return it.

        Returns ``None`` if no such agent was cached.
        """
        return self._cache.pop((user_id, slug), None)

    def clear(self) -> None:
        """Drop every cached agent (used by tests to reset state)."""
        self._cache.clear()

    def cached(self, user_id: str, slug: str) -> Agent | None:
        """Return the cached agent for ``(user_id, slug)``, or ``None``."""
        return self._cache.get((user_id, slug))

    def get_improvement_state(
        self, user_id: str, slug: str,
    ) -> AgentImprovementState | None:
        """Return the ``AgentImprovementState`` view for a cached agent.

        Used as the ``get_agent_state_fn`` callback handed to
        :class:`telos.services.improvement.ImprovementService`. Returns
        ``None`` when the agent is not in cache — events should always
        come from agents that just ran a turn (and are therefore
        cached), so a ``None`` return is a bug worth logging in the
        consumer.
        """
        agent = self._cache.get((user_id, slug))
        if agent is None:
            return None
        return agent.improvement_state()

    async def ensure_improvement_state(
        self, user_id: str, slug: str,
    ) -> AgentImprovementState | None:
        """Return the improvement-state view, building the agent on demand.

        Unlike :meth:`get_improvement_state` (cache-only), this builds and
        caches the agent when it is not resident, so callers can obtain a
        state for any agent that exists in the registry — even one that has
        not served a live turn since startup. Building constructs the agent
        (config, prompt, MCP wiring) but runs no turn, so there are no
        side effects. Returns ``None`` only when the slug is unknown for the
        user. Used by the nightly synthesizer to stage prompt candidates
        reliably regardless of cache warmth.
        """
        try:
            agent = await self.get(user_id, slug)
        except LookupError:
            return None
        return agent.improvement_state()

    def all_cached(self) -> list[Agent]:
        """Return every currently-cached agent (any user, any slug)."""
        return list(self._cache.values())

    def all_cached_pairs(self) -> list[tuple[str, str]]:
        """Return ``(user_id, slug)`` for every cached agent.

        Used by the improvement subsystem's polling trigger (phase 5)
        to know which conversation logs to inspect. Snapshot semantics:
        the returned list is a copy, so iteration won't observe later
        cache mutations.
        """
        return list(self._cache.keys())

    # ------------------------------------------------------------------
    # Internal: name-fallback resolution + specialist build steps
    # ------------------------------------------------------------------

    def _resolve_slug(self, user_id: str, slug: str) -> tuple[str, dict[str, Any]]:
        """Translate *slug* into ``(slug, meta)``, accepting the agent name
        as a fallback. The LLM occasionally passes the name instead of the
        slug; matching by lowercase name preserves the prior behaviour.
        """
        meta = self._registry.get(user_id, slug)
        if meta:
            return slug, meta
        for agent_meta in self._registry.list_for_user(user_id):
            if agent_meta.get("name", "").lower() == slug.lower():
                return agent_meta["slug"], agent_meta
        raise LookupError(f"Agent not found: user={user_id!r}, slug={slug!r}")

    async def _build_specialist(self, user_id: str, slug: str, meta: dict[str, Any]) -> Agent:
        # Parse the stored whitelist as-is. An empty list means "no tools" —
        # the registry seeds the housekeeper with every built-in name via
        # ``ensure_housekeeper`` so it doesn't depend on a None fallback.
        agent_tools: list[str] | None = json.loads(meta.get("tools") or "[]")
        goal = Goal(
            name=meta["name"],
            description=meta["description"],
            system_prompt=meta["system_prompt"],
        )
        # ``is_housekeeper`` is a behavior flag (enables ``delegate_to`` /
        # ``create_agent`` tools, suppresses Recordatio input-side extraction).
        # It's orthogonal to registry presence — the housekeeper IS now a
        # registry row — and is derived from the reserved slug at build time.
        is_housekeeper = slug == HOUSEKEEPER_SLUG
        recordatio_config = self._build_recordatio_config(user_id, slug)
        agent = Agent(
            goal=goal,
            user_id=user_id,
            slug=slug,
            model=self._model,
            api_key=self._chat_api_key,
            improver_api_key=self._improver_api_key,
            recordatio_url=self._recordatio_url,
            storage_root=self._storage_root,
            on_improvement_ready=self._on_improvement_ready or (lambda _candidate: None),
            tools=agent_tools,
            routine_store=self._routine_store,
            utility_model=self._utility_model,
            subscription_provider=self._subscription_provider,
            is_housekeeper=is_housekeeper,
            max_history_turns=self._max_history_turns,
            max_context_tokens=self._max_context_tokens,
            send_email_fn=self._send_email_fn,
            registry=self._registry,
            get_agent_fn=self.get,
            google_oauth_client_id=self._google_oauth_client_id,
            google_oauth_client_secret=self._google_oauth_client_secret,
            routine_runner=self.routine_runner,
            sandbox_run=self._sandbox_run,
            recordatio_config=recordatio_config,
        )
        await self._attach_mcp(agent, agent_tools)
        await self._seed_recordatio_config_if_needed(user_id, slug)
        return agent

    def _build_recordatio_config(self, user_id: str, slug: str) -> dict[str, Any] | None:
        """Build the base per-query AgentConfig telos sends to Recordatio each
        turn (the agent merges its live system prompt in — see
        ``Agent._live_recordatio_config``).

        Reuses the same builder as the (now legacy) seed path, so the
        per-query payload and any still-seeded config stay identical in
        shape. Returns ``None`` on any failure (e.g. telos MCP URL unset in
        dev/tests) so the turn falls back to Recordatio's stored config rather
        than erroring.
        """
        try:
            return build_agent_config_payload(
                registry=self._registry,
                user_id=user_id,
                slug=slug,
                model=self._model,
                telos_mcp_url=self._telos_mcp_url,
                telos_mcp_auth_token=self._telos_mcp_auth_token,
            )
        except Exception as exc:
            log.warning(
                "could not build per-query recordatio config for %s/%s: %s; "
                "falling back to Recordatio's stored config",
                user_id, slug, exc,
            )
            return None

    async def _seed_recordatio_config_if_needed(
        self, user_id: str, slug: str,
    ) -> None:
        """Create Recordatio's AgentConfig on first build of an agent.

        Idempotent: GETs first, only PUTs on 404. On failure we raise
        so the caller (``_build_specialist`` → ``get``) aborts BEFORE the
        partially-built agent is cached. Caching a half-built agent on a
        transient seed failure would pin a broken instance until process
        restart or explicit eviction; surfacing the error lets the next
        ``get`` call retry the seed cleanly.

        Failures are classified for the API layer (see ``deps.get_agent``):

        - transport errors (connect refused/timeout/read timeout) →
          :class:`RecordatioUnavailableError` — transient, retryable;
        - HTTP rejections (401/409/5xx) and payload-construction
          ``RuntimeError`` (missing telos MCP URL) →
          :class:`AgentConfigSeedError` — fail closed.

        The seed client authenticates the same way the chat path does
        (``Agent.__init__`` sends ``Authorization: Bearer {chat_api_key}``)
        — Recordatio's config endpoints 401 without a credential.

        TODO(perf): the GET+PUT round trip is on the cold-start path
        for the first chat turn after a process restart — measured at
        ~20s in production. Move to a background warm-up task or stream
        the PUT in parallel with the first agent build once the
        observability for "seed in progress" is in place.
        """
        headers: dict[str, str] = {}
        if self._chat_api_key:
            headers["Authorization"] = f"Bearer {self._chat_api_key}"
        try:
            async with httpx.AsyncClient(
                base_url=self._recordatio_url, headers=headers, timeout=10.0,
            ) as client:
                await seed_agent_config_if_missing(
                    client,
                    registry=self._registry,
                    user_id=user_id,
                    slug=slug,
                    model=self._model,
                    telos_mcp_url=self._telos_mcp_url,
                    telos_mcp_auth_token=self._telos_mcp_auth_token,
                )
        except httpx.TransportError as exc:
            # ConnectError / ConnectTimeout / ReadTimeout / … — Recordatio is
            # down or slow right now. Warning (not exception): expected at
            # cold start, retried on the next request.
            log.warning(
                "recordatio unreachable during config seed for %s/%s: %s",
                user_id, slug, exc,
            )
            raise RecordatioUnavailableError(
                f"recordatio unreachable during config seed for {user_id}/{slug}: {exc}",
            ) from exc
        except (httpx.HTTPStatusError, RuntimeError) as exc:
            detail = str(exc)
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 401
                and not self._chat_api_key
            ):
                # Recordatio rejected an *anonymous* request — the operator
                # never configured a credential. Say so, actionably.
                detail += (
                    " — telos sent no credential; set CHAT_API_KEY / "
                    "chat_api_key so telos can authenticate to Recordatio"
                )
            log.exception(
                "recordatio config seed failed for %s/%s: %s", user_id, slug, detail,
            )
            raise AgentConfigSeedError(
                f"recordatio config seed failed for {user_id}/{slug}: {detail}",
            ) from exc
        except Exception:
            log.exception("recordatio config seed failed for %s/%s", user_id, slug)
            raise

    async def _attach_mcp(self, agent: Agent, agent_tools: list[str] | None) -> None:
        """Connect MCP servers for any ``mcp:`` tools the agent has enabled."""
        if not agent_tools:
            return
        mcp_tools = [t for t in agent_tools if t.startswith("mcp:")]
        if not mcp_tools:
            return
        mcp = McpToolManager(workspace=str(agent.workspace))
        failed = await mcp.connect_all(mcp_tools)
        if failed:
            log.warning("removing failed MCP tools: %s", failed)
            agent.tools_list = [t for t in agent_tools if t not in failed]
        agent.set_mcp(mcp)


# Type alias for callers (e.g. tool dispatch) that want to type their
# ``get_agent_fn`` parameter without depending on ``AgentRuntime``.
GetAgentFn = Callable[[str, str], Awaitable[Agent]]
