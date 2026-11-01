"""``ImprovementService`` — message-count-triggered self-improvement.

Pre-Phase-5 (5D) this service subscribed to the in-process
``TurnComplete`` event bus and counted events per ``(user_id, slug)`` to
decide when to fire the improver. Phase 5 of the Telos+Recordatio migration
replaces both halves of that wiring:

- The trigger now polls chat persistence directly via
  :class:`telos.improvement.MessageCountTrigger` — independent of the
  in-process chat coroutine, so it keeps working once Phase 3 swaps the
  chat turn to run inside Recordatio.
- The auto-deploy step now writes the winning candidate's
  ``system_prompt`` to Recordatio's AgentConfig API
  (:func:`telos.improvement.deploy_candidate_to_recordatio`) instead of
  mutating the local in-process ``Agent``. The local ``ApprovalManager``
  still stages low-delta candidates for the user-review UI; only the
  auto-deploy path's terminal write target moves to Recordatio.

Construction order at runtime is unchanged from 5D:

1. ``BackgroundScheduler`` is built in ``api/app.py``'s lifespan.
2. ``ImprovementService`` is built with the scheduler, a callable that
   enumerates the ``(uid, slug)`` pairs to poll (typically over the
   ``AgentRuntime`` cache), and a callable that resolves a pair to its
   :class:`AgentImprovementState`.
3. ``start()`` spawns the polling thread. ``shutdown()`` stops it. The
   caller wires both into the FastAPI lifespan.

The pre-5D in-Agent counter logic ("seed from ``count() - 1`` on first
event so cross-restart math matches the legacy seed-at-init timing") is
gone — the new trigger seeds from ``current_count`` on first observation
and only fires on *delta*, which is the semantically cleaner property
once the chat path no longer publishes a per-turn event.

``AUTO_DEPLOY_THRESHOLD`` is still re-exported here for backwards
compatibility with callers that import it from
``telos.services.improvement``; the canonical value lives in
``telos.improver`` alongside the ``CandidatePrompt.score_delta`` it
gates.
"""

import logging
from collections.abc import Callable
from typing import Any

from telos.domain import AgentImprovementState
from telos.improvement import (
    ConfigDeployFailed,
    MessageCountTrigger,
    RecordatioConfigClient,
    deploy_candidate_to_recordatio,
)
from telos.improvement.triggers import DEFAULT_POLL_INTERVAL_SECONDS, DEFAULT_THRESHOLD
from telos.improver import AUTO_DEPLOY_THRESHOLD
from telos.services.memory_reconcile import AGENT_SLUG as MEMORY_STEWARD_SLUG
from telos.services.scheduler import BackgroundScheduler, SchedulerShutdownError

log = logging.getLogger(__name__)

# Re-exports unchanged — see module docstring.
__all__ = [
    "AUTO_DEPLOY_THRESHOLD",
    "AgentImprovementState",
    "GetAgentStateFn",
    "ImprovementService",
    "ListAgentPairsFn",
]


GetAgentStateFn = Callable[[str, str], AgentImprovementState | None]
ListAgentPairsFn = Callable[[], list[tuple[str, str]]]
ConfigClientFactory = Callable[[], RecordatioConfigClient]

# Agents whose prompt is a fixed contract, never rewritten by the improver: the
# memory steward's judging rules mirror what Recordatio verifies.
NO_IMPROVEMENT_SLUGS = frozenset({MEMORY_STEWARD_SLUG})


class ImprovementService:
    """Polling-trigger-driven improvement scheduler with Recordatio deploy.

    Args:
        scheduler:           Background pool that runs improver tasks
                             off the request loop.
        list_pairs_fn:       Returns the ``(user_id, slug)`` pairs to
                             poll. Production passes
                             ``lambda: [(a.user_id, a.slug) for a in
                             runtime.all_cached()]``.
        get_agent_state_fn:  Resolves a pair to its
                             :class:`AgentImprovementState`. ``None``
                             means the agent has been evicted; the
                             trigger silently skips.
        recordatio_url:         Base URL of the Recordatio service. Used to
                             read/write AgentConfig.
        recordatio_api_key:     Bearer token for Recordatio (typically
                             ``settings.chat_api_key``).
        improve_every:       Message-count threshold (default 50, per
                             MIGRATION_PLAN §"Phase 5"). Configurable
                             via ``Settings.improve_every``.
        poll_interval:       Seconds between polls. Default 60s.
        config_client_factory:
                             Test seam — overrides the default
                             ``RecordatioConfigClient(recordatio_url, ...)``
                             constructor. Lets tests inject a fake
                             without monkeypatching httpx.

    The trigger is built in ``__init__`` but the polling thread is NOT
    started until ``start()`` is called. Tests therefore can drive
    ``poll_once`` synchronously via ``trigger.poll_once()``.
    """

    def __init__(
        self,
        scheduler: BackgroundScheduler,
        *,
        list_pairs_fn: ListAgentPairsFn,
        get_agent_state_fn: GetAgentStateFn,
        recordatio_url: str,
        recordatio_api_key: str | None = None,
        improve_every: int = DEFAULT_THRESHOLD,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        config_client_factory: ConfigClientFactory | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._get_agent_state = get_agent_state_fn
        self._recordatio_url = recordatio_url
        self._recordatio_api_key = recordatio_api_key or None
        self._config_client_factory = config_client_factory or self._default_client_factory

        self._trigger = MessageCountTrigger(
            list_pairs_fn=lambda: [p for p in list_pairs_fn() if p[1] not in NO_IMPROVEMENT_SLUGS],
            get_state_fn=get_agent_state_fn,
            on_threshold_fn=self._on_threshold,
            threshold=improve_every,
            poll_interval=poll_interval,
            name="improver-trigger",
        )

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the polling thread.

        Idempotent; safe to call from FastAPI lifespan startup.
        """
        self._trigger.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop polling. Does NOT stop the scheduler — the caller owns it."""
        self._trigger.stop(timeout=timeout)

    # ── introspection (tests / debugging) ───────────────────────────

    @property
    def trigger(self) -> MessageCountTrigger:
        """Expose the trigger so tests can call ``poll_once()`` directly.

        Production code does not reach in.
        """
        return self._trigger

    # ── threshold handler ──────────────────────────────────────────

    def _on_threshold(
        self,
        user_id: str,
        slug: str,
        state: AgentImprovementState,
    ) -> None:
        """Called by the trigger when a pair crosses ``improve_every``.

        Reads the current ``AgentConfig`` from Recordatio (captures the
        version for the deploy round-trip) and submits the improver
        run to the background scheduler. The actual research → propose
        → judge → deploy pipeline runs on the worker thread.
        """
        # Snapshot the current Recordatio AgentConfig. We capture this
        # *before* submitting the improver so the version stays "the
        # version at the start of the cycle" per plan §"Phase 5". A
        # `None` config means the agent isn't seeded in Recordatio; we
        # could still run the improver and just log on deploy, but
        # bailing here avoids wasting a research run we can't ship.
        try:
            initial_config = self._fetch_initial_config(user_id, slug)
        except Exception:
            log.exception(
                "ImprovementService: failed to fetch Recordatio config for %s/%s; "
                "skipping cycle",
                user_id, slug,
            )
            return
        if initial_config is None:
            log.warning(
                "ImprovementService: no Recordatio AgentConfig for %s/%s; "
                "cannot deploy improvements — seed it via PUT first",
                user_id, slug,
            )
            return

        self._submit_improver_run(state, user_id, slug, initial_config)

    def _fetch_initial_config(self, user_id: str, slug: str) -> dict[str, Any] | None:
        with self._config_client_factory() as client:
            return client.get(user_id, slug)

    def _default_client_factory(self) -> RecordatioConfigClient:
        return RecordatioConfigClient(self._recordatio_url, api_key=self._recordatio_api_key)

    # ── scheduler submission ────────────────────────────────────────

    def _submit_improver_run(
        self,
        state: AgentImprovementState,
        user_id: str,
        slug: str,
        initial_config: dict[str, Any],
    ) -> None:
        """Hand the improver run to the background scheduler.

        The callable runs on a worker thread with its own asyncio loop;
        the improver's tool-call loop awaits ``Pontifex`` calls there
        without sharing the request loop.

        Per MIGRATION_PLAN §"Phase 5":

            Improver's own LLM calls keep using Pontifex DIRECTLY.
            Do NOT route them through Recordatio; that would drain
            subscription credits.

        The ``Improver`` instance already binds the per-agent Pontifex
        client (``Agent._pf_internal``) at Agent-construction time with
        ``backend="anthropic"``, no ``base_url`` override. Pontifex's
        Anthropic backend honours an explicit ``base_url`` if set on
        the client, falls back to ``anthropic.AsyncAnthropic``'s
        default (which reads ``ANTHROPIC_BASE_URL`` from the
        environment) otherwise. We rely on telos NOT setting
        ``ANTHROPIC_BASE_URL`` in production; Phase 5's risk-mitigation
        note in MIGRATION_PLAN tracks this.
        """
        task_name = f"improver:{user_id}/{slug}"

        async def _run() -> None:
            try:
                current_prompt = state.get_current_prompt()
            except Exception:
                log.exception(
                    "ImprovementService: failed to read current prompt for %s/%s",
                    user_id, slug,
                )
                return
            try:
                candidate = await state.improver.run(current_prompt)
            except Exception:
                log.exception(
                    "ImprovementService: improver run raised for %s/%s",
                    user_id, slug,
                )
                return
            if candidate is None:
                return

            # Always stage in the local ApprovalManager so the
            # user-review UI can surface the candidate. Low-delta
            # candidates stop here.
            try:
                state.approval.stage(candidate)
            except Exception:
                log.exception(
                    "ImprovementService: failed to stage candidate %s for %s/%s",
                    candidate.sha, user_id, slug,
                )
                return

            if candidate.score_delta <= AUTO_DEPLOY_THRESHOLD:
                log.info(
                    "staged improvement %s for %s/%s review (delta=%.1f%%)",
                    candidate.sha, user_id, slug,
                    candidate.score_delta * 100,
                )
                return

            # Auto-deploy: PUT the new prompt to Recordatio with the
            # version captured at the start of the cycle. One refetch
            # retry on 409, then abort and log. The legacy in-process
            # ``Agent`` mutation is intentionally NOT called here —
            # Phase 3 will swap chat to read from Recordatio, so any
            # transient gap between deploy and chat picking up the new
            # prompt closes when that ships.
            try:
                with self._config_client_factory() as client:
                    deploy_candidate_to_recordatio(
                        client=client,
                        user_id=user_id,
                        slug=slug,
                        new_system_prompt=candidate.content,
                        initial_config=initial_config,
                    )
            except ConfigDeployFailed:
                log.exception(
                    "ImprovementService: auto-deploy failed for %s/%s "
                    "(candidate %s)",
                    user_id, slug, candidate.sha,
                )
                return
            except Exception:
                log.exception(
                    "ImprovementService: unexpected error deploying "
                    "candidate %s for %s/%s",
                    candidate.sha, user_id, slug,
                )
                return

            log.info(
                "auto-deployed improvement %s for %s/%s (delta=%.1f%%) to Recordatio",
                candidate.sha, user_id, slug,
                candidate.score_delta * 100,
            )

        try:
            self._scheduler.submit(_run, name=task_name)
        except SchedulerShutdownError:
            log.info(
                "ImprovementService: scheduler shutting down; dropped run for %s/%s",
                user_id, slug,
            )
