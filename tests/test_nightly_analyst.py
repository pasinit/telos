"""Tests for NightlyAnalyst — the thin adapter over pontifex.agent.AgentSession.

The nightly service/synthesizer tests use a fake analyst; these lock the real
adapter's behaviour: that it builds the subscription/forced-tool config the way
the old inline runner did and maps pontifex's outcome back to telos's
``SessionOutcome``. Injects fake ``query_fn``/``options_cls`` so no CLI runs.
"""
from telos.services.nightly.analyst import NightlyAnalyst, SessionOutcome
from telos.services.nightly.tools import Toolset


class ResultMessage:
    """Stand-in whose class name matches what pontifex.agent dispatches on."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _OptionsRecorder:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def __call__(self, **kw):
        self.kwargs = kw
        return {"_opts": kw}


def _query(result):
    async def q(*, prompt, options):
        yield result
    return q


async def test_analyst_builds_subscription_config_and_maps_outcome() -> None:
    rec = _OptionsRecorder()
    sentinel = object()
    result = ResultMessage(
        total_cost_usd=1.25, num_turns=4, subtype="success", is_error=False,
        usage={}, result="ok", structured_output=None,
    )
    analyst = NightlyAnalyst(model="claude-opus-4-8", max_turns=7, query_fn=_query(result), options_cls=rec)
    toolset = Toolset(mcp_servers={"x": sentinel}, allowed_tools=["mcp__x__submit"], result=None, handlers={})

    out = await analyst.run_session(system_prompt="sys", prompt="go", toolset=toolset)

    # Subscription env + forced-tool options built as the inline runner did.
    assert rec.kwargs["env"] == {"ANTHROPIC_API_KEY": "", "IS_SANDBOX": "1"}
    assert rec.kwargs["permission_mode"] == "bypassPermissions"
    assert rec.kwargs["model"] == "claude-opus-4-8"
    assert rec.kwargs["max_turns"] == 7
    assert rec.kwargs["mcp_servers"] == {"x": sentinel}
    assert rec.kwargs["allowed_tools"] == ["mcp__x__submit"]
    assert "output_format" not in rec.kwargs  # forced-tool path, no native schema
    # Outcome mapped back to telos's SessionOutcome.
    assert isinstance(out, SessionOutcome)
    assert out.cost_usd == 1.25
    assert out.num_turns == 4
    assert out.is_error is False


async def test_analyst_maps_error_outcome() -> None:
    result = ResultMessage(
        total_cost_usd=0.0, num_turns=1, subtype="error_during_execution",
        is_error=True, errors=["boom"], usage={}, result=None, structured_output=None,
    )
    analyst = NightlyAnalyst(model="m", query_fn=_query(result), options_cls=_OptionsRecorder())
    toolset = Toolset(mcp_servers={}, allowed_tools=[], result=None, handlers={})

    out = await analyst.run_session(system_prompt="s", prompt="p", toolset=toolset)

    assert out.is_error is True
    assert out.error == "boom"
