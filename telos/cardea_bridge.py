"""Mirror Telos routines into Cardea, which owns their execution.

Cardea (github.com/pasinit/cardea) runs each routine slot as an agent run whose only tools
are installed operations, with receipts, idempotency scopes and crash recovery. Telos keeps
routine text and presentation and syncs each routine to a supervisor-managed Cardea trigger.

Modes (``TELOS_CARDEA_MODE``, overridable per routine with
``TELOS_CARDEA_ROUTINES='{"<routine id>": "off|shadow|live"}'``):

- ``off``: nothing is mirrored and existing ``telos.*`` triggers are removed; Telos runs
  routines itself (default).
- ``shadow``: routines are mirrored with email swapped for a non-sending preview operation,
  while Telos keeps running them. Compare outcomes before switching over.
- ``live``: routines are mirrored with real email. Telos stops running a live routine only
  once Cardea has confirmed its trigger; while Cardea is unreachable it skips the routine
  (Cardea may still be running it) and raises an operator alert after repeated failures,
  and when Cardea definitely refuses the trigger Telos runs the routine itself.

The bridge is active whenever ``TELOS_CARDEA_URL`` and ``TELOS_CARDEA_TOKEN_FILE`` are set, so
switching to ``off`` converges Cardea to no Telos triggers instead of leaving them firing.
"""

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import httpx

from telos.core.repos.routines import Routine
from telos.routines import scope_note

log = logging.getLogger("telos.cardea")

# Telos tool name -> Cardea installed operation name (see packages/telos-cardea).
# A ``None`` value (from ``TELOS_CARDEA_OPERATIONS``) marks a tool as not available.
DEFAULT_OPERATIONS: dict[str, str | None] = {
    "send_email": "mail.send",
    "rightmove_search": "listings.rightmove_search",
    "zoopla_search": "listings.zoopla_search",
    "agency_search": "listings.agency_search",
    "web_search": "web.search",
    "browser_open": "browser.open",
    "browser_click": "browser.click",
    "memory_recall": "memory.recall",
}
# Mapped only with ``TELOS_CARDEA_LEDGER=1``: Cardea refuses triggers naming operations it
# has not installed, so these stay out until the ledger operations are deployed.
LEDGER_OPERATIONS: dict[str, str | None] = {
    "list_property_candidates": "ledger.list",
    "record_property_assessment": "ledger.record",
}
KERNEL_RIGHTS = (
    "objects.read",
    "objects.write",
    "tasks.inspect",
    "tasks.cancel",
    "tasks.resume",
    "events.read",
    "tasks.submit",
    "models.invoke",
)
PREVIEW = {"mail.send": "mail.preview", "ledger.record": "ledger.record_preview"}
PREVIEW_NOTES = {
    "mail.preview": "shadow run: the email is recorded for review, not sent",
    "ledger.record_preview": "shadow run: the assessment is validated, not recorded",
}
MODES = ("off", "shadow", "live")
# Slots missed by up to this long (a Cardea restart, a deploy) still run once, late.
GRACE_SECONDS = 3 * 3600
# Cardea answers these when it has understood and rejected the trigger (validation,
# operation_not_installed, ...). Auth failures (401/403) mean a stale token, not a refusal.
# 413 (request_too_large) is a refusal too, but of this version only: the installed one is kept.
REFUSALS = frozenset({400, 404, 409, 413, 422})
TOO_LARGE = 413
# Room left under Cardea's request cap for headers-free framing differences (httpx encodes
# more compactly than ``json.dumps`` defaults, which is what the budget measures).
REQUEST_MARGIN = 256
TRUNCATED_NOTE = "[The system prompt above was truncated to fit Cardea's request size limit.]"
EMAIL_LINE = "When you are done, email the user one summary of the results."
_SAYS_ONE_EMAIL = re.compile(r"\b(one|a single|exactly one)\s+(summary\s+)?e-?mail\b|\be-?mail the user (one|a single)\b", re.IGNORECASE)
_SAFE = re.compile(r"[^A-Za-z0-9._:-]")
_MAX_INSTRUCTIONS = 65536
# memory.briefing's inputSchema maxLength (packages/telos-cardea/descriptors/memory.briefing.json).
BRIEFING_QUERY_CHARS = 4000


def _json_env(name: str) -> Any:
    raw = os.getenv(name, "").strip()
    return json.loads(raw) if raw else None


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _mode(value: Any, source: str) -> str:
    mode = str(value).strip().lower()
    if mode not in MODES:
        raise ValueError(f"{source} must be off, shadow or live, not {value!r}")
    return mode


@dataclass(frozen=True)
class BridgeConfig:
    mode: str = "off"
    url: str = ""
    ca_bundle: str | None = None
    token_file: str = ""
    supervisor: str = "telos"
    timezone: str = "Europe/London"
    operations: dict[str, str | None] = field(default_factory=lambda: dict(DEFAULT_OPERATIONS))
    # routine id -> mode, overriding ``mode`` for that routine.
    routines: dict[str, str] = field(default_factory=dict)
    # Telos tools a live routine may lose in Cardea (no installed operation for them).
    allow_drop: frozenset[str] = frozenset()
    # Send C2 context operations with each trigger (needs a Cardea that accepts them).
    context: bool = False
    # Consecutive failed syncs before the operator is alerted.
    alert_after: int = 3
    # Cardea's cap on a whole request body; trigger specs are trimmed to fit under it.
    max_request_bytes: int = 32768

    @classmethod
    def from_env(cls) -> Self:
        """Read ``TELOS_CARDEA_*``; raises ValueError on an unknown mode or malformed JSON."""
        mapping = dict(DEFAULT_OPERATIONS)
        if _flag("TELOS_CARDEA_LEDGER"):
            mapping.update(LEDGER_OPERATIONS)
        mapping.update(_json_env("TELOS_CARDEA_OPERATIONS") or {})
        routines = {str(k): _mode(v, f"TELOS_CARDEA_ROUTINES[{k!r}]") for k, v in (_json_env("TELOS_CARDEA_ROUTINES") or {}).items()}
        allow_drop = _json_env("TELOS_CARDEA_ALLOW_DROP") or []
        if not isinstance(allow_drop, list):
            raise ValueError("TELOS_CARDEA_ALLOW_DROP must be a JSON list of Telos tool names")
        return cls(
            mode=_mode(os.getenv("TELOS_CARDEA_MODE", "off"), "TELOS_CARDEA_MODE"),
            url=os.getenv("TELOS_CARDEA_URL", "").rstrip("/"),
            ca_bundle=os.getenv("TELOS_CARDEA_CA") or None,
            token_file=os.getenv("TELOS_CARDEA_TOKEN_FILE", ""),
            supervisor=os.getenv("TELOS_CARDEA_SUPERVISOR", "telos"),
            timezone=os.getenv("TELOS_LOCAL_TIMEZONE", "Europe/London"),
            operations=mapping,
            routines=routines,
            allow_drop=frozenset(str(t) for t in allow_drop),
            context=_flag("TELOS_CARDEA_CONTEXT"),
            alert_after=max(1, int(os.getenv("TELOS_CARDEA_ALERT_AFTER", "3"))),
            max_request_bytes=int(os.getenv("TELOS_CARDEA_MAX_REQUEST_BYTES", "32768")),
        )

    @property
    def configured(self) -> bool:
        """Cardea is reachable in principle, so the bridge must keep its triggers in line."""
        return bool(self.url and self.token_file)

    @property
    def wants_mirroring(self) -> bool:
        return self.mode != "off" or any(m != "off" for m in self.routines.values())

    def mode_for(self, routine_id: str) -> str:
        return self.routines.get(routine_id, self.mode)


@dataclass(frozen=True)
class MirrorPlan:
    """What the bridge wants in Cardea for one routine: a trigger spec, or why there is none."""

    mode: str
    spec: dict[str, Any] | None
    dropped: tuple[str, ...] = ()
    reason: str = ""
    # (original, kept) system prompt chars when the prompt was trimmed to fit the request cap.
    trimmed: tuple[int, int] | None = None


@dataclass(frozen=True)
class Confirmation:
    """Cardea accepted ``sha`` (our spec digest) and its listing showed the trigger enabled."""

    sha: str
    at: datetime
    definition_sha256: str = ""


def identifier(value: str) -> str:
    """A Cardea Identifier: [A-Za-z0-9][A-Za-z0-9._:-]*, at most 110 chars here."""
    cleaned = _SAFE.sub("-", value).strip("-.:") or "x"
    return cleaned[:110]


def trigger_id(routine: Routine) -> str:
    return identifier(f"telos.{routine.id}")


def tool_name(operation: str) -> str:
    """The name Cardea gives an operation's tool in agent runs (model APIs reject dots)."""
    return operation.replace(".", "-")


def spec_sha(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cron_for(routine: Routine) -> str | None:
    """Five-field cron for a recurring routine, or None when Telos must keep it."""
    if routine.one_shot:
        return None
    if routine.time_of_day and routine.interval_seconds >= 86400:
        hour, _, minute = routine.time_of_day.partition(":")
        days = routine.interval_seconds // 86400
        day = "*" if days == 1 else f"*/{days}"
        return f"{int(minute or 0)} {int(hour)} {day} * *"
    seconds = routine.interval_seconds
    if seconds % 60 or seconds < 60:
        return None
    minutes = seconds // 60
    if minutes < 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0 and minutes < 1440 and 24 % (minutes // 60) == 0:
        return f"0 */{minutes // 60} * * *"
    if minutes == 1440:
        return "0 0 * * *"
    return None


def tools_appendix(mapped: dict[str, str], dropped: Iterable[str]) -> str:
    """The "Tools in this run" section: Telos tool names the prompt uses -> Cardea tool names."""
    lines = [
        "## Tools in this run",
        "",
        "This run's tools are named differently from the Telos tools your instructions mention:",
    ]
    for tool, operation in mapped.items():
        note = PREVIEW_NOTES.get(operation)
        lines.append(f"- {tool}: use `{tool_name(operation)}`" + (f" ({note})" if note else ""))
    if dropped := list(dropped):
        lines += ["", "Not available in this run (do not try to use them): " + ", ".join(dropped) + "."]
    return "\n".join(lines)


def compose_instructions(
    routine: Routine,
    mapped: dict[str, str],
    dropped: Iterable[str],
    *,
    system_prompt: str = "",
    note: str = "",
    prompt_chars: int | None = None,
) -> str:
    """Agent prompt + ``## Routine`` (description, sibling fence, email line) + tools appendix.

    The system prompt alone is trimmed (to ``prompt_chars``, and to Cardea's instructions limit)
    so the routine and tools always fit; a trimmed prompt is followed by ``TRUNCATED_NOTE``.
    """
    routine_part = routine.description + note
    if routine.notify_email and not _SAYS_ONE_EMAIL.search(system_prompt + "\n" + routine.description):
        routine_part += "\n\n" + EMAIL_LINE
    tail = f"## Routine\n\n{routine_part}\n\n{tools_appendix(mapped, dropped)}"
    prompt = system_prompt.strip()
    limit = max(0, _MAX_INSTRUCTIONS - len(tail) - len(TRUNCATED_NOTE) - 4)
    if prompt_chars is not None:
        limit = min(limit, prompt_chars)
    head = prompt[:limit]
    if len(head) < len(prompt):
        head = f"{head}\n\n{TRUNCATED_NOTE}" if head else TRUNCATED_NOTE
    return (f"{head}\n\n{tail}" if head else tail)[:_MAX_INSTRUCTIONS]


def request_bytes(spec: dict[str, Any]) -> int:
    """Size of ``spec`` as a request body (``json.dumps`` defaults: an upper bound on httpx's)."""
    return len(json.dumps(spec).encode())


def context_calls(routine: Routine, operations: list[str]) -> list[dict[str, Any]]:
    """C2 context operations Cardea runs before each attempt (C4).

    ``memory.briefing`` is always included: every Telos turn gets Recordatio's briefing,
    whether or not the agent has memory tools. Its query is capped at the operation's 4000 chars.
    """
    query = (routine.description.strip() or routine.name or routine.id)[:BRIEFING_QUERY_CHARS]
    calls: list[dict[str, Any]] = [
        {"operation": "telos.routine_context", "arguments": {}},
        {"operation": "memory.briefing", "arguments": {"query": query}},
    ]
    if any(op.startswith("ledger.") for op in operations):
        calls.append({"operation": "ledger.list", "arguments": {"limit": 50}})
    return calls


def plan(
    routine: Routine,
    agent_tools: Iterable[str],
    config: BridgeConfig,
    *,
    system_prompt: str = "",
    siblings: Iterable[Routine] = (),
) -> MirrorPlan:
    """The Cardea trigger for one routine under its mode, or the reason it stays local."""
    mode = config.mode_for(routine.id)
    if mode == "off":
        return MirrorPlan(mode, None, reason="mode is off")
    schedule = cron_for(routine)
    if schedule is None:
        return MirrorPlan(mode, None, reason="schedule has no cron equivalent")
    mapped: dict[str, str] = {}
    dropped: list[str] = []
    for tool in dict.fromkeys(agent_tools):
        name = config.operations.get(tool)
        if not name:
            dropped.append(tool)
            continue
        mapped[tool] = PREVIEW.get(name, name) if mode == "shadow" else name
    if not mapped:
        return MirrorPlan(mode, None, tuple(dropped), reason="agent has no tool with a Cardea operation")
    if mode == "live" and (unallowed := [t for t in dropped if t not in config.allow_drop]):
        return MirrorPlan(
            mode, None, tuple(dropped), reason=f"tools without a Cardea operation: {', '.join(unallowed)} (see TELOS_CARDEA_ALLOW_DROP)"
        )
    operations = list(dict.fromkeys(mapped.values()))
    email = PREVIEW["mail.send"] if mode == "shadow" else "mail.send"
    expect = [{"operation": email, "min_calls": 1, "max_calls": 1}] if routine.notify_email and email in operations else []
    note = scope_note(routine, list(siblings))
    template: dict[str, Any] = {
        "agent_id": identifier(f"telos-{routine.agent_slug}"),
        "namespace": identifier(f"telos-{routine.user_id}"),
        "objective": (routine.name or routine.description)[:1024],
        "instructions": "",
        "operations": operations,
        "grants": [*KERNEL_RIGHTS, *operations],
        "payload": {},
        "expect": expect,
    }
    if config.context:
        template["context"] = context_calls(routine, operations)
        template["grants"] += [c["operation"] for c in template["context"] if c["operation"] not in template["grants"]]
    spec = {
        "trigger_id": trigger_id(routine),
        "supervisor_id": config.supervisor,
        "schedule": schedule,
        "timezone": config.timezone,
        "misfire": "grace",
        "grace_seconds": GRACE_SECONDS,
        "enabled": routine.enabled,
        "template_kind": "agent-run",
        "template": template,
    }

    def fits(prompt_chars: int | None) -> bool:
        template["instructions"] = compose_instructions(
            routine, mapped, dropped, system_prompt=system_prompt, note=note, prompt_chars=prompt_chars
        )
        return request_bytes(spec) <= budget

    budget = config.max_request_bytes - REQUEST_MARGIN
    full = len(system_prompt.strip())
    if fits(None) or not full:
        return MirrorPlan(mode, spec, tuple(dropped))
    # The longest prompt prefix whose spec fits; with none fitting, Cardea's 413 is surfaced.
    low, high = 0, full
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    fits(low)
    return MirrorPlan(mode, spec, tuple(dropped), trimmed=(full, low))


def trigger_spec(
    routine: Routine,
    agent_tools: Iterable[str],
    config: BridgeConfig,
    *,
    system_prompt: str = "",
    siblings: Iterable[Routine] = (),
) -> dict[str, Any] | None:
    """The Cardea trigger for one routine, or None when it cannot be mirrored."""
    return plan(routine, agent_tools, config, system_prompt=system_prompt, siblings=siblings).spec


class CardeaClient:
    """Authenticated client for the Cardea kernel API over TLS."""

    def __init__(self, config: BridgeConfig, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        verify: Any = config.ca_bundle or True
        self.http = httpx.Client(
            base_url=config.url, verify=verify, timeout=15.0, transport=transport
        )

    def _headers(self) -> dict[str, str]:
        # spiffe-helper (or the operator) keeps a fresh short-lived JWT-SVID in this file.
        token = Path(self.config.token_file).read_text().strip()
        return {"Authorization": f"Bearer {token}", "X-Cardea-Auth": "spiffe"}

    def put_trigger(self, spec: dict[str, Any]) -> dict[str, Any]:
        response = self.http.put(
            f"/v0.2/triggers/{spec['trigger_id']}", json=spec, headers=self._headers()
        )
        response.raise_for_status()
        return dict(response.json())

    def delete_trigger(self, identity: str) -> None:
        response = self.http.delete(f"/v0.2/triggers/{identity}", headers=self._headers())
        if response.status_code not in {200, 404}:
            response.raise_for_status()

    def triggers(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = []
        after = ""
        while True:
            response = self.http.get(
                "/v0.2/triggers", params={"after": after, "limit": 20}, headers=self._headers()
            )
            response.raise_for_status()
            body = response.json()
            listed.extend(body["triggers"])
            if not body.get("next"):
                return listed
            after = body["next"]

    def run(self, task_id: str) -> dict[str, Any]:
        response = self.http.get(f"/v0.2/tasks/{task_id}/run", headers=self._headers())
        response.raise_for_status()
        return dict(response.json())


def _refusal(error: httpx.HTTPError) -> str | None:
    """Cardea's reason when ``error`` is a definite refusal of the request, else None."""
    if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code not in REFUSALS:
        return None
    return f"HTTP {error.response.status_code}: {error.response.text[:500]}"


class RoutineMirror:
    """Keeps Cardea triggers equal to Telos's mirrorable routines; idempotent.

    Alert hooks (callables, defaults log only; never allowed to break the sync):

    - ``on_sync_failure(count, error)``: called once when ``config.alert_after`` consecutive
      syncs have failed (Cardea unreachable, stale token, 5xx), and again each time the count
      doubles; the count resets after a fully successful sync.
    - ``on_refusal(routine_id, reason)``: a live routine runs locally because it cannot be
      mirrored (unmapped tools, no cron) or Cardea refused its trigger, or Cardea refused a
      trigger update as too large (any mode; the installed version keeps running); called
      once per distinct reason.
    """

    def __init__(
        self,
        client: CardeaClient,
        routines: Callable[[], list[Routine]],
        agent_tools: Callable[[str, str], list[str]],
        agent_prompt: Callable[[str, str], str] | None = None,
        on_sync_failure: Callable[[int, str], None] | None = None,
        on_refusal: Callable[[str, str], None] | None = None,
    ) -> None:
        self.client = client
        self.routines = routines
        self.agent_tools = agent_tools
        self.agent_prompt = agent_prompt
        self.on_sync_failure = on_sync_failure
        self.on_refusal = on_refusal
        # trigger id -> confirmation; only these routines are owned by Cardea.
        self.confirmed: dict[str, Confirmation] = {}
        # trigger id -> reason Cardea definitely refused it (and does not run it).
        self.refused: dict[str, str] = {}
        self.failures = 0
        self.synced = False
        self._next_alert = 0
        self._refusals: dict[str, str] = {}

    def plan(self, routine: Routine, routines: list[Routine] | None = None) -> MirrorPlan:
        """``routine``'s plan; given all ``routines``, with the full instructions (prompt, sibling fence)."""
        prompt = ""
        if routines is not None and self.agent_prompt is not None:
            prompt = self.agent_prompt(routine.user_id, routine.agent_slug)
        return plan(
            routine,
            self.agent_tools(routine.user_id, routine.agent_slug),
            self.client.config,
            system_prompt=prompt,
            siblings=routines or (),
        )

    def mirrored(self, routine: Routine) -> bool:
        return self.plan(routine).spec is not None

    def owned_elsewhere(self, routine: Routine) -> bool:
        """Whether Telos must not run ``routine`` itself (the RoutineRunner hook).

        False unless the routine is live and mirrorable. Then True when Cardea confirmed
        the trigger, False when Cardea definitely refused it, and True while unknown (not
        synced yet, Cardea unreachable): running locally then risks a double send.
        """
        mirror = self.plan(routine)
        if mirror.mode != "live" or mirror.spec is None:
            return False
        identity = mirror.spec["trigger_id"]
        if identity in self.confirmed:
            return True
        if identity in self.refused:
            return False
        log.warning("cardea ownership of %s is unconfirmed (synced=%s, failures=%d) — skipping local run", routine.id, self.synced, self.failures)
        return True

    def sync(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        wanted: set[str] = set()
        accepted: dict[str, str] = {}
        refused: dict[str, str] = {}
        failure = ""
        routines = self.routines()
        for routine in routines:
            mirror = self.plan(routine, routines)
            if mirror.spec is None:
                if mirror.mode == "live":
                    self._refuse(routine.id, mirror.reason)
                continue
            spec = mirror.spec
            identity = spec["trigger_id"]
            wanted.add(identity)
            try:
                outcomes[identity] = self.client.put_trigger(spec)["change"]
                accepted[identity] = spec_sha(spec)
                if mirror.dropped and outcomes[identity] != "unchanged":
                    log.info("cardea trigger %s (%s) runs without %s", identity, mirror.mode, ", ".join(mirror.dropped))
                if mirror.trimmed and outcomes[identity] != "unchanged":
                    log.warning(
                        "cardea trigger %s: system prompt trimmed from %d to %d chars to fit %d request bytes",
                        identity, *mirror.trimmed, self.client.config.max_request_bytes,
                    )
                self._refusals.pop(routine.id, None)
            except (httpx.HTTPError, OSError) as error:
                reason = _refusal(error) if isinstance(error, httpx.HTTPError) else None
                if reason is None:
                    log.warning("cardea trigger sync failed for %s: %s", routine.id, error)
                    failure = failure or f"PUT {identity}: {error}"
                    outcomes[identity] = "error"
                    continue
                outcomes[identity] = "refused"
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == TOO_LARGE:
                    # Keep the installed version running; Telos runs it only if there is none.
                    refused[identity] = reason
                    self._refuse(routine.id, f"Cardea refused the trigger update as too large, keeping the installed version: {reason}")
                elif mirror.mode == "live":
                    # Remove any older version so Cardea and Telos cannot both run it.
                    wanted.discard(identity)
                    refused[identity] = reason
                    self._refuse(routine.id, f"Cardea refused the trigger: {reason}")
                else:
                    log.warning("cardea refused shadow trigger %s: %s", identity, reason)
        try:
            installed = self.client.triggers()
        except (httpx.HTTPError, OSError) as error:
            log.warning("cardea trigger listing failed: %s", error)
            self._failed(f"listing: {error}")
            return outcomes
        active: dict[str, dict[str, Any]] = {}
        for trigger in installed:
            identity = trigger["trigger_id"]
            if not identity.startswith("telos.") or trigger["removed"]:
                continue
            if identity not in wanted:
                try:
                    self.client.delete_trigger(identity)
                    outcomes[identity] = "removed"
                    continue
                except (httpx.HTTPError, OSError) as error:
                    log.warning("cardea trigger removal failed for %s: %s", identity, error)
                    failure = failure or f"DELETE {identity}: {error}"
                    outcomes[identity] = "error"
            if trigger.get("enabled", True):
                active[identity] = trigger
        self._confirm(active, accepted, refused)
        if failure:
            self._failed(failure)
        else:
            self.failures, self._next_alert = 0, 0
        return outcomes

    def _confirm(self, active: dict[str, dict[str, Any]], accepted: dict[str, str], refused: dict[str, str]) -> None:
        now = datetime.now(UTC)
        self.confirmed = {k: v for k, v in self.confirmed.items() if k in active}
        for identity, sha in accepted.items():
            if identity in active:
                self.confirmed[identity] = Confirmation(sha, now, str(active[identity].get("definition_sha256", "")))
        # A refused trigger whose older version is still enabled keeps running in Cardea.
        self.refused = {k: v for k, v in refused.items() if k not in active}
        self.synced = True

    def _failed(self, error: str) -> None:
        self.failures += 1
        threshold = self.client.config.alert_after
        if self.failures < threshold or self.failures < self._next_alert:
            return
        self._next_alert = self.failures * 2
        log.error("cardea routine sync failed %d times in a row: %s", self.failures, error)
        if self.on_sync_failure is not None:
            try:
                self.on_sync_failure(self.failures, error)
            except Exception:
                log.exception("cardea sync-failure alert hook failed")

    def _refuse(self, routine_id: str, reason: str) -> None:
        if self._refusals.get(routine_id) == reason:
            return
        self._refusals[routine_id] = reason
        log.error("cardea trigger for routine %s not installed: %s", routine_id, reason)
        if self.on_refusal is not None:
            try:
                self.on_refusal(routine_id, reason)
            except Exception:
                log.exception("cardea refusal alert hook failed")
