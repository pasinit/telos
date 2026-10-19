"""Tests for telos.pricing.cost_for and telos.costs.CostLog."""
from datetime import UTC, datetime, timedelta

import pytest

from telos.costs import CostLog
from telos.pricing import cost_for

# ──────────────────────────────────────────────────────────────────────
# Pricing
# ──────────────────────────────────────────────────────────────────────


def test_cost_for_sonnet_basic():
    # 1M input + 1M output of Sonnet → $3 + $15 = $18
    assert cost_for("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_cost_for_haiku_basic():
    # 1M input + 1M output of Haiku → $0.80 + $4.00 = $4.80
    assert cost_for("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(4.80)


def test_cost_for_dated_alias():
    # Dated and undated should agree.
    a = cost_for("claude-haiku-4-5", 1234, 5678)
    b = cost_for("claude-haiku-4-5-20251001", 1234, 5678)
    assert a == b
    assert a > 0


def test_cost_for_unknown_model_returns_zero():
    assert cost_for("totally-fake-model", 1_000_000, 1_000_000) == 0.0


def test_cost_for_empty_model_returns_zero():
    assert cost_for("", 1_000_000, 1_000_000) == 0.0


def test_cost_for_zero_tokens_is_zero():
    assert cost_for("claude-sonnet-4-6", 0, 0) == 0.0


def test_cost_for_negative_tokens_clamped_to_zero():
    # Defensive: never produce a negative cost.
    assert cost_for("claude-sonnet-4-6", -10, -20) == 0.0


def test_cost_for_partial_tokens():
    # 1000 in + 2000 out on Sonnet = (1000*3 + 2000*15)/1M = 0.033
    assert cost_for("claude-sonnet-4-6", 1000, 2000) == pytest.approx(0.033)


def test_cost_for_unknown_model_with_dated_fallback():
    # An unknown dated alias of a known base model should fall back.
    # claude-sonnet-4-6 is in the table; suffix ``-20990101`` is not.
    val = cost_for("claude-sonnet-4-6-20990101", 1_000_000, 0)
    # Falls back by stripping the dated suffix.
    assert val == pytest.approx(3.0)


def test_cost_for_provider_prefix_stripped():
    # Recordatio routes via ``provider/model`` (e.g. ``anthropic/claude-sonnet-4-6``).
    # Telos records ``self.model`` verbatim in costs.db, so the pricing
    # fallback must accept the prefixed form.
    assert cost_for("anthropic/claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)
    assert cost_for("openai/gpt-4o", 1_000_000, 0) == pytest.approx(2.50)


def test_cost_for_provider_prefix_with_dated_suffix():
    # Combine: ``anthropic/claude-sonnet-4-6-20990101`` should strip both
    # the ``anthropic/`` prefix and the dated suffix.
    val = cost_for("anthropic/claude-sonnet-4-6-20990101", 1_000_000, 0)
    assert val == pytest.approx(3.0)


def test_cost_for_unknown_model_emits_warning_once(caplog):
    # Reset the once-only de-dup set so the warning is observable.
    from telos.pricing import _default_table
    _default_table.reset_warnings()

    with caplog.at_level("WARNING", logger="telos.pricing"):
        cost_for("totally-fake-model-xyz", 100, 100)
        cost_for("totally-fake-model-xyz", 200, 200)  # second call: no extra warning

    matches = [r for r in caplog.records if "totally-fake-model-xyz" in r.message]
    assert len(matches) == 1
    assert "no pricing entry" in matches[0].message


# ──────────────────────────────────────────────────────────────────────
# CostLog
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def costs(tmp_path) -> CostLog:
    return CostLog(tmp_path / "costs.db")


def test_record_and_summary_empty(costs):
    summary = costs.cost_summary()
    assert summary == {"total_usd": 0.0, "by_source": {}, "by_model": {}}


def test_record_single_event(costs):
    costs.record_cost(
        source="chat",
        model="claude-sonnet-4-6",
        prompt_tokens=1000,
        completion_tokens=500,
        cost_usd=0.0105,
        turn_id="turn-1",
    )
    s = costs.cost_summary()
    assert s["total_usd"] == pytest.approx(0.0105)
    assert s["by_source"]["chat"]["calls"] == 1
    assert s["by_source"]["chat"]["input_tokens"] == 1000
    assert s["by_source"]["chat"]["output_tokens"] == 500
    assert s["by_source"]["chat"]["total_usd"] == pytest.approx(0.0105)
    assert s["by_model"]["claude-sonnet-4-6"]["calls"] == 1


def test_summary_aggregates_by_source(costs):
    costs.record_cost("chat", "claude-sonnet-4-6", 100, 50, 0.001)
    costs.record_cost("chat", "claude-sonnet-4-6", 200, 100, 0.002)
    costs.record_cost("signal_classify", "claude-haiku-4-5", 50, 10, 0.0001)
    costs.record_cost("improver_judge", "claude-haiku-4-5", 80, 20, 0.0002)

    s = costs.cost_summary()
    assert s["by_source"]["chat"]["calls"] == 2
    assert s["by_source"]["chat"]["input_tokens"] == 300
    assert s["by_source"]["chat"]["output_tokens"] == 150
    assert s["by_source"]["chat"]["total_usd"] == pytest.approx(0.003)
    assert s["by_source"]["signal_classify"]["calls"] == 1
    assert s["by_source"]["improver_judge"]["calls"] == 1
    assert s["total_usd"] == pytest.approx(0.0033)


def test_summary_aggregates_by_model(costs):
    costs.record_cost("chat", "claude-sonnet-4-6", 100, 50, 0.001)
    costs.record_cost("summary", "claude-haiku-4-5", 100, 50, 0.0001)
    costs.record_cost("improver_research", "claude-sonnet-4-6", 100, 50, 0.001)

    s = costs.cost_summary()
    assert s["by_model"]["claude-sonnet-4-6"]["calls"] == 2
    assert s["by_model"]["claude-sonnet-4-6"]["total_usd"] == pytest.approx(0.002)
    assert s["by_model"]["claude-haiku-4-5"]["calls"] == 1


def test_summary_since_filter(costs):
    # Insert two rows manually so we can control timestamps.
    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    new_ts = datetime.now(UTC).isoformat()
    with costs._connect() as conn:
        conn.execute(
            "INSERT INTO cost_events (ts, source, model, prompt_tokens, "
            "completion_tokens, cost_usd, turn_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (old_ts, "chat", "claude-sonnet-4-6", 100, 50, 0.001, None),
        )
        conn.execute(
            "INSERT INTO cost_events (ts, source, model, prompt_tokens, "
            "completion_tokens, cost_usd, turn_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_ts, "chat", "claude-sonnet-4-6", 200, 100, 0.005, None),
        )

    all_time = costs.cost_summary()
    assert all_time["total_usd"] == pytest.approx(0.006)

    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    recent = costs.cost_summary(since=cutoff)
    assert recent["total_usd"] == pytest.approx(0.005)
    assert recent["by_source"]["chat"]["calls"] == 1


def test_record_handles_none_values(costs):
    # Defensive: callers may pass None/empty for unknown turn_id; should
    # not raise and should coerce numerics to int/float.
    costs.record_cost("chat", "claude-sonnet-4-6", 0, 0, 0.0, None)
    s = costs.cost_summary()
    assert s["total_usd"] == 0.0
    assert s["by_source"]["chat"]["calls"] == 1


def test_record_from_multiple_threads(costs):
    """CostLog is written from the chat coroutine, executor threads, and
    the Improver background thread; verify cross-thread writes work."""
    import threading

    def _write(i: int) -> None:
        costs.record_cost("chat", "claude-sonnet-4-6", i, i, 0.001)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    s = costs.cost_summary()
    assert s["by_source"]["chat"]["calls"] == 10
    assert s["by_source"]["chat"]["input_tokens"] == sum(range(10))
