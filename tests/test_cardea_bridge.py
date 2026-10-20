"""Routine mirroring into Cardea: schedule mapping, shadow/live specs and the sync loop."""

import json
from pathlib import Path

import httpx
import pytest

from telos.cardea_bridge import (
    DEFAULT_OPERATIONS,
    GRACE_SECONDS,
    TRUNCATED_NOTE,
    BridgeConfig,
    CardeaClient,
    RoutineMirror,
    cron_for,
    plan,
    request_bytes,
    trigger_spec,
)
from telos.core.repos.routines import Routine
from telos.routines import RoutineRunner


def routine(**changes) -> Routine:
    values = {
        "id": "73103f92fad9",
        "user_id": "testuser",
        "agent_slug": "scout",
        "name": "Daily search",
        "description": "Search listings in E8.",
        "interval_seconds": 86400,
        "time_of_day": "08:30",
        "one_shot": False,
        "notify_email": True,
        "enabled": True,
        "last_run": None,
        "created_at": "2026-10-01T00:00:00",
    }
    values.update(changes)
    return Routine(**values)


TOOLS = ["rightmove_search", "agency_search", "send_email", "create_routine"]
LIVE = BridgeConfig(mode="live", url="https://c", token_file="t", allow_drop=frozenset({"create_routine"}))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "30 8 * * *"),
        ({"interval_seconds": 7 * 86400, "time_of_day": "07:00"}, "0 7 */7 * *"),
        ({"interval_seconds": 900, "time_of_day": None}, "*/15 * * * *"),
        ({"interval_seconds": 3600, "time_of_day": None}, "0 */1 * * *"),
        ({"interval_seconds": 6 * 3600, "time_of_day": None}, "0 */6 * * *"),
        ({"interval_seconds": 7 * 60, "time_of_day": None}, None),
        ({"one_shot": True}, None),
    ],
)
def test_cron_mapping(changes, expected):
    assert cron_for(routine(**changes)) == expected


def test_live_spec_exposes_mapped_operations_and_expects_one_email():
    spec = trigger_spec(routine(), TOOLS, LIVE)
    assert spec is not None
    template = spec["template"]
    assert spec["trigger_id"] == "telos.73103f92fad9" and spec["schedule"] == "30 8 * * *"
    assert template["operations"] == ["listings.rightmove_search", "listings.agency_search", "mail.send"]
    assert set(template["operations"]) <= set(template["grants"]) and "models.invoke" in template["grants"]
    assert template["expect"] == [{"operation": "mail.send", "min_calls": 1, "max_calls": 1}]
    assert template["namespace"] == "telos-testuser" and template["agent_id"] == "telos-scout"


def test_shadow_spec_swaps_email_for_preview():
    spec = trigger_spec(routine(), TOOLS, BridgeConfig(mode="shadow", url="https://c", token_file="t"))
    assert spec is not None
    assert "mail.send" not in spec["template"]["operations"]
    assert spec["template"]["expect"][0]["operation"] == "mail.preview"


def test_routines_without_mapped_tools_stay_local():
    assert trigger_spec(routine(), ["create_routine"], BridgeConfig(mode="live")) is None


def test_mode_must_be_known(monkeypatch):
    monkeypatch.setenv("TELOS_CARDEA_MODE", "sometimes")
    with pytest.raises(ValueError):
        BridgeConfig.from_env()


class FakeCardea:
    def __init__(self):
        self.triggers: dict[str, dict] = {"telos.stale": {"trigger_id": "telos.stale", "removed": False}}
        self.tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers["Authorization"])
        path = request.url.path
        if request.method == "PUT":
            spec = json.loads(request.content)
            change = "unchanged" if path.rsplit("/", 1)[1] in self.triggers else "installed"
            self.triggers[spec["trigger_id"]] = {"trigger_id": spec["trigger_id"], "removed": False}
            return httpx.Response(200, json={"change": change})
        if request.method == "DELETE":
            self.triggers.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={"change": "removed"})
        return httpx.Response(200, json={"triggers": list(self.triggers.values()), "next": None})


def test_sync_installs_routines_and_removes_stale_triggers(tmp_path: Path):
    token = tmp_path / "jwt"
    token.write_text("svid-token\n")
    fake = FakeCardea()
    config = BridgeConfig(mode="live", url="https://cardea.internal:8443", token_file=str(token), allow_drop=frozenset({"create_routine"}))
    mirror = RoutineMirror(
        CardeaClient(config, transport=httpx.MockTransport(fake.handler)),
        lambda: [routine(), routine(id="local-only", one_shot=True)],
        lambda user, slug: TOOLS,
    )
    assert mirror.sync() == {"telos.73103f92fad9": "installed", "telos.stale": "removed"}
    assert mirror.sync() == {"telos.73103f92fad9": "unchanged"}
    assert set(fake.tokens) == {"Bearer svid-token"}
    assert mirror.mirrored(routine()) and not mirror.mirrored(routine(one_shot=True))


@pytest.mark.asyncio
async def test_runner_skips_routines_owned_by_cardea(tmp_path: Path):
    from telos.routines import RoutineStore

    store = RoutineStore(tmp_path / "routines.db")
    created = store.create("testuser", "scout", "Search", time_of_day="08:30", interval_seconds=86400)
    calls = []

    async def execute(user_id, slug, message, r):
        calls.append(r.id)
        return "ok"

    runner = RoutineRunner(store, execute, owned_elsewhere=lambda r: r.id == created.id)
    await runner._run_routine(created)  # noqa: SLF001
    assert calls == []
    runner_local = RoutineRunner(store, execute)
    await runner_local._run_routine(created)  # noqa: SLF001
    assert calls == [created.id]


def test_live_spec_has_misfire_grace_and_no_idempotency_context():
    spec = trigger_spec(routine(), TOOLS, LIVE)
    assert spec is not None
    assert spec["misfire"] == "grace" and spec["grace_seconds"] == GRACE_SECONDS == 3 * 3600
    assert "idempotency_context" not in spec["template"] and "context" not in spec["template"]


def test_live_refuses_unmapped_tools_unless_allowed():
    strict = BridgeConfig(mode="live", url="https://c", token_file="t")
    refused = plan(routine(), TOOLS, strict)
    assert refused.spec is None and "create_routine" in refused.reason and refused.dropped == ("create_routine",)
    assert plan(routine(), TOOLS, LIVE).spec is not None


def test_shadow_mirrors_with_dropped_tools_listed_in_instructions():
    mirror = plan(routine(), TOOLS, BridgeConfig(mode="shadow"))
    assert mirror.spec is not None and mirror.dropped == ("create_routine",)
    instructions = mirror.spec["template"]["instructions"]
    assert "Not available in this run (do not try to use them): create_routine." in instructions


def test_per_routine_mode_overrides_global(monkeypatch):
    config = BridgeConfig(mode="off", routines={"73103f92fad9": "live"}, allow_drop=frozenset({"create_routine"}))
    assert config.mode_for("73103f92fad9") == "live" and config.mode_for("other") == "off"
    assert trigger_spec(routine(), TOOLS, config) is not None
    assert trigger_spec(routine(id="other"), TOOLS, config) is None
    shadow_everywhere_but_one = BridgeConfig(mode="shadow", routines={"73103f92fad9": "off"})
    assert trigger_spec(routine(), TOOLS, shadow_everywhere_but_one) is None
    monkeypatch.setenv("TELOS_CARDEA_ROUTINES", '{"r1": "LIVE", "r2": "off"}')
    assert BridgeConfig.from_env().routines == {"r1": "live", "r2": "off"}
    monkeypatch.setenv("TELOS_CARDEA_ROUTINES", '{"r1": "maybe"}')
    with pytest.raises(ValueError):
        BridgeConfig.from_env()


def test_instructions_compose_prompt_routine_fence_and_tool_names():
    sibling = routine(id="weekly", name="Weekly digest")
    other_agent = routine(id="elsewhere", agent_slug="helper", name="Not a sibling")
    spec = trigger_spec(routine(), TOOLS, LIVE, system_prompt="You are Scout, a lettings researcher.", siblings=[routine(), sibling, other_agent])
    assert spec is not None
    text = spec["template"]["instructions"]
    assert text.startswith("You are Scout, a lettings researcher.\n\n## Routine\n\nSearch listings in E8.")
    assert "Scope of this run: perform ONLY the routine" in text and "- Weekly digest" in text and "Not a sibling" not in text
    assert text.count("email the user one summary") == 1
    assert "- send_email: use `mail-send`" in text and "- rightmove_search: use `listings-rightmove_search`" in text
    assert text.index("## Routine") < text.index("## Tools in this run")
    shadow = trigger_spec(routine(), TOOLS, BridgeConfig(mode="shadow"))
    assert shadow is not None and "- send_email: use `mail-preview` (shadow run: the email is recorded for review, not sent)" in shadow["template"]["instructions"]


def test_email_line_not_duplicated_when_prompt_already_asks_for_one_email():
    spec = trigger_spec(routine(), TOOLS, LIVE, system_prompt="After each routine, send one email with the results.")
    assert spec is not None and "email the user one summary" not in spec["template"]["instructions"]


def test_oversized_prompt_is_trimmed_not_the_routine():
    spec = trigger_spec(routine(), TOOLS, LIVE, system_prompt="x" * 70000)
    assert spec is not None
    text = spec["template"]["instructions"]
    assert len(text) <= 65536 and text.endswith("create_routine.") and "## Routine" in text


def test_ledger_mapping_is_gated(monkeypatch):
    tools = ["list_property_candidates", "record_property_assessment", "send_email"]
    monkeypatch.delenv("TELOS_CARDEA_LEDGER", raising=False)
    assert "list_property_candidates" not in BridgeConfig.from_env().operations
    monkeypatch.setenv("TELOS_CARDEA_LEDGER", "1")
    config = BridgeConfig.from_env()
    live = trigger_spec(routine(), tools, BridgeConfig(mode="live", operations=config.operations))
    shadow = trigger_spec(routine(), tools, BridgeConfig(mode="shadow", operations=config.operations))
    assert live is not None and live["template"]["operations"] == ["ledger.list", "ledger.record", "mail.send"]
    assert shadow is not None and shadow["template"]["operations"] == ["ledger.list", "ledger.record_preview", "mail.preview"]


def test_context_operations_are_gated(monkeypatch):
    monkeypatch.setenv("TELOS_CARDEA_CONTEXT", "1")
    monkeypatch.setenv("TELOS_CARDEA_LEDGER", "1")
    env = BridgeConfig.from_env()
    assert env.context
    config = BridgeConfig(mode="live", operations=env.operations, context=True)
    spec = trigger_spec(routine(), ["rightmove_search", "memory_recall", "list_property_candidates", "send_email"], config)
    assert spec is not None
    assert spec["template"]["context"] == [
        {"operation": "telos.routine_context", "arguments": {}},
        {"operation": "memory.briefing", "arguments": {"query": "Search listings in E8."}},
        {"operation": "ledger.list", "arguments": {"limit": 50}},
    ]
    assert {"telos.routine_context", "memory.briefing", "ledger.list"} <= set(spec["template"]["grants"])
    bare = trigger_spec(routine(), ["rightmove_search", "send_email"], BridgeConfig(mode="live", context=True))
    # Every Telos turn gets Recordatio's briefing, so agents without memory tools get it too.
    assert bare is not None and bare["template"]["context"] == [
        {"operation": "telos.routine_context", "arguments": {}},
        {"operation": "memory.briefing", "arguments": {"query": "Search listings in E8."}},
    ]
    assert "memory.briefing" in bare["template"]["grants"] and "memory.briefing" not in bare["template"]["operations"]
    long = trigger_spec(routine(description="Search listings. " * 400), ["rightmove_search", "send_email"], BridgeConfig(mode="live", context=True))
    assert long is not None and len(long["template"]["context"][1]["arguments"]["query"]) == 4000


def test_prod_shadow_configuration_keeps_mirroring(monkeypatch):
    monkeypatch.setenv("TELOS_CARDEA_MODE", "shadow")
    monkeypatch.setenv("TELOS_CARDEA_OPERATIONS", '{"browser_open": null, "browser_click": null}')
    for name in ("TELOS_CARDEA_ROUTINES", "TELOS_CARDEA_ALLOW_DROP", "TELOS_CARDEA_LEDGER", "TELOS_CARDEA_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    config = BridgeConfig.from_env()
    mirror = plan(routine(), ["rightmove_search", "browser_open", "browser_click", "send_email"], config)
    assert mirror.spec is not None and mirror.dropped == ("browser_open", "browser_click")
    assert mirror.spec["template"]["operations"] == ["listings.rightmove_search", "mail.preview"]
    assert "context" not in mirror.spec["template"]


class Cardea:
    """In-memory Cardea trigger API with switchable failures."""

    def __init__(self, token: Path):
        self.triggers: dict[str, dict] = {}
        self.refuse: dict[str, tuple[int, dict]] = {}
        self.down = False
        self.status = 0
        self.config = BridgeConfig(mode="live", url="https://c", token_file=str(token), allow_drop=frozenset({"create_routine"}))

    def client(self, **changes) -> CardeaClient:
        from dataclasses import replace

        return CardeaClient(replace(self.config, **changes), transport=httpx.MockTransport(self.handler))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("cardea unreachable", request=request)
        if self.status:
            return httpx.Response(self.status, json={"code": "unauthenticated"})
        identity = request.url.path.rsplit("/", 1)[1]
        if request.method == "PUT":
            if identity in self.refuse:
                status, body = self.refuse[identity]
                return httpx.Response(status, json=body)
            spec = json.loads(request.content)
            before = self.triggers.get(identity)
            change = "installed" if before is None else "unchanged" if before["definition"] == spec else "updated"
            self.triggers[identity] = {"trigger_id": identity, "definition": spec, "definition_sha256": "d", "enabled": spec["enabled"], "removed": False}
            return httpx.Response(200, json={"change": change})
        if request.method == "DELETE":
            self.triggers.pop(identity, None)
            return httpx.Response(200, json={"change": "removed"})
        return httpx.Response(200, json={"triggers": list(self.triggers.values()), "next": None})


@pytest.fixture
def cardea(tmp_path: Path) -> Cardea:
    token = tmp_path / "jwt"
    token.write_text("svid\n")
    return Cardea(token)


def test_live_ownership_requires_confirmation(cardea: Cardea):
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS)
    # Before any sync Cardea may already own the slot: skip, don't double send.
    assert mirror.owned_elsewhere(routine())
    assert mirror.sync() == {"telos.73103f92fad9": "installed"}
    assert set(mirror.confirmed) == {"telos.73103f92fad9"} and mirror.owned_elsewhere(routine())
    # Unmirrorable and non-live routines always run locally.
    assert not mirror.owned_elsewhere(routine(one_shot=True))
    shadow = RoutineMirror(cardea.client(mode="shadow"), lambda: [routine()], lambda user, slug: TOOLS)
    shadow.sync()
    assert not shadow.owned_elsewhere(routine())


def test_definite_refusal_runs_locally_and_alerts_once(cardea: Cardea):
    alerts: list[tuple[str, str]] = []
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS, on_refusal=lambda rid, why: alerts.append((rid, why)))
    mirror.sync()
    assert mirror.owned_elsewhere(routine())
    cardea.refuse["telos.73103f92fad9"] = (409, {"code": "operation_not_installed"})
    assert mirror.sync() == {"telos.73103f92fad9": "removed"}  # the older version is taken down
    assert "telos.73103f92fad9" not in cardea.triggers
    assert not mirror.owned_elsewhere(routine())
    mirror.sync()
    assert len(alerts) == 1 and alerts[0][0] == "73103f92fad9" and "operation_not_installed" in alerts[0][1]


def test_unmirrorable_live_routine_is_surfaced(cardea: Cardea):
    alerts: list[tuple[str, str]] = []
    mirror = RoutineMirror(
        cardea.client(allow_drop=frozenset()), lambda: [routine()], lambda user, slug: TOOLS, on_refusal=lambda rid, why: alerts.append((rid, why))
    )
    assert mirror.sync() == {}
    assert not mirror.owned_elsewhere(routine())
    assert alerts and "create_routine" in alerts[0][1]


@pytest.mark.parametrize("failure", ["down", "stale-token"])
def test_unreachable_cardea_skips_and_alerts_after_repeated_failures(cardea: Cardea, failure: str):
    alerts: list[tuple[int, str]] = []
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS, on_sync_failure=lambda n, e: alerts.append((n, e)))
    mirror.sync()
    if failure == "down":
        cardea.down = True
    else:
        cardea.status = 401
    for _ in range(6):
        mirror.sync()
        assert mirror.owned_elsewhere(routine())  # never falls back to a local run
    assert [n for n, _ in alerts] == [3, 6]
    assert not mirror.refused
    cardea.down, cardea.status = False, 0
    mirror.sync()
    assert mirror.failures == 0 and mirror.owned_elsewhere(routine())


def test_unreachable_before_first_sync_still_skips(cardea: Cardea):
    cardea.down = True
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS)
    assert mirror.sync() == {"telos.73103f92fad9": "error"}
    assert mirror.owned_elsewhere(routine()) and not mirror.synced


def test_mode_off_removes_existing_triggers(cardea: Cardea):
    RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS).sync()
    assert "telos.73103f92fad9" in cardea.triggers
    off = RoutineMirror(cardea.client(mode="off"), lambda: [routine()], lambda user, slug: TOOLS)
    assert off.sync() == {"telos.73103f92fad9": "removed"}
    assert cardea.triggers == {} and not off.owned_elsewhere(routine())


def test_cardea_bridge_is_active_when_configured_even_if_off(monkeypatch, tmp_path: Path):
    from telos.api import app as app_module

    monkeypatch.setenv("TELOS_CARDEA_MODE", "off")
    monkeypatch.delenv("TELOS_CARDEA_URL", raising=False)
    assert app_module._cardea_bridge() is None  # noqa: SLF001
    monkeypatch.setenv("TELOS_CARDEA_URL", "https://cardea.internal:8443")
    monkeypatch.setenv("TELOS_CARDEA_TOKEN_FILE", str(tmp_path / "jwt"))
    bridge = app_module._cardea_bridge()  # noqa: SLF001
    assert bridge is not None and bridge.client.config.mode == "off"


def test_prompt_or_tool_change_updates_the_trigger(cardea: Cardea):
    prompt = {"text": "You are Scout."}
    tools = list(TOOLS)
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: tools, agent_prompt=lambda user, slug: prompt["text"])
    assert mirror.sync() == {"telos.73103f92fad9": "installed"}
    assert cardea.triggers["telos.73103f92fad9"]["definition"]["template"]["instructions"].startswith("You are Scout.")
    assert mirror.sync() == {"telos.73103f92fad9": "unchanged"}
    prompt["text"] = "You are Scout, now terser."
    assert mirror.sync() == {"telos.73103f92fad9": "updated"}
    tools.append("web_search")
    assert mirror.sync() == {"telos.73103f92fad9": "updated"}
    assert "web.search" in cardea.triggers["telos.73103f92fad9"]["definition"]["template"]["operations"]


@pytest.mark.asyncio
async def test_skipping_for_cardea_advances_last_run(tmp_path: Path):
    from telos.routines import RoutineStore

    store = RoutineStore(tmp_path / "routines.db")
    created = store.create("testuser", "scout", "Search", time_of_day="08:30", interval_seconds=86400)

    async def execute(user_id, slug, message, r):
        raise AssertionError("must not run locally")

    await RoutineRunner(store, execute, owned_elsewhere=lambda r: True)._run_routine(created)  # noqa: SLF001
    current = store.get(created.id)
    assert current is not None and current.last_run is not None


def test_large_prompt_is_trimmed_to_fit_the_request_cap():
    prompt = "Prompt line with a quote \" and é.\n" * 1500  # ~55 KB once JSON-escaped
    mirror = plan(routine(), TOOLS, LIVE, system_prompt=prompt, siblings=[routine(), routine(id="weekly", name="Weekly digest")])
    assert mirror.spec is not None and mirror.trimmed is not None
    full, kept = mirror.trimmed
    assert full == len(prompt.strip()) and 0 < kept < full
    assert 32768 - 512 < request_bytes(mirror.spec) <= 32768
    text = mirror.spec["template"]["instructions"]
    assert text.startswith("Prompt line") and TRUNCATED_NOTE in text
    untrimmed = trigger_spec(routine(), TOOLS, LIVE, siblings=[routine(), routine(id="weekly", name="Weekly digest")])
    assert untrimmed is not None
    tail = untrimmed["template"]["instructions"]
    assert tail.startswith("## Routine") and text.endswith(tail)  # routine, fence and appendix intact
    roomy = BridgeConfig(**{**LIVE.__dict__, "max_request_bytes": 131072})
    assert plan(routine(), TOOLS, roomy, system_prompt=prompt).trimmed is None


def test_small_prompt_is_not_trimmed():
    mirror = plan(routine(), TOOLS, LIVE, system_prompt="You are Scout.")
    assert mirror.trimmed is None and mirror.spec is not None
    assert TRUNCATED_NOTE not in mirror.spec["template"]["instructions"]


def test_max_request_bytes_from_env(monkeypatch):
    monkeypatch.delenv("TELOS_CARDEA_MAX_REQUEST_BYTES", raising=False)
    assert BridgeConfig.from_env().max_request_bytes == 32768
    monkeypatch.setenv("TELOS_CARDEA_MAX_REQUEST_BYTES", "131072")
    assert BridgeConfig.from_env().max_request_bytes == 131072


def test_too_large_update_keeps_the_installed_trigger(cardea: Cardea):
    alerts: list[tuple[str, str]] = []
    prompt = {"text": "You are Scout."}
    mirror = RoutineMirror(
        cardea.client(), lambda: [routine()], lambda user, slug: TOOLS,
        agent_prompt=lambda user, slug: prompt["text"], on_refusal=lambda rid, why: alerts.append((rid, why)),
    )
    mirror.sync()
    installed = cardea.triggers["telos.73103f92fad9"]
    prompt["text"] = "You are Scout, revised."
    cardea.refuse["telos.73103f92fad9"] = (413, {"code": "request_too_large"})
    assert mirror.sync() == {"telos.73103f92fad9": "refused"}
    assert cardea.triggers["telos.73103f92fad9"] is installed  # not deleted, not replaced
    assert mirror.owned_elsewhere(routine())  # the installed version still runs in Cardea
    assert len(alerts) == 1 and "request_too_large" in alerts[0][1]


def test_too_large_first_install_runs_locally(cardea: Cardea):
    cardea.refuse["telos.73103f92fad9"] = (413, {"code": "request_too_large"})
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS)
    assert mirror.sync() == {"telos.73103f92fad9": "refused"}
    assert not mirror.owned_elsewhere(routine())


@pytest.mark.parametrize("config", [LIVE, BridgeConfig(mode="shadow")])
def test_instructions_name_the_routine_recipient(config: BridgeConfig):
    spec = trigger_spec(routine(), TOOLS, config, recipient="sam@example.com")
    assert spec is not None
    text = spec["template"]["instructions"]
    routine_section = text[text.index("## Routine") : text.index("## Tools in this run")]
    assert "Send the summary email to: sam@example.com" in routine_section
    assert spec["template"]["expect"] and spec["template"]["expect"][0]["operation"] in {"mail.send", "mail.preview"}


def test_recipient_line_follows_a_prompt_that_already_asks_for_one_email():
    spec = trigger_spec(routine(), TOOLS, LIVE, system_prompt="Send one email with the results.", recipient="sam@example.com")
    assert spec is not None
    text = spec["template"]["instructions"]
    assert "email the user one summary" not in text and "Send the summary email to: sam@example.com" in text


@pytest.mark.parametrize("config", [LIVE, BridgeConfig(mode="shadow")])
def test_no_recipient_withholds_mail_and_expects_no_email(config: BridgeConfig):
    spec = trigger_spec(routine(), TOOLS, config, recipient="")
    assert spec is not None
    template = spec["template"]
    assert not {"mail.send", "mail.preview"} & set(template["operations"]) and template["expect"] == []
    text = template["instructions"]
    assert "do not send any email in this run" in text and "Send the summary email to" not in text
    assert "email the user one summary" not in text
    assert "Not available in this run (do not try to use them): create_routine, send_email." in text


def test_no_recipient_and_only_mail_tools_stays_local():
    assert trigger_spec(routine(), ["send_email"], LIVE, recipient="") is None


def test_mirror_resolves_the_recipient_per_user(cardea: Cardea):
    emails = {"testuser": "sam@example.com", "other": ""}
    mirror = RoutineMirror(
        cardea.client(), lambda: [routine(), routine(id="quiet", user_id="other")], lambda user, slug: TOOLS, user_email=emails.__getitem__
    )
    mirror.sync()
    loud = cardea.triggers["telos.73103f92fad9"]["definition"]["template"]
    quiet = cardea.triggers["telos.quiet"]["definition"]["template"]
    assert "Send the summary email to: sam@example.com" in loud["instructions"] and "mail.send" in loud["operations"]
    assert "mail.send" not in quiet["operations"] and quiet["expect"] == []


def test_browser_read_is_served_by_browser_open():
    tools = ["browser_open", "browser_read", "send_email"]
    spec = trigger_spec(routine(), tools, BridgeConfig(mode="live"))  # no ALLOW_DROP needed
    assert spec is not None
    assert spec["template"]["operations"] == ["browser.open", "mail.send"]
    text = spec["template"]["instructions"]
    assert "- browser_read: use `browser-open` again with the same `url` and `offset` set to `next_offset` from the previous result" in text
    assert "- browser_open: use `browser-open`\n" in text and "Not available" not in text


def test_browser_read_alias_without_browser_open_tool():
    spec = trigger_spec(routine(), ["browser_read", "send_email"], BridgeConfig(mode="live"))
    assert spec is not None and spec["template"]["operations"] == ["browser.open", "mail.send"]


def test_browser_read_stays_dropped_when_browser_open_is_not_installed():
    prod = {**DEFAULT_OPERATIONS, "browser_open": None, "browser_click": None}
    tools = ["rightmove_search", "browser_open", "browser_read", "send_email"]
    mirror = plan(routine(), tools, BridgeConfig(mode="shadow", operations=prod))
    assert mirror.spec is not None and mirror.dropped == ("browser_open", "browser_read")
    assert plan(routine(), tools, BridgeConfig(mode="live", operations=prod)).spec is None
    explicit = plan(routine(), tools, BridgeConfig(mode="shadow", operations={**DEFAULT_OPERATIONS, "browser_read": None}))
    assert explicit.dropped == ("browser_read",)


def test_templates_carry_run_limits():
    template = trigger_spec(routine(), TOOLS, LIVE)["template"]  # type: ignore[index]
    assert template["attempt_timeout_seconds"] == 2700 and template["timeout_seconds"] == 3600
    assert "max_attempts" not in template  # Cardea's default (2)
    custom = BridgeConfig(mode="live", allow_drop=frozenset({"create_routine"}), attempt_timeout_seconds=600, run_timeout_seconds=1200, max_attempts=1)
    template = trigger_spec(routine(), TOOLS, custom)["template"]  # type: ignore[index]
    assert (template["attempt_timeout_seconds"], template["timeout_seconds"], template["max_attempts"]) == (600, 1200, 1)


def test_run_limits_from_env(monkeypatch):
    for name in ("TELOS_CARDEA_ATTEMPT_TIMEOUT_SECONDS", "TELOS_CARDEA_RUN_TIMEOUT_SECONDS", "TELOS_CARDEA_MAX_ATTEMPTS"):
        monkeypatch.delenv(name, raising=False)
    config = BridgeConfig.from_env()
    assert (config.attempt_timeout_seconds, config.run_timeout_seconds, config.max_attempts) == (2700, 3600, None)
    monkeypatch.setenv("TELOS_CARDEA_ATTEMPT_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("TELOS_CARDEA_RUN_TIMEOUT_SECONDS", "3000")
    monkeypatch.setenv("TELOS_CARDEA_MAX_ATTEMPTS", "3")
    config = BridgeConfig.from_env()
    assert (config.attempt_timeout_seconds, config.run_timeout_seconds, config.max_attempts) == (1800, 3000, 3)


@pytest.mark.parametrize(("env", "message"), [
    ({"TELOS_CARDEA_ATTEMPT_TIMEOUT_SECONDS": "10"}, "ATTEMPT_TIMEOUT_SECONDS must be between 30 and 3600, not 10"),
    ({"TELOS_CARDEA_ATTEMPT_TIMEOUT_SECONDS": "0"}, "between 30 and 3600, not 0"),
    ({"TELOS_CARDEA_RUN_TIMEOUT_SECONDS": "7200"}, "RUN_TIMEOUT_SECONDS must be between 60 and 3600, not 7200"),
    ({"TELOS_CARDEA_MAX_ATTEMPTS": "4"}, "MAX_ATTEMPTS must be between 1 and 3, not 4"),
    ({"TELOS_CARDEA_ATTEMPT_TIMEOUT_SECONDS": "3000", "TELOS_CARDEA_RUN_TIMEOUT_SECONDS": "2000"}, r"\(3000\) must not exceed"),
    ({"TELOS_CARDEA_RUN_TIMEOUT_SECONDS": "an hour"}, "must be an integer"),
])
def test_run_limits_are_validated_at_startup(monkeypatch, env, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        BridgeConfig.from_env()


def test_changing_run_limits_updates_the_trigger(cardea: Cardea):
    mirror = RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS)
    assert mirror.sync() == {"telos.73103f92fad9": "installed"}
    assert mirror.sync() == {"telos.73103f92fad9": "unchanged"}
    longer = RoutineMirror(cardea.client(attempt_timeout_seconds=3000), lambda: [routine()], lambda user, slug: TOOLS)
    assert longer.sync() == {"telos.73103f92fad9": "updated"}
    assert cardea.triggers["telos.73103f92fad9"]["definition"]["template"]["attempt_timeout_seconds"] == 3000


def test_templates_carry_run_budgets():
    template = trigger_spec(routine(), TOOLS, LIVE)["template"]  # type: ignore[index]
    assert (template["max_turns"], template["max_tool_calls"], template["max_cost_usd"]) == (120, 300, 6.0)
    assert isinstance(template["max_cost_usd"], float)
    tight = BridgeConfig(mode="live", allow_drop=frozenset({"create_routine"}), max_turns=50, max_tool_calls=80, max_cost_usd=3)
    template = trigger_spec(routine(), TOOLS, tight)["template"]  # type: ignore[index]
    assert (template["max_turns"], template["max_tool_calls"], template["max_cost_usd"]) == (50, 80, 3.0)
    assert isinstance(template["max_cost_usd"], float)


def test_run_budgets_from_env(monkeypatch):
    for name in ("TELOS_CARDEA_MAX_TURNS", "TELOS_CARDEA_MAX_TOOL_CALLS", "TELOS_CARDEA_MAX_COST_USD"):
        monkeypatch.delenv(name, raising=False)
    config = BridgeConfig.from_env()
    assert (config.max_turns, config.max_tool_calls, config.max_cost_usd) == (120, 300, 6.0)
    monkeypatch.setenv("TELOS_CARDEA_MAX_TURNS", "200")
    monkeypatch.setenv("TELOS_CARDEA_MAX_TOOL_CALLS", "1")
    monkeypatch.setenv("TELOS_CARDEA_MAX_COST_USD", "50")
    config = BridgeConfig.from_env()
    assert (config.max_turns, config.max_tool_calls, config.max_cost_usd) == (200, 1, 50.0)


@pytest.mark.parametrize(("env", "message"), [
    ({"TELOS_CARDEA_MAX_TURNS": "0"}, "MAX_TURNS must be between 1 and 200, not 0"),
    ({"TELOS_CARDEA_MAX_TURNS": "201"}, "MAX_TURNS must be between 1 and 200, not 201"),
    ({"TELOS_CARDEA_MAX_TOOL_CALLS": "501"}, "MAX_TOOL_CALLS must be between 1 and 500, not 501"),
    ({"TELOS_CARDEA_MAX_COST_USD": "0"}, "MAX_COST_USD must be above 0 and at most 50, not 0.0"),
    ({"TELOS_CARDEA_MAX_COST_USD": "50.01"}, "MAX_COST_USD must be above 0 and at most 50"),
    ({"TELOS_CARDEA_MAX_COST_USD": "nan"}, "MAX_COST_USD must be above 0"),
    ({"TELOS_CARDEA_MAX_COST_USD": "six"}, "MAX_COST_USD must be a number"),
    ({"TELOS_CARDEA_MAX_TURNS": "1.5"}, "MAX_TURNS must be an integer"),
])
def test_run_budgets_are_validated_at_startup(monkeypatch, env, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        BridgeConfig.from_env()


def test_changing_a_run_budget_updates_the_trigger(cardea: Cardea):
    assert RoutineMirror(cardea.client(), lambda: [routine()], lambda user, slug: TOOLS).sync() == {"telos.73103f92fad9": "installed"}
    richer = RoutineMirror(cardea.client(max_cost_usd=8.0), lambda: [routine()], lambda user, slug: TOOLS)
    assert richer.sync() == {"telos.73103f92fad9": "updated"}
    assert cardea.triggers["telos.73103f92fad9"]["definition"]["template"]["max_cost_usd"] == 8.0


def kernel_client(handler, tmp_path: Path) -> CardeaClient:
    token = tmp_path / "jwt"
    token.write_text("svid\n")
    return CardeaClient(BridgeConfig(url="https://c", token_file=str(token)), transport=httpx.MockTransport(handler))


def test_run_joins_every_page_of_calls(tmp_path: Path):
    pages = {
        None: {"task_id": "t", "state": "succeeded", "calls": [{"operation_id": "ctx-01"}], "calls_total": 3, "calls_next": "ctx-01"},
        "ctx-01": {"task_id": "t", "state": "succeeded", "calls": [{"operation_id": "call-0001"}], "calls_total": 3, "calls_next": "call-0001"},
        "call-0001": {"task_id": "t", "state": "succeeded", "calls": [{"operation_id": "call-0002"}], "calls_total": 3, "calls_next": None},
    }
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0.2/tasks/t/run"
        after = request.url.params.get("after")
        seen.append(after)
        return httpx.Response(200, json=pages[after])

    run_view = kernel_client(handler, tmp_path).run("t")
    assert [c["operation_id"] for c in run_view["calls"]] == ["ctx-01", "call-0001", "call-0002"]
    assert seen == [None, "ctx-01", "call-0001"] and run_view["calls_next"] is None


def test_operation_returns_the_full_receipt(tmp_path: Path):
    receipt = {"request": {"arguments": {"arguments": {"to": "sam@example.com"}}}, "result": {"output": {"message_id": "<m>"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0.2/tasks/t/operations/call-0002"
        return httpx.Response(200, json={"schema_version": "0.2", "receipt": receipt})

    assert kernel_client(handler, tmp_path).operation("t", "call-0002") == receipt


def test_output_inline_and_chunked(tmp_path: Path):
    import base64
    import hashlib

    document = {"text": "Long digest. " * 3000, "turns": 9}
    data = json.dumps(document).encode()
    state = {"omitted": False, "sha": hashlib.sha256(data).hexdigest()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0.2/tasks/t/output":
            if not state["omitted"]:
                return httpx.Response(200, json={"content": document, "content_omitted": False})
            return httpx.Response(200, json={"content": None, "content_omitted": True, "content_bytes": len(data)})
        offset = int(request.url.params["offset"])
        end = min(offset + 10000, len(data))
        return httpx.Response(200, json={"total_bytes": len(data), "sha256": state["sha"], "offset": offset,
                                         "next_offset": end if end < len(data) else None,
                                         "data_base64": base64.b64encode(data[offset:end]).decode()})

    client = kernel_client(handler, tmp_path)
    assert client.output("t") == document
    state["omitted"] = True
    assert client.output("t") == document
    state["sha"] = "0" * 64
    with pytest.raises(ValueError, match="sha256"):
        client.output("t")
