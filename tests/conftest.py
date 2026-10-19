"""Shared test fixtures.

Recordatio mode is unconditional (Phase 6 of the migration): every
``AgentRuntime.get`` seeds Recordatio's AgentConfig (GET, PUT on 404)
before caching the agent. Unit tests don't run a Recordatio, so the seed
is stubbed out by default — otherwise every test that resolves an agent
through the runtime would fail with ``RecordatioUnavailableError``.

Tests that exercise the seed path itself re-patch
``telos.services.agent_runtime.seed_agent_config_if_missing`` inside
their own scope (an inner ``with patch(...)`` overrides this stub and
restores it on exit). Tests for the seed *implementation* import it
from ``telos.services.recordatio_agent_seed`` directly, which this stub
does not touch.
"""
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _stub_recordatio_config_seed(monkeypatch):
    monkeypatch.setattr(
        "telos.services.agent_runtime.seed_agent_config_if_missing",
        AsyncMock(return_value=False),
    )
