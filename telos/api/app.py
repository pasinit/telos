"""Telos API — FastAPI app factory and module-level singletons.

Owns the singletons every route depends on (registry, agent runtime,
routine store, email service, ...), the lifespan that starts/stops the
routine runner and background scheduler, and the ``main()`` console-script
entry point.

Post-Phase-7K, the SSR Jinja layer is gone; this module serves only the
JSON API and mounts the Svelte SPA's built ``dist/`` tree at ``/``.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Phase 5 mitigation (MIGRATION_PLAN.md §"Risks and mitigations" #2):
# the improver's LLM calls must NOT inherit ``ANTHROPIC_BASE_URL`` —
# routing them through a proxy that's been pointed at Recordatio would
# drain Claude subscription credits. Pontifex's Anthropic backend reads
# ``ANTHROPIC_BASE_URL`` indirectly via ``anthropic.AsyncAnthropic``
# whenever the client is built without an explicit ``base_url``, which
# is the case for Telos's per-Agent ``_pf_internal``. Pop the env var
# at process start so neither path can pick it up. This is safe: the
# chat path uses ``settings.recordatio_url`` directly and never reads
# ``ANTHROPIC_BASE_URL``; Recordatio itself runs in a separate process.
_inherited_base_url = os.environ.pop("ANTHROPIC_BASE_URL", None)
if _inherited_base_url:
    logging.getLogger("telos.api").info(
        "Phase 5: dropped inherited ANTHROPIC_BASE_URL=%r so improver "
        "LLM calls hit Anthropic directly (not the proxy).",
        _inherited_base_url,
    )

import uvicorn
from fastapi import FastAPI, HTTPException
from itsdangerous import URLSafeSerializer
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from telos import Agent
from telos.api.registry import AgentRegistry
from telos.api.settings import Settings
from telos.cardea_bridge import BridgeConfig, CardeaClient, RoutineMirror
from telos.cardea_ingest import (
    IngestLedger,
    RunPoller,
    TranscriptSink,
    configure_operator_alerts,
    extract_enabled,
    on_refusal,
    on_sync_failure,
)
from telos.core.persistence import Database
from telos.goal import Goal
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.mailer import EmailConfig
from telos.observability import OTelLogFormatter, init_tracing
from telos.routines import RoutineRunner, RoutineStore
from telos.services import (
    AgentConfigSeedError,
    AgentRuntime,
    BackgroundScheduler,
    EmailNotificationService,
    ImprovementService,
    RecordatioUnavailableError,
    RoutineExecutor,
)
from telos.services.nightly import (
    FeedbackStore,
    NightlyAnalyst,
    NightlyConfig,
    NightlyFeedbackService,
    RecordatioFeedbackClient,
)
from telos.services.nightly.implementer import ImplementerReportStore, NightlyImplementer
from telos.services.nightly.synthesizer import ImprovementSynthesizer, SynthReportStore
from telos.services.routine_recall import RoutineRecall
from telos.transport.sandbox import SandboxRunner

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_DIR = Path.home() / ".telos" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s%(otel_context)s — %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(OTelLogFormatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

_file_handler = logging.handlers.TimedRotatingFileHandler(
    filename=_LOG_DIR / "telos.log",
    when="midnight",
    backupCount=30,
    encoding="utf-8",
)
_file_handler.suffix = "%Y-%m-%d"
_file_handler.setFormatter(OTelLogFormatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler, _file_handler],
)
log = logging.getLogger("telos.api")


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

settings = Settings()
_signer = URLSafeSerializer(settings.secret_key or secrets.token_hex(32))

_registry = AgentRegistry(settings.storage_root / "registry.db")
_routine_store = RoutineStore(settings.storage_root / "routines.db")
_sandbox_runner = SandboxRunner(enable_nested_sandbox=settings.sandbox_enable_nested)

_email_service = EmailNotificationService(EmailConfig(
    host=settings.smtp_host,
    port=settings.smtp_port,
    user=settings.smtp_user,
    password=settings.smtp_password,
    from_addr=settings.smtp_from,
))


_background_scheduler = BackgroundScheduler(max_workers=4, name="telos-bg")

# Nightly feedback loop. The store + service are always built (so the read
# routes work and a run can be triggered manually), but the cron is only
# scheduled when ``nightly_feedback_enabled`` — dev/test never spawns SDK
# sessions. A fresh Recordatio client is made per run (its own httpx lifecycle).
_nightly_store = FeedbackStore(settings.storage_root / "nightly" / "feedback.db")
_nightly_service = NightlyFeedbackService(
    NightlyConfig(
        storage_root=settings.storage_root,
        owner_email=settings.nightly_feedback_owner_email or None,
        # The digest links to the Recordatio memory-feedback dashboard — the
        # actionable surface where proposed clean-up ops are approved. Caddy
        # exposes Recordatio at ``/recordatio/*`` on the telos host, so the page is
        # ``<base_url>/recordatio/feedback``. (The old ``/nightly`` target was a
        # route that never existed.)
        dashboard_url=(settings.base_url.rstrip("/") + "/recordatio/feedback") if settings.base_url else "/recordatio/feedback",
        model=settings.runtime_nightly_model,
    ),
    store=_nightly_store,
    analyst=NightlyAnalyst(model=settings.runtime_nightly_model, provider=settings.telos_subscription_provider),
    make_client=lambda: RecordatioFeedbackClient(settings.recordatio_url, settings.chat_api_key or "nightly"),
    email_sink=_email_service.send_routine_notification,
)


async def _run_nightly_feedback() -> None:
    """Cron entry: analyse the day that just ended, then synthesize.

    The synthesizer runs AFTER the analysis so it sees tonight's
    judgements; a synthesis failure never fails the analysis run (it is
    proposals-only, so skipping a night just means no new proposals).
    """
    from datetime import datetime, timedelta  # noqa: PLC0415

    day = (datetime.now() - timedelta(days=1)).date()
    await _nightly_service.run_for_day(day)
    if settings.nightly_synthesizer_enabled:
        outcome = None
        try:
            outcome = await _synthesizer.run()
            log.info(
                "nightly synthesizer: staged=%d cost=$%.2f error=%s",
                outcome.staged_prompts, outcome.cost_usd, outcome.error,
            )
        except Exception:
            log.exception("nightly synthesizer failed")
        # Phase 2: turn tonight's code proposals into draft PRs. Gated
        # separately, and a failure here never fails the night — the
        # proposals are already persisted and emailed.
        if (
            settings.nightly_implementer_enabled
            and outcome is not None
            and outcome.report is not None
            and outcome.report.code_improvements
        ):
            try:
                impl = await _implementer.run(outcome.report)
                log.info(
                    "nightly implementer: prs=%d skipped=%d cost=$%.2f error=%s",
                    len(impl.prs), len(impl.skipped), impl.cost_usd, impl.error,
                )
            except Exception:
                log.exception("nightly implementer failed")


async def _catch_up_nightly_if_missed() -> None:
    """Run tonight's nightly if its window passed while we were down.

    APScheduler holds jobs in memory, so a restart AFTER the cron time (a
    deploy at 03:51 vs a 03:30 schedule) silently skips the day — misfire
    grace only covers a job that existed when the time passed. On startup we
    therefore check whether the day the cron would analyse already has a run
    and, if not (and its time has passed), fire it once in the background.
    """
    from datetime import datetime, timedelta  # noqa: PLC0415

    now = datetime.now()
    scheduled = now.replace(
        hour=settings.nightly_feedback_hour, minute=settings.nightly_feedback_minute,
        second=0, microsecond=0,
    )
    if now < scheduled:
        return  # today's window hasn't passed yet — the cron will fire it
    day = str((now - timedelta(days=1)).date())
    try:
        runs = [r for r in _nightly_store.list_runs(limit=10) if r.day == day]
    except Exception:
        log.exception("nightly catch-up: could not read run history; skipping")
        return
    def _blocks_catch_up(run) -> bool:
        """True if ``run`` means tonight is already handled.

        Two rows must NOT block, or a night is lost for good:
        - ``running`` left behind by a killed process (a deploy mid-run) —
          stale after 3h, while a genuinely in-flight run still blocks;
        - a run that died before doing any work ($0, no summary) — nothing
          was produced, so it deserves the retry. A failure that DID cost
          something still blocks, so a persistently failing night can't
          re-run on every restart.
        """
        if run.status == "running":
            try:
                started = datetime.fromisoformat(run.started_at)
            except (TypeError, ValueError):
                return True
            started = started.replace(tzinfo=None) if started.tzinfo else started
            return (datetime.utcnow() - started) < timedelta(hours=3)
        if run.status != "completed" and not run.cost_usd and not run.memory_summary:
            return False
        return True

    if any(_blocks_catch_up(r) for r in runs):
        return
    log.info("nightly catch-up: no run for %s and %02d:%02d has passed — running now",
             day, settings.nightly_feedback_hour, settings.nightly_feedback_minute)
    try:
        await _run_nightly_feedback()
    except Exception:
        log.exception("nightly catch-up run failed")


# Resolve the MCP MASTER secret up front: the agent runtime needs it to seed
# Recordatio's AgentConfig with the right per-(user, slug) ``Authorization``
# header, and the MCP host (mounted further down) needs the same value to
# validate incoming calls. ``resolve_auth_token`` reads
# ``TELOS_MCP_AUTH_TOKEN`` or falls back to a secret persisted under
# ``storage_root`` — stable across restarts, so derived tokens already baked
# into Recordatio configs keep working.
from telos.mcp.server import resolve_auth_token as _resolve_mcp_auth_token  # noqa: E402

_mcp_auth_token = _resolve_mcp_auth_token(settings.storage_root)


def _build_agent_runtime() -> AgentRuntime:
    """Construct an :class:`AgentRuntime` from the current module-level state.

    Tests that monkeypatch ``settings.storage_root`` / ``_registry`` rebuild
    the runtime via this factory so the cache reflects the patched state.
    """
    return AgentRuntime(
        registry=_registry,
        storage_root=settings.storage_root,
        recordatio_url=settings.recordatio_url,
        model=settings.runtime_model,
        subscription_provider=settings.telos_subscription_provider,
        chat_api_key=settings.chat_api_key,
        improver_api_key=settings.improver_api_key,
        utility_model=settings.runtime_utility_model,
        max_history_turns=settings.max_history_turns,
        max_context_tokens=settings.max_context_tokens,
        routine_store=_routine_store,
        sandbox_run=_sandbox_runner.run,
        send_email_fn=_email_service.send_agent_email,
        google_oauth_client_id=settings.google_oauth_client_id,
        google_oauth_client_secret=settings.google_oauth_client_secret,
        telos_mcp_url=settings.telos_mcp_url or settings.base_url,
        telos_mcp_auth_token=_mcp_auth_token,
    )


_agent_runtime = _build_agent_runtime()

# Improvement synthesizer (Phase 1, proposals-first). Built unconditionally
# (cheap; no SDK session until run); the cron only invokes it when
# ``nightly_synthesizer_enabled``. Prompt candidates are staged through the
# same AgentRuntime seam the Improver uses, so they surface in Settings.
_synth_report_store = SynthReportStore(settings.storage_root / "nightly" / "synth_reports.db")
_synthesizer = ImprovementSynthesizer(
    analyst=NightlyAnalyst(model=settings.runtime_nightly_model, provider=settings.telos_subscription_provider),
    feedback_store=_nightly_store,
    report_store=_synth_report_store,
    make_client=lambda: RecordatioFeedbackClient(settings.recordatio_url, settings.chat_api_key or "nightly"),
    storage_root=settings.storage_root,
    repo_roots={name: Path(p) for name, p in settings.synthesizer_repo_roots.items()},
    get_agent_state=_agent_runtime.ensure_improvement_state,
    email_sink=_email_service.send_routine_notification,
    owner_email=settings.nightly_feedback_owner_email or None,
    base_url=settings.base_url,
    # So the synth can map proposals against open PRs (drafts included) and not
    # re-propose a fix already in flight — same slugs/token the implementer uses.
    repo_slugs=settings.nightly_implementer_repo_slugs,
    github_token=settings.github_token,
)

# Nightly implementer (Phase 2, draft PRs). Built unconditionally like the
# synthesizer; the cron only invokes it when ``nightly_implementer_enabled``
# and tonight's report actually carries code proposals. Its outcomes (PRs,
# skips, cap-dropped proposals) are persisted to a sidecar DB so they survive
# past the digest email/logs.
_implementer_report_store = ImplementerReportStore(settings.storage_root / "nightly" / "implementer_reports.db")
_implementer = NightlyImplementer(
    provider=settings.telos_subscription_provider,
    model=settings.runtime_nightly_model,
    repo_roots={name: Path(p) for name, p in settings.synthesizer_repo_roots.items()},
    repo_slugs=settings.nightly_implementer_repo_slugs,
    github_token=settings.github_token,
    report_store=_implementer_report_store,
    email_sink=_email_service.send_routine_notification,
    owner_email=settings.nightly_feedback_owner_email or None,
    max_prs=settings.nightly_implementer_max_prs,
    worker_url=settings.nightly_worker_url,
    worker_token=settings.nightly_worker_token,
    workspace_root=settings.nightly_worker_workspace_root,
)


_improvement_service = ImprovementService(
    scheduler=_background_scheduler,
    # Polling trigger reads ``(user_id, slug)`` pairs every cycle.
    # Cached-only is correct: an agent that hasn't been touched since
    # startup cannot accumulate new chat messages without going through
    # ``AgentRuntime.get`` (which caches it), so the cache is the
    # complete set of "potentially active" agents.
    list_pairs_fn=_agent_runtime.all_cached_pairs,
    get_agent_state_fn=_agent_runtime.get_improvement_state,
    recordatio_url=settings.recordatio_url,
    recordatio_api_key=settings.chat_api_key or None,
    improve_every=settings.improve_every,
)


def _agent_display_name(user_id: str, slug: str) -> str:
    meta = _registry.get(user_id, slug)
    return meta["name"] if meta else slug


_routine_executor = RoutineExecutor(
    agent_runtime=_agent_runtime,
    email_service=_email_service,
    get_user_email_fn=_registry.get_user_email,
    get_agent_name_fn=_agent_display_name,
    base_url=settings.base_url,
    storage_root=settings.storage_root,
    recall_fn=RoutineRecall(
        lambda: RecordatioFeedbackClient(settings.recordatio_url, settings.chat_api_key or "nightly"),
        settings.routine_recall_queries,
    ),
)


async def _resolve_agent(user_id: str, slug: str) -> Agent:
    """Resolve an agent via the runtime, mapping build failures to clean
    HTTP errors instead of raw 500s.

    This is the single shared mapping used by BOTH the ``Depends()``
    callables in :mod:`telos.api.deps` and the module-level
    ``_get_agent`` / ``_get_housekeeper`` helpers the route modules call
    directly (chat history, costs, settings page, routines, agents) —
    the SPA hydrates ``GET /api/agents/{slug}/turns`` before the first
    chat, so the cold-start seed failure hits those helpers first.

    - ``LookupError`` → 404 (unknown slug).
    - ``RecordatioUnavailableError`` → 503 + ``Retry-After`` (Recordatio down or
      slow during the first-build config seed; transient — the agent was
      NOT cached, so the next request retries the seed).
    - ``AgentConfigSeedError`` → 503 fail-closed (bad credentials /
      version conflict / missing telos MCP URL; needs operator action).
    """
    try:
        return await _agent_runtime.get(user_id, slug)
    except LookupError:
        raise HTTPException(status_code=404, detail="Agent not found") from None
    except RecordatioUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Agent backend temporarily unavailable; please retry",
            headers={"Retry-After": "5"},
        ) from None
    except AgentConfigSeedError:
        raise HTTPException(
            status_code=503,
            detail="Agent backend configuration failed; contact the administrator",
        ) from None


async def _get_housekeeper(user_id: str) -> Agent:
    return await _resolve_agent(user_id, HOUSEKEEPER_SLUG)


async def _get_agent(user_id: str, slug: str) -> Agent:
    return await _resolve_agent(user_id, slug)


# ---------------------------------------------------------------------------
# Lifespan + app factory
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise tracing + start the routine runner on startup; tear
    both down on shutdown.

    Tracing is opt-in via ``Settings.otel_exporter_otlp_endpoint`` —
    when unset, ``init_tracing`` is a no-op and ``FastAPIInstrumentor``
    is skipped, so dev / test runs without a collector incur zero
    overhead. Drains ``_background_scheduler`` on shutdown so in-flight
    improvement runs finish before the process exits.
    """
    if init_tracing(settings.otel_exporter_otlp_endpoint, settings.otel_service_name):
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz")
    bridge = _cardea_bridge()
    runner = RoutineRunner(
        _routine_store,
        _routine_executor.execute_with_email,
        owned_elsewhere=bridge.owned_elsewhere if bridge else None,
    )
    runner.start()
    if bridge is not None:
        asyncio.create_task(_sync_cardea(bridge, _cardea_poller(bridge)))  # noqa: RUF006
    app.state.routine_runner = runner
    _agent_runtime.routine_runner = runner
    # Nightly feedback cron shares the routine runner's AsyncIOScheduler so
    # the async job runs on the event loop (the SDK sessions are async).
    if settings.nightly_feedback_enabled:
        runner._scheduler.add_job(  # noqa: SLF001 (reuse the running scheduler)
            _run_nightly_feedback, "cron",
            hour=settings.nightly_feedback_hour, minute=settings.nightly_feedback_minute,
            id="nightly_feedback", replace_existing=True, misfire_grace_time=3600,
        )
        log.info("nightly feedback scheduled at %02d:%02d",
                 settings.nightly_feedback_hour, settings.nightly_feedback_minute)
        # A deploy that lands after the cron time would otherwise skip the
        # night entirely (in-memory jobstore, nothing to misfire). Check on
        # startup and run it once if the window passed unserved.
        asyncio.create_task(_catch_up_nightly_if_missed())  # noqa: RUF006
    # Start the message-count polling thread for the improver (phase 5).
    # Stop order on shutdown: trigger first so no new improver tasks land
    # on the scheduler while we drain it, then the routine runner, then
    # the background scheduler.
    _improvement_service.start()
    try:
        yield
    finally:
        _improvement_service.shutdown()
        runner.stop()
        app.state.routine_runner = None
        _agent_runtime.routine_runner = None
        _background_scheduler.shutdown(wait=True)


def _agent_tools(user_id: str, slug: str) -> list[str]:
    meta = _registry.get(user_id, slug) or {}
    tools = meta.get("tools") or []
    return list(json.loads(tools) if isinstance(tools, str) else tools)


def _agent_prompt(user_id: str, slug: str) -> str:
    """The agent's live system prompt: its approved ``current.txt``, else the registry seed."""
    path = settings.storage_root / user_id / slug / "prompts" / "current.txt"
    if path.exists():
        return path.read_text()
    meta = _registry.get(user_id, slug) or {}
    return Goal(name=meta.get("name", slug), description=meta.get("description", ""), system_prompt=meta.get("system_prompt", "")).system_prompt


def _cardea_bridge() -> RoutineMirror | None:
    """Routine mirroring into Cardea whenever ``TELOS_CARDEA_URL`` and ``TELOS_CARDEA_TOKEN_FILE`` are set.

    Active even in mode off, so the sync removes the triggers a previous shadow/live run installed.
    Its alert hooks email the operator (``TELOS_CARDEA_OPERATOR_EMAIL``, else the nightly owner).
    """
    config = BridgeConfig.from_env()
    if not config.configured:
        if config.wants_mirroring:
            log.error("TELOS_CARDEA_MODE=%s needs TELOS_CARDEA_URL and TELOS_CARDEA_TOKEN_FILE", config.mode)
        return None
    configure_operator_alerts(
        _email_service.send_routine_notification,
        os.getenv("TELOS_CARDEA_OPERATOR_EMAIL") or settings.nightly_feedback_owner_email,
    )
    return RoutineMirror(
        CardeaClient(config), _routine_store.list_all_enabled, _agent_tools, agent_prompt=_agent_prompt,
        on_sync_failure=on_sync_failure, on_refusal=on_refusal,
    )


def _cardea_poller(bridge: RoutineMirror) -> RunPoller:
    """Ingests Cardea run outcomes into Telos (see telos.cardea_ingest)."""
    transcripts = TranscriptSink(settings.recordatio_url, settings.chat_api_key) if extract_enabled() else None
    return RunPoller(
        bridge.client, _routine_store, IngestLedger(Database(settings.storage_root / "routines.db")),
        settings.storage_root, _routine_executor, transcripts=transcripts,
    )


async def _sync_cardea(bridge: RoutineMirror, poller: RunPoller | None = None, every: float = 60.0) -> None:
    """Keep Cardea triggers equal to mirrorable routines; edits converge within a minute."""
    while True:
        try:
            outcomes = await asyncio.to_thread(bridge.sync)
            changed = {k: v for k, v in outcomes.items() if v != "unchanged"}
            if changed:
                log.info("cardea routine sync: %s", changed)
        except Exception:
            log.exception("cardea routine sync failed")
        if poller is not None:
            try:
                if ingested := await asyncio.to_thread(poller.poll):
                    log.info("cardea run ingestion: %s", ingested)
            except Exception:
                log.exception("cardea run ingestion failed")
        await asyncio.sleep(every)


app = FastAPI(title="telos-api", lifespan=lifespan)
app.state.routine_runner = None


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, bool]:
    return {"ok": True}


# Include all route modules.
from telos.api.routes import routers  # noqa: E402  (after `app` is defined)

for _r in routers:
    app.include_router(_r)


# ---------------------------------------------------------------------------
# MCP tool host — Phase 2 of the Telos+Recordatio migration
# ---------------------------------------------------------------------------
#
# Exposes Telos's existing tools as MCP tools over HTTP under
# ``/mcp/{user_id}/{slug}`` so Recordatio can call them remotely. The host
# wraps the *same* ``_dispatch_tool_call`` path the in-process Agent
# uses — tool implementations are unchanged.
#
# Wire the host *before* the SPA catch-all mount so its ``/mcp/...``
# routes win over the SPA's wildcard. The host adds its own lifespan
# step to the FastAPI lifespan; the FastMCP session manager must be
# running before any request reaches it.
from pontifex import Pontifex  # noqa: E402
from pontifex.codex_completion import CodexCompletion  # noqa: E402

from telos.mcp.server import mount_mcp_host  # noqa: E402
from telos.tool_wiring import build_tools  # noqa: E402
from telos.transport.browser import agencies as _agencies  # noqa: E402
from telos.transport.browser.fetcher import fetch as _web_fetch  # noqa: E402
from telos.transport.browser.fetcher import fetch_json as _fetch_json  # noqa: E402
from telos.transport.browser.fetcher import fetch_raw as _fetch_raw  # noqa: E402
from telos.transport.browser.fetcher import fetch_rendered as _fetch_rendered  # noqa: E402
from telos.transport.browser.images import is_private_url as _is_private_url  # noqa: E402
from telos.transport.llm.pontifex import complete as _pontifex_complete  # noqa: E402
from telos.transport.search.base import search as _web_search  # noqa: E402

_mcp_pontifex = CodexCompletion(model=settings.codex_model) if settings.telos_subscription_provider == "codex" else Pontifex(
    model=settings.utility_model,
    api_key=settings.improver_api_key or None,
    base_url=settings.llm_base_url,
)


async def _mcp_llm_complete(*, messages, system=None, tools=None, max_tokens=4096):
    """Pontifex-backed utility LLM the routine-naming tool needs.

    Bound to the deployment's utility model + improver API key — the
    MCP host never routes through Recordatio (which itself talks to the
    chat models). Same shape ``tool_wiring.build_tools`` already uses
    on the agent side, just bound up here so we can build the tool
    catalogue at module load.
    """
    return await _pontifex_complete(
        _mcp_pontifex,
        messages=messages,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
    )


async def _mcp_memory_recall(query, *, user_id, slug, k=10):
    """Targeted memory-search probe backing the ``memory_recall`` tool.

    Reuses the same Recordatio retrieval endpoint the nightly analyst calls
    (:meth:`RecordatioFeedbackClient.memory_search`), scoped to the calling
    agent's ``slug`` and authenticated with the deployment's chat key —
    same construction as the nightly feedback client above. A fresh
    client per call keeps this stateless.
    """
    async with RecordatioFeedbackClient(
        settings.recordatio_url, settings.chat_api_key or "nightly",
    ) as client:
        return await client.memory_search(user_id, query, slug=slug, k=k)


_mcp_tools = build_tools(
    sandbox_run=_sandbox_runner.run,
    web_search=_web_search,
    web_fetch=_web_fetch,
    agency_search=_agencies.make_search_dispatch(
        fetch_html=_fetch_raw, fetch_json=_fetch_json, fetch_html_js=_fetch_rendered),
    is_private_url=_is_private_url,
    llm_complete=_mcp_llm_complete,
    routine_store=_routine_store,
    # Late binding: the live ``RoutineRunner`` is created by the lifespan
    # AFTER this catalogue is built, so the routine tools resolve it from
    # ``app.state`` at call time (same source the HTTP routine routes
    # read via ``telos.api.state.routine_runner``). By the time Recordatio
    # can reach the MCP host the lifespan has started, so create/delete
    # over MCP (un)schedule jobs on the real scheduler — previously this
    # was ``routine_runner=None`` and deleted routines kept firing while
    # newly activated ones never started until a restart.
    routine_runner_provider=lambda: getattr(app.state, "routine_runner", None),
    send_email_fn=_email_service.send_agent_email,
    memory_recall=_mcp_memory_recall,
    registry=_registry,
    google_oauth_client_id=settings.google_oauth_client_id,
    google_oauth_client_secret=settings.google_oauth_client_secret,
    get_agent_fn=_agent_runtime.get,
)


_mcp_host = mount_mcp_host(
    app,
    tools=_mcp_tools,
    storage_root=settings.storage_root,
    auth_token=_mcp_auth_token,
    # Scope each /mcp/{user}/{slug} surface's tools/list to that agent's
    # own registry whitelist (agents.tools) — invocation gating unchanged.
    registry=_registry,
)


# SPA static-serve mount — always on now that SSR is gone. Mounts
# ``telos-web/dist/`` at ``/`` and serves the SPA shell for any
# unmatched path. MUST run after every ``include_router(...)`` above
# and after the MCP mount so ``/mcp/...`` resolves to the host.
from telos.api import spa as _spa  # noqa: E402

_spa.register(app)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    uvicorn.run("telos.api.app:app", host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
