import json
import os
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

from telos_cardea import _cardea

import _cardea_operations as vendored
from conftest import CARDEA, PACKAGE_ROOT, descriptor_files, load_descriptor

EXPECTED = {
    "mail.send": "effect",
    "mail.preview": "read",
    "listings.rightmove_search": "read",
    "listings.zoopla_search": "read",
    "listings.agency_search": "read",
    "web.search": "read",
    "browser.open": "read",
    "browser.click": "read",
    "memory.recall": "read",
}


def entry_points() -> dict[str, str]:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())["project"]
    return dict(project["entry-points"]["cardea.operations"])


def test_descriptor_set_matches_expected_operations() -> None:
    assert {path.stem for path in descriptor_files()} == set(EXPECTED)


@pytest.mark.parametrize("path", descriptor_files(), ids=lambda p: p.stem)
def test_descriptor_validates_against_cardea(path: Path) -> None:
    descriptor = CARDEA.OperationDescriptor.model_validate_json(path.read_text())
    assert descriptor.name == path.stem
    assert descriptor.cardea.effect == EXPECTED[descriptor.name]
    # File content is plain JSON a host owner can review and copy into operations_dir.
    assert json.loads(path.read_text())["tool"]["name"] == path.stem


@pytest.mark.parametrize("path", descriptor_files(), ids=lambda p: p.stem)
def test_handlers_and_reconcilers_are_registered_entry_points(path: Path) -> None:
    descriptor = CARDEA.OperationDescriptor.model_validate_json(path.read_text())
    points = entry_points()
    for name in filter(None, (descriptor.cardea.handler, descriptor.cardea.reconciler)):
        module, _, attr = points[name].partition(":")
        assert callable(getattr(import_module(module), attr))


def test_every_entry_point_is_used_by_a_descriptor() -> None:
    used = set()
    for path in descriptor_files():
        binding = CARDEA.OperationDescriptor.model_validate_json(path.read_text()).cardea
        used |= {binding.handler, binding.reconciler} - {None}
    assert used == set(entry_points())


def test_mail_send_scope_is_one_email_per_recipient() -> None:
    binding = load_descriptor("mail.send").cardea
    assert binding.idempotency_scope == ("/to",)
    assert binding.reconciler == "mail.send.reconcile"


def test_preview_shares_send_input_schema() -> None:
    assert load_descriptor("mail.preview").input_schema == load_descriptor("mail.send").input_schema


def test_read_operation_with_scope_is_refused() -> None:
    raw = json.loads((PACKAGE_ROOT / "descriptors" / "web.search.json").read_text())
    raw["cardea"]["idempotency_scope"] = ["/query"]
    with pytest.raises(ValueError):
        CARDEA.OperationDescriptor.model_validate(raw)


def test_shim_uses_cardea_classes_when_installed() -> None:
    if CARDEA is vendored:
        pytest.skip("real Cardea not installed (uv sync --group cardea)")
    assert _cardea.OperationRejected is CARDEA.OperationRejected
    assert _cardea.OperationContext is CARDEA.OperationContext
    assert _cardea.Reconciliation is CARDEA.Reconciliation


def test_cardea_registry_loads_descriptors_and_entry_points(tmp_path: Path) -> None:
    """Cardea's own loader: private descriptor files plus installed entry points."""
    if CARDEA is vendored:
        pytest.skip("real Cardea not installed (uv sync --group cardea)")
    for path in descriptor_files():
        target = tmp_path / path.name
        target.write_bytes(path.read_bytes())
        os.chmod(target, 0o600)
    registry = CARDEA.Registry.load(tmp_path)
    assert set(registry.descriptors) == set(EXPECTED)
    assert registry.reconciler("mail.send") is not None
    first = registry.scope_key("mail.send", "telos-owner", "slot-1", {"to": "a@example.com", "subject": "x", "body": "y"})
    reworded = registry.scope_key("mail.send", "telos-owner", "slot-1", {"to": "a@example.com", "subject": "z", "body": "w"})
    other = registry.scope_key("mail.send", "telos-owner", "slot-1", {"to": "b@example.com", "subject": "x", "body": "y"})
    assert first == reworded != other
