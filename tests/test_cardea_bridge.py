"""Routine mirroring into Cardea: schedule mapping, shadow/live specs and the sync loop."""

import json
from pathlib import Path

import httpx
import pytest

from telos.cardea_bridge import BridgeConfig, CardeaClient, RoutineMirror, cron_for, trigger_spec
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
    spec = trigger_spec(routine(), TOOLS, BridgeConfig(mode="live", url="https://c", token_file="t"))
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
    config = BridgeConfig(mode="live", url="https://cardea.internal:8443", token_file=str(token))
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
