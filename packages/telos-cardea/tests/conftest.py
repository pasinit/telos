import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from telos_cardea._cardea import OperationContext
from telos_cardea.config import Config, RecordatioScope, SmtpConfig

import _cardea_operations as vendored

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DESCRIPTORS = PACKAGE_ROOT / "descriptors"

try:  # Prefer the real Cardea (`uv sync --group cardea`); fall back to the vendored copy.
    import cardea.service.operations as cardea_operations

    CARDEA: ModuleType = cardea_operations
except ImportError:
    CARDEA = vendored

EFFECT_KEY = "a" * 64


def descriptor_files() -> list[Path]:
    return sorted(DESCRIPTORS.glob("*.json"))


def load_descriptor(name: str) -> Any:
    return CARDEA.OperationDescriptor.model_validate_json((DESCRIPTORS / f"{name}.json").read_text())


def assert_output(name: str, output: object) -> None:
    """The handler output passes the same check Cardea applies before settling."""
    descriptor = load_descriptor(name)
    assert vendored.check_output(descriptor, output) is not None, json.dumps(output, default=str)[:2000]


def assert_input(name: str, arguments: dict[str, Any]) -> None:
    assert vendored.valid_arguments(load_descriptor(name), arguments), arguments


@pytest.fixture
def make_context() -> Callable[..., OperationContext]:
    def make(namespace: str = "telos-owner", effect_key: str = EFFECT_KEY, deadline: datetime | None = None) -> OperationContext:
        return OperationContext(task_id="t1", namespace=namespace, receipt_id="r1",
                                deadline=deadline or datetime.now(UTC) + timedelta(minutes=1), effect_key=effect_key)

    return make


@pytest.fixture
def config() -> Config:
    return Config(
        smtp=SmtpConfig(host="smtp-relay.brevo.com", port=587, user="login@example.com", password="secret", from_addr="Telos <telos@example.org>"),
        brevo_api_key="brevo-key",
        recordatio_url="http://recordatio.test:8787",
        recordatio_api_key="chat-key",
        recordatio_scopes={"telos-owner": RecordatioScope(user_id="owner", slug="homey")},
        search_engine="serper",
        serper_key="serper-key",
    )
