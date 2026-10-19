"""Unit tests for ``DelegationGraph`` cycle-prevention.

These tests exercise the ``ContextVar`` propagation logic in isolation —
no Agent, no chat loop. Phase 2a acceptance criterion.
"""

import asyncio

import pytest

from telos.core.delegation import DelegationGraph


def test_active_starts_empty():
    graph = DelegationGraph()
    assert graph.active() == frozenset()


def test_enter_adds_and_exit_removes():
    graph = DelegationGraph()
    with graph.enter("alice"):
        assert graph.active() == frozenset({"alice"})
    assert graph.active() == frozenset()


def test_enter_is_reentrant_for_different_slugs():
    graph = DelegationGraph()
    with graph.enter("alice"):
        with graph.enter("bob"):
            assert graph.active() == frozenset({"alice", "bob"})
        assert graph.active() == frozenset({"alice"})
    assert graph.active() == frozenset()


def test_contains_reflects_current_chain():
    graph = DelegationGraph()
    assert graph.contains("alice") is False
    with graph.enter("alice"):
        assert graph.contains("alice") is True
        assert graph.contains("bob") is False


def test_cyclic_subset_returns_only_active_slugs():
    graph = DelegationGraph()
    with graph.enter("alice"):
        with graph.enter("bob"):
            assert graph.cyclic_subset(["alice", "carol", "bob"]) == ["alice", "bob"]
            assert graph.cyclic_subset(["carol", "dave"]) == []


def test_chain_restored_when_body_raises():
    graph = DelegationGraph()
    with pytest.raises(RuntimeError):
        with graph.enter("alice"):
            assert graph.contains("alice")
            raise RuntimeError("boom")
    assert graph.active() == frozenset()


def test_isolated_instances_do_not_share_state():
    """Two ``DelegationGraph`` instances each own their own ``ContextVar``."""
    a = DelegationGraph()
    b = DelegationGraph()
    with a.enter("alice"):
        assert a.contains("alice")
        assert not b.contains("alice")  # different ContextVar object


@pytest.mark.asyncio
async def test_sibling_tasks_do_not_see_each_others_additions():
    """``asyncio.gather`` siblings inherit the parent chain but don't leak to each other.

    This is the property that makes ``coordinate`` (parallel fan-out) safe:
    each child task sees the parent's chain, but neither child sees the
    other child's slug.
    """
    graph = DelegationGraph()
    observed: dict[str, frozenset[str]] = {}

    async def child(slug: str) -> None:
        # Yield to the scheduler so the other task races on its own context.
        await asyncio.sleep(0)
        with graph.enter(slug):
            await asyncio.sleep(0)
            observed[slug] = graph.active()

    with graph.enter("parent"):
        await asyncio.gather(child("alpha"), child("beta"))

    assert observed["alpha"] == frozenset({"parent", "alpha"})
    assert observed["beta"] == frozenset({"parent", "beta"})
    # And the parent context still only has "parent" after both children exit.
    with graph.enter("parent"):
        pass
    assert graph.active() == frozenset()


@pytest.mark.asyncio
async def test_child_sees_parent_chain():
    """A nested ``await`` sees the parent's active chain (the property
    ``delegate_to`` relies on to detect cycles)."""
    graph = DelegationGraph()
    seen: frozenset[str] = frozenset()

    async def child() -> None:
        nonlocal seen
        await asyncio.sleep(0)
        seen = graph.active()

    with graph.enter("alice"):
        await child()
    assert seen == frozenset({"alice"})
