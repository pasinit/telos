"""FastAPI ``Settings`` model for the Telos web UI.

Loaded from environment variables (or ``.env``); the rest of the UI layer
imports the singleton ``settings`` from :mod:`telos.api.app`.
"""
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings.

    Note on MCP auth (not a field here, read directly from the env by
    :func:`telos.mcp.server.resolve_auth_token`): ``TELOS_MCP_AUTH_TOKEN``
    is the MASTER secret from which per-(user_id, slug) MCP bearer tokens
    are HMAC-derived. When unset, a generated secret is persisted to
    ``<storage_root>/mcp_master_secret`` so it stays stable across
    restarts — tokens already seeded into Recordatio agent configs would
    otherwise go stale and 401 forever.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    recordatio_url: str = "http://localhost:8787"
    storage_root: Path = Path.home() / ".telos"
    host: str = "0.0.0.0"
    port: int = 7433
    telos_subscription_provider: Literal["codex", "claude"] = "claude"
    # Empty selects the installed Codex CLI default; old model settings are
    # retained for explicitly selecting Claude again.
    codex_model: str = ""
    model: str = "qwen2.5:14b"

    @property
    def runtime_model(self) -> str:
        return self.codex_model if self.telos_subscription_provider == "codex" else self.model

    @property
    def runtime_utility_model(self) -> str:
        return self.codex_model if self.telos_subscription_provider == "codex" else self.utility_model

    @property
    def runtime_nightly_model(self) -> str:
        return self.codex_model if self.telos_subscription_provider == "codex" else self.nightly_feedback_model

    llm_base_url: str = "http://localhost:11434/v1"
    chat_api_key: str = ""           # LLM key for main chat (Telos→Recordatio→provider)
    improver_api_key: str = ""       # LLM key for the auto-improvement loop (Telos→provider direct)
    secret_key: str = ""
    # Prompt generation — falls back to main model settings if not set
    prompt_gen_model: str | None = None
    prompt_gen_backend: str | None = None
    prompt_gen_base_url: str | None = None
    # Cheap/fast model for utility tasks (signals, summaries, naming, improvement)
    utility_model: str = "claude-haiku-4-5-20251001"
    # Context window
    max_history_turns: int = 30
    max_context_tokens: int = 50_000
    # Improvement loop — trigger an improver pass when N new chat
    # messages have accrued per agent since the last fire. Default 50
    # per MIGRATION_PLAN §"Phase 5 — Improver migration"; the pre-5
    # event-driven path defaulted to 20, but the new polling-based path
    # also pays for a Recordatio GET+PUT on every fire so the higher value
    # is a better latency/quality tradeoff. Used by
    # ``telos.services.improvement.ImprovementService``.
    improve_every: int = 50
    # Sandbox runtime. Docker deployments set this true so bubblewrap uses
    # sandbox-runtime's nested-container mode.
    sandbox_enable_nested: bool = False
    # Email notifications
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    base_url: str = ""  # public URL for email links, e.g. https://telos.example.com
    # Externally-reachable base URL of THIS telos process — Recordatio uses it
    # to call back into the telos MCP host at ``{telos_mcp_url}/mcp/...``.
    # Falls back to ``base_url`` when empty (the two are usually the same;
    # split here so deployments that route email through a different
    # vanity domain can keep them separate).
    telos_mcp_url: str = ""
    # Public URL of the deployment, used by the auth boundary (PR 7C) to
    # decide whether to set the ``Secure`` attribute on the auth cookie.
    # Read from the ``PUBLIC_HOST`` env var. HTTPS deploys get
    # ``Secure=True``; HTTP localhost dev (the default empty value)
    # gets ``Secure=False``.
    public_host: str = ""
    # Google OAuth (per-user Docs/Sheets connect flow)
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""  # e.g. https://telos.example.com/oauth/google/callback
    # Public URL for Recordatio's user-facing dashboard, used to build the
    # "open in knowledge graph" link from the recalled-memories panel. The
    # internal ``recordatio_url`` (e.g. http://recordatio:8787) is unreachable from
    # the user's browser, so the link is omitted unless this is set.
    recordatio_public_url: str = ""
    # Observability — see telos/observability.py. Empty endpoint = no-op
    # (no collector); set e.g. http://localhost:4317 for the local Jaeger
    # sidecar (`make jaeger`).
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "telos"
    # Nightly feedback loop (telos.services.nightly). Disabled by default so
    # dev/test never spawns SDK sessions; the deployment opts in. The analyst
    # runs a standalone Claude SDK session — subscription-billed via
    # ``CLAUDE_CODE_OAUTH_TOKEN`` (read from the env by the bundled CLI, not a
    # field here). ``owner_email`` gets the nightly digest; empty ⇒ no digest.
    routine_recall_queries: dict[str, list[str]] = {}
    # Optional cost ceiling on how many of a routine's configured reference
    # queries run per execution. ``None`` (default) runs the whole configured
    # list so a routine can cover application-status AND profile/competency
    # evidence in one run; set an int only to cap cost.
    routine_recall_max_queries: int | None = None
    # Per-routine (keyed ``user_id/slug``) status-event terms that float
    # matching records to the front of each query's candidate list. Empty ⇒ the
    # global job-status defaults (``STATUS_EVENT_TERMS``). Lets a non-job
    # routine float its own evidence terms (e.g. competency phrases).
    routine_recall_status_terms: dict[str, list[str]] = {}
    nightly_feedback_enabled: bool = False
    nightly_feedback_hour: int = 3  # local-time hour for the 03:30 cron
    nightly_feedback_minute: int = 30
    nightly_feedback_model: str = "claude-opus-4-8"
    nightly_feedback_owner_email: str = ""
    # Improvement synthesizer (Phase 1, proposals-first): a second SDK
    # session after the nightly analysis that turns the judgements into
    # staged prompt candidates + a code/UX improvement report. Requires
    # nightly_feedback_enabled. ``synthesizer_repo_roots`` maps repo name to
    # a READ-ONLY checkout path inside the container (compose mounts
    # ~/telos and ~/recordatio at /repos/*) so code proposals cite real files.
    nightly_synthesizer_enabled: bool = False
    synthesizer_repo_roots: dict[str, str] = {
        "telos": "/repos/telos",
        "Recordatio": "/repos/recordatio",
    }
    # Nightly implementer (Phase 2): a third SDK session that turns the
    # synthesizer's code proposals into DRAFT pull requests (throwaway
    # clones of the repo roots; pushes nightly/* branches with
    # ``github_token``; never merges). Requires nightly_synthesizer_enabled.
    # The token must have push access to every repo in ``repo_slugs``.
    nightly_implementer_enabled: bool = False
    nightly_implementer_max_prs: int = 10
    nightly_worker_url: str = ""
    nightly_worker_token: str = ""
    nightly_worker_workspace_root: Path = Path("/nightly-work")
    nightly_implementer_repo_slugs: dict[str, str] = {
        "telos": "pasinit/telos",
        "Recordatio": "pasinit/recordatio",
    }
    github_token: str = ""  # env GITHUB_TOKEN (already set in the deployment)


__all__ = ["Settings"]
