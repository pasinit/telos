"""Mirror Telos routines into Cardea, which owns their execution.

Cardea (github.com/pasinit/cardea) runs each routine slot as an agent run whose only tools
are installed operations, with receipts, idempotency scopes and crash recovery. Telos keeps
routine text and presentation and syncs each routine to a supervisor-managed Cardea trigger.

Modes (``TELOS_CARDEA_MODE``):

- ``off``: nothing is mirrored; Telos runs routines itself (default).
- ``shadow``: routines are mirrored with email swapped for a non-sending preview operation,
  while Telos keeps running them. Compare outcomes before switching over.
- ``live``: routines are mirrored with real email and Telos stops running mirrored ones.
"""

import json
import logging
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import httpx

from telos.core.repos.routines import Routine

log = logging.getLogger("telos.cardea")

# Telos tool name -> Cardea installed operation name (see packages/telos-cardea).
DEFAULT_OPERATIONS = {
    "send_email": "mail.send",
    "rightmove_search": "listings.rightmove_search",
    "zoopla_search": "listings.zoopla_search",
    "agency_search": "listings.agency_search",
    "web_search": "web.search",
    "browser_open": "browser.open",
    "browser_click": "browser.click",
    "memory_recall": "memory.recall",
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
PREVIEW = {"mail.send": "mail.preview"}
_SAFE = re.compile(r"[^A-Za-z0-9._:-]")


@dataclass(frozen=True)
class BridgeConfig:
    mode: str = "off"
    url: str = ""
    ca_bundle: str | None = None
    token_file: str = ""
    supervisor: str = "telos"
    timezone: str = "Europe/London"
    operations: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_OPERATIONS))

    @classmethod
    def from_env(cls) -> Self:
        mapping = dict(DEFAULT_OPERATIONS)
        if os.getenv("TELOS_CARDEA_OPERATIONS"):
            mapping.update(json.loads(os.environ["TELOS_CARDEA_OPERATIONS"]))
        mode = os.getenv("TELOS_CARDEA_MODE", "off").strip().lower()
        if mode not in {"off", "shadow", "live"}:
            raise ValueError(f"TELOS_CARDEA_MODE must be off, shadow or live, not {mode!r}")
        return cls(
            mode=mode,
            url=os.getenv("TELOS_CARDEA_URL", "").rstrip("/"),
            ca_bundle=os.getenv("TELOS_CARDEA_CA") or None,
            token_file=os.getenv("TELOS_CARDEA_TOKEN_FILE", ""),
            supervisor=os.getenv("TELOS_CARDEA_SUPERVISOR", "telos"),
            timezone=os.getenv("TELOS_LOCAL_TIMEZONE", "Europe/London"),
            operations=mapping,
        )

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "live"}


def identifier(value: str) -> str:
    """A Cardea Identifier: [A-Za-z0-9][A-Za-z0-9._:-]*, at most 110 chars here."""
    cleaned = _SAFE.sub("-", value).strip("-.:") or "x"
    return cleaned[:110]


def trigger_id(routine: Routine) -> str:
    return identifier(f"telos.{routine.id}")


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


def trigger_spec(
    routine: Routine, agent_tools: Iterable[str], config: BridgeConfig
) -> dict[str, Any] | None:
    """The Cardea trigger for one routine, or None when it cannot be mirrored."""
    schedule = cron_for(routine)
    if schedule is None:
        return None
    operations = []
    for tool in agent_tools:
        name = config.operations.get(tool)
        if name is None:
            continue
        if config.mode == "shadow":
            name = PREVIEW.get(name, name)
        if name not in operations:
            operations.append(name)
    if not operations:
        return None
    email = PREVIEW["mail.send"] if config.mode == "shadow" else "mail.send"
    expect = (
        [{"operation": email, "min_calls": 1, "max_calls": 1}]
        if routine.notify_email and email in operations
        else []
    )
    instructions = routine.description
    if routine.notify_email:
        instructions += "\n\nWhen you are done, email the user one summary of the results."
    return {
        "trigger_id": trigger_id(routine),
        "supervisor_id": config.supervisor,
        "schedule": schedule,
        "timezone": config.timezone,
        "misfire": "run_once_late",
        "enabled": routine.enabled,
        "template_kind": "agent-run",
        "template": {
            "agent_id": identifier(f"telos-{routine.agent_slug}"),
            "namespace": identifier(f"telos-{routine.user_id}"),
            "objective": (routine.name or routine.description)[:1024],
            "instructions": instructions[:65536],
            "operations": operations,
            "grants": [*KERNEL_RIGHTS, *operations],
            "payload": {},
            "expect": expect,
        },
    }


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


class RoutineMirror:
    """Keeps Cardea triggers equal to Telos's mirrorable routines; idempotent."""

    def __init__(
        self,
        client: CardeaClient,
        routines: Callable[[], list[Routine]],
        agent_tools: Callable[[str, str], list[str]],
    ) -> None:
        self.client = client
        self.routines = routines
        self.agent_tools = agent_tools

    def mirrored(self, routine: Routine) -> bool:
        spec = trigger_spec(
            routine, self.agent_tools(routine.user_id, routine.agent_slug), self.client.config
        )
        return spec is not None

    def sync(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        wanted: set[str] = set()
        for routine in self.routines():
            spec = trigger_spec(
                routine,
                self.agent_tools(routine.user_id, routine.agent_slug),
                self.client.config,
            )
            if spec is None:
                continue
            wanted.add(spec["trigger_id"])
            try:
                outcomes[spec["trigger_id"]] = self.client.put_trigger(spec)["change"]
            except httpx.HTTPError as error:
                log.warning("cardea trigger sync failed for %s: %s", routine.id, error)
                outcomes[spec["trigger_id"]] = "error"
        try:
            installed = self.client.triggers()
        except httpx.HTTPError as error:
            log.warning("cardea trigger listing failed: %s", error)
            return outcomes
        for trigger in installed:
            identity = trigger["trigger_id"]
            if identity.startswith("telos.") and not trigger["removed"] and identity not in wanted:
                try:
                    self.client.delete_trigger(identity)
                    outcomes[identity] = "removed"
                except httpx.HTTPError as error:
                    log.warning("cardea trigger removal failed for %s: %s", identity, error)
                    outcomes[identity] = "error"
        return outcomes
