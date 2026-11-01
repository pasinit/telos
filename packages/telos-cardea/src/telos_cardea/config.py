"""Deployment configuration: environment variables over an optional private TOML file.

Secrets never come from operation arguments. Environment names match Telos's own
(``SMTP_HOST``, ``RECORDATIO_URL``, ``SEARCH_ENGINE``…) so one ``.env`` serves both.
The file (``TELOS_CARDEA_CONFIG``, default ``/etc/telos-cardea/config.toml``) is
read on every call, so rotating a credential needs no restart; it must not be
group- or world-readable.
"""

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/telos-cardea/config.toml")


class ConfigError(RuntimeError):
    """Configuration is missing or unsafe."""


@dataclass(frozen=True)
class SmtpConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_addr: str = ""

    @property
    def sender(self) -> str:
        return self.from_addr or self.user


@dataclass(frozen=True)
class RecordatioScope:
    user_id: str
    slug: str


@dataclass(frozen=True)
class Config:
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    brevo_api_key: str = ""
    brevo_api_url: str = "https://api.brevo.com/v3"
    recordatio_url: str = "http://localhost:8787"
    recordatio_api_key: str = ""
    # Key Recordatio lets call only its memory-reconcile routes (memory.reconcile_*).
    recordatio_reconcile_key: str = ""
    # Pairs per memory.reconcile_candidates batch (the agent's limit is capped at this).
    reconcile_batch_size: int = 10
    # "<namespace>/<agent_id>" (or a bare namespace) -> the Recordatio user/agent whose memory it may read.
    recordatio_scopes: dict[str, RecordatioScope] = field(default_factory=dict)
    # Telos's internal API (ledger, routine context) and the JWT-SVID file authenticating to it.
    telos_url: str = ""
    telos_token_file: str = ""
    search_engine: str = "duckduckgo"
    serper_key: str = ""
    serpapi_key: str = ""
    unblocker_url: str = ""
    unblocker_timeout: int = 75
    # Allow listing fetches to fall back to a headless browser (patchright).
    browser_fallback: bool = False

    def recordatio_scope(self, namespace: str, agent: str | None) -> RecordatioScope | None:
        """The scope for ``(namespace, agent)``; a bare-namespace key is the backward-compatible fallback."""
        if agent and (scope := self.recordatio_scopes.get(f"{namespace}/{agent}")) is not None:
            return scope
        return self.recordatio_scopes.get(namespace)


def _read_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{path} must not be accessible by group or others (chmod 600)")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: dict[str, str] | None = None) -> Config:
    """Merge the private file (if any) with environment variables; env wins."""
    env = dict(os.environ) if env is None else env
    data = _read_file(Path(env.get("TELOS_CARDEA_CONFIG", str(DEFAULT_CONFIG_PATH))))
    smtp = data.get("smtp", {})
    brevo = data.get("brevo", {})
    recordatio = data.get("recordatio", {})
    search = data.get("search", {})
    fetch = data.get("fetch", {})
    telos = data.get("telos", {})
    reconcile = data.get("reconcile", {})

    def pick(name: str, section: dict[str, Any], key: str, default: Any = "") -> Any:
        return env.get(name) or section.get(key, default)

    scopes = {ns: RecordatioScope(user_id=str(v["user_id"]), slug=str(v["slug"])) for ns, v in recordatio.get("scopes", {}).items()}
    return Config(
        smtp=SmtpConfig(
            host=pick("SMTP_HOST", smtp, "host"),
            port=int(pick("SMTP_PORT", smtp, "port", 587)),
            user=pick("SMTP_USER", smtp, "user"),
            password=pick("SMTP_PASSWORD", smtp, "password"),
            from_addr=pick("SMTP_FROM", smtp, "from"),
        ),
        brevo_api_key=pick("BREVO_API_KEY", brevo, "api_key"),
        brevo_api_url=pick("BREVO_API_URL", brevo, "api_url", "https://api.brevo.com/v3"),
        recordatio_url=pick("RECORDATIO_URL", recordatio, "url", "http://localhost:8787"),
        recordatio_api_key=pick("CHAT_API_KEY", recordatio, "api_key"),
        recordatio_reconcile_key=pick("RECORDATIO_RECONCILE_KEY", recordatio, "reconcile_key"),
        reconcile_batch_size=max(1, int(pick("TELOS_CARDEA_RECONCILE_BATCH", reconcile, "batch_size", 10))),
        recordatio_scopes=scopes,
        telos_url=pick("TELOS_INTERNAL_URL", telos, "url"),
        telos_token_file=pick("TELOS_INTERNAL_TOKEN_FILE", telos, "token_file"),
        search_engine=pick("SEARCH_ENGINE", search, "engine", "duckduckgo").lower(),
        serper_key=pick("SERPER_KEY", search, "serper_key"),
        serpapi_key=pick("SERPAPI_KEY", search, "serpapi_key"),
        unblocker_url=pick("TELOS_UNBLOCKER_URL", fetch, "unblocker_url"),
        unblocker_timeout=int(pick("TELOS_UNBLOCKER_TIMEOUT", fetch, "unblocker_timeout", 75)),
        browser_fallback=_bool(pick("TELOS_CARDEA_BROWSER_FALLBACK", fetch, "browser_fallback", False)),
    )
