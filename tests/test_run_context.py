"""Unit tests for ``RunContext``.

The dataclass itself is trivial — these tests pin the frozen-ness and the
defaults so Phase 2b callers can rely on them.
"""

import dataclasses

import pytest

from telos.domain import DelegationGraph, RunContext


def test_construct_with_defaults():
    g = DelegationGraph()
    ctx = RunContext(user_id="alice", delegation_graph=g)
    assert ctx.user_id == "alice"
    assert ctx.delegation_graph is g
    assert ctx.image_url is None
    assert ctx.is_routine is False
    assert ctx.request_id is None
    assert ctx.correlation_id is None


def test_frozen_disallows_mutation():
    ctx = RunContext(user_id="alice", delegation_graph=DelegationGraph())
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.user_id = "mallory"  # type: ignore[misc]


def test_construct_with_all_fields():
    g = DelegationGraph()
    ctx = RunContext(
        user_id="alice",
        delegation_graph=g,
        image_url="data:image/png;base64,abc",
        is_routine=True,
        request_id="req_123",
        correlation_id="trace_abc",
    )
    assert ctx.image_url == "data:image/png;base64,abc"
    assert ctx.is_routine is True
    assert ctx.request_id == "req_123"
    assert ctx.correlation_id == "trace_abc"
