"""Confined descriptor variants and the host-owner worker configuration they name (ADR-028)."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from telos_cardea._vendor import agencies
from telos_cardea.config import load_config

from conftest import CARDEA, DESCRIPTORS, PACKAGE_ROOT

CONFINED = DESCRIPTORS / "confined"
HOST_WORKERS = PACKAGE_ROOT / "deploy" / "cardea-host-workers.example.json"
TIMEOUTS = {"browser.open": 150, "browser.click": 150, "listings.rightmove_search": 180, "listings.zoopla_search": 180,
            "listings.agency_search": 180}
# The worker prints {"output": ...} with json.dumps (ASCII escapes); the helper refuses stdout over 256 KiB.
STDOUT_MARGIN_BYTES = 196608
# From cardea.hosts.launcher at the pinned commit.
HOSTNAME = r"(\*\.)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+"
READ_ONLY_PREFIXES = ("/usr/", "/opt/", "/etc/ssl/", "/etc/fonts/")
RESERVED_ENV = {"PATH", "HOME", "CARDEA_DEADLINE", "CARDEA_CREDENTIAL", "NO_PROXY"}


def entry_points() -> dict[str, str]:
    return dict(tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())["project"]["entry-points"]["cardea.operations"])


def confined_files() -> list[Path]:
    return sorted(CONFINED.glob("*.json"))


def host_workers() -> Any:
    return json.loads(HOST_WORKERS.read_text())


def allowed(host: str, hosts: list[str]) -> bool:
    return host in hosts or any(entry.startswith("*.") and host.endswith(entry[1:]) for entry in hosts)


def test_confined_set() -> None:
    assert {path.stem for path in confined_files()} == set(TIMEOUTS)


@pytest.mark.parametrize("path", confined_files(), ids=lambda p: p.stem)
def test_confined_variant_matches_in_process_descriptor(path: Path) -> None:
    confined = CARDEA.OperationDescriptor.model_validate_json(path.read_text())
    in_process = CARDEA.OperationDescriptor.model_validate_json((DESCRIPTORS / path.name).read_text())
    assert confined.name == path.stem and confined.tool == in_process.tool
    assert confined.cardea.effect == in_process.cardea.effect == "read"
    assert confined.cardea.handler is None and confined.cardea.executor.kind == "confined"
    assert confined.cardea.executor.profile == "browser"
    assert confined.cardea.timeout_seconds == TIMEOUTS[confined.name]
    assert confined.cardea.max_output_bytes <= STDOUT_MARGIN_BYTES


@pytest.mark.parametrize("path", confined_files(), ids=lambda p: p.stem)
def test_executor_names_an_example_worker_running_the_entry_point(path: Path) -> None:
    executor = CARDEA.OperationDescriptor.model_validate_json(path.read_text()).cardea.executor
    config = host_workers()
    worker = config["workers"][executor.worker]
    assert executor.profile in worker["profiles"] and executor.profile in config["profiles"]
    assert worker["argv"][-3:] == ["-m", "cardea.hosts.operation_worker", path.stem]
    assert path.stem in entry_points()
    env = worker["env"]
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/ms-playwright"
    if path.stem.startswith("listings."):
        # The private config file is not visible in the sandbox; everything comes from env.
        assert env["TELOS_CARDEA_BROWSER_FALLBACK"] == "1"
    assert config["profiles"][executor.profile]["wall_seconds"] >= TIMEOUTS[path.stem]


def test_example_config_respects_helper_rules() -> None:
    config = host_workers()
    profile = config["profiles"]["browser"]
    assert profile["jit"] is True and profile["output_kb"] * 1024 >= STDOUT_MARGIN_BYTES + 65536
    network = profile["network"]
    assert network["policy"] == "allow_list" and not network["proxy"].startswith("127.0.0.1:")
    assert 1 <= len(network["hosts"]) <= 64 and all(re.fullmatch(HOSTNAME, h) for h in network["hosts"])
    for worker in config["workers"].values():
        assert worker["argv"][0].startswith(("/usr/", "/opt/"))
        assert all(p.startswith(READ_ONLY_PREFIXES) for p in worker["read_only"])
        assert not any(k in RESERVED_ENV or k.endswith("_PROXY") for k in worker["env"])


def test_allow_list_covers_every_source() -> None:
    hosts = host_workers()["profiles"]["browser"]["network"]["hosts"]
    sources = ["www.rightmove.co.uk", "www.zoopla.co.uk", "challenges.cloudflare.com"]
    sources += [f"www.{impl.host}" for impl in agencies.REGISTRY.values()] + [impl.host for impl in agencies.REGISTRY.values()]
    assert [host for host in sources if not allowed(host, hosts)] == []


def test_real_launcher_accepts_example() -> None:
    launcher = pytest.importorskip("cardea.hosts.launcher", reason="real Cardea not installed (uv sync --group cardea)")
    config = host_workers()
    for entry in config["profiles"].values():
        launcher.validate_profile(entry)
    for entry in config["workers"].values():
        launcher.validate_worker(entry, config["profiles"])


def test_confined_settings_come_from_env_alone(tmp_path: Path) -> None:
    config = load_config({"TELOS_CARDEA_CONFIG": str(tmp_path / "unreadable-in-sandbox.toml"), "TELOS_CARDEA_BROWSER_FALLBACK": "1"})
    assert config.browser_fallback is True
