"""Codex selection, host tool execution, and subscription usage contracts."""
from types import SimpleNamespace

import pytest
from pontifex.codex import CodexRunConfig
from pontifex.codex_completion import CodexCompletion

from telos.agent import Agent
from telos.api.settings import Settings
from telos.goal import Goal
from telos.services.agent_runtime_v2 import _usage_from_completed
from telos.services.nightly.analyst import NightlyAnalyst
from telos.services.nightly.tool_definitions import ToolDefinition, ToolServer, codex_tools
from telos.services.nightly.tools import Toolset
from telos.transport.llm.pontifex import complete


def test_settings_select_all_codex_models_without_overwriting_claude():
    s = Settings(_env_file=None, model="claude-opus-4-8", utility_model="claude-haiku-4-5", codex_model="test-codex")
    assert s.telos_subscription_provider == "claude"
    s.telos_subscription_provider = "codex"
    assert s.runtime_model == s.runtime_utility_model == s.runtime_nightly_model == "test-codex"
    s.telos_subscription_provider = "claude"
    assert s.runtime_model == "claude-opus-4-8" and s.runtime_utility_model == "claude-haiku-4-5"


async def test_internal_jobs_use_codex_completion(tmp_path):
    agent = Agent(Goal("test", "test", system_prompt="test"), storage_root=tmp_path,
                  subscription_provider="codex", model="test-codex", utility_model="test-codex")
    assert isinstance(agent._pf_internal, CodexCompletion)
    assert agent._improver._pf is agent._pf_internal
    await agent._http.aclose()


async def test_nightly_uses_host_tool_schemas_and_handlers():
    called = []

    async def submit(args):
        called.append(args)
        return {"content": [{"type": "text", "text": "accepted"}]}

    class Session:
        async def run(self, *, config, **kwargs):
            assert isinstance(config, CodexRunConfig)
            assert not config.workspace_write
            spec = config.dynamic_tools[0]
            assert spec["name"] == "host__nightly__submit"
            await config.handlers[spec["name"]]({"score": 5})
            return SimpleNamespace(cost_usd=0, is_error=False, error=None, num_turns=2, structured_output={"ok": True})

    server = ToolServer("nightly", [ToolDefinition("submit", "Submit feedback", {"type": "object"}, submit)])
    toolset = Toolset({"nightly": server}, ["mcp__nightly__submit"], None, {"submit": submit})
    out = await NightlyAnalyst(model="test-codex", provider="codex", session=Session()).run_session(
        system_prompt="system", prompt="task", toolset=toolset)
    assert called == [{"score": 5}] and out.cost_usd == 0


def test_unknown_tools_fail_closed():
    with pytest.raises(ValueError, match="unregistered"):
        codex_tools({}, ["missing"])


def test_codex_usage_includes_cached_input_and_preserves_billing():
    usage = _usage_from_completed({"model": "test-codex", "provider": "openai", "billing_mode": "subscription",
                                   "cost_usd": 0, "usage": {"input_tokens": 40, "cache_read_input_tokens": 60, "output_tokens": 7}})
    assert usage["prompt_tokens"] == 100 and usage["completion_tokens"] == 7
    assert usage["model"] == "test-codex" and usage["cost_usd"] == 0


async def test_utility_collector_retains_codex_metadata():
    class Stream:
        async def stream(self, **kwargs):
            yield {"model": "test-codex", "billing_mode": "subscription", "cost_usd": 0,
                   "choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 30, "completion_tokens": 2}}

    out = await complete(Stream(), messages=[])
    assert out["billing_mode"] == "subscription" and out["model"] == "test-codex"
    assert out["cost_usd"] == 0 and out["usage"]["prompt_tokens"] == 30


def test_real_nightly_schemas_are_json_serializable():
    import json
    from types import SimpleNamespace

    from telos.services.nightly.tools import build_agent_toolset

    toolset = build_agent_toolset(SimpleNamespace(turns=[]))
    specs, _ = codex_tools(toolset.mcp_servers, toolset.allowed_tools)
    json.dumps(specs)
    get_turns = next(s for s in specs if s["name"].endswith("__get_turns"))
    assert get_turns["inputSchema"]["properties"]["page"]["type"] == "integer"
    assert get_turns["inputSchema"]["properties"]["page"]["description"]


def test_codex_tool_mapping_preserves_allowlist_scope():
    allowed_handler = object()
    forbidden_handler = object()
    server = ToolServer("nightly", [
        ToolDefinition("read", "Read", {"type": "object"}, allowed_handler),
        ToolDefinition("write", "Write", {"type": "object"}, forbidden_handler),
    ])
    allowed = ["mcp__nightly__read"]
    specs, handlers = codex_tools({"nightly": server}, allowed)
    assert [s["name"] for s in specs] == ["host__nightly__read"]
    assert handlers == {"host__nightly__read": allowed_handler}
    assert allowed == ["mcp__nightly__read"]
    with pytest.raises(ValueError, match="unregistered"):
        codex_tools({"nightly": server}, allowed + ["mcp__nightly__unknown"])


async def test_shared_nightly_tools_switch_between_codex_and_real_claude_sdk():
    import json

    from mcp.shared.memory import create_connected_server_and_client_session

    from telos.services.nightly.tool_definitions import claude_servers
    from telos.services.nightly.tools import ToolContext, build_agent_toolset

    toolset = build_agent_toolset(ToolContext(user_id="test", slug="test", day="2026-09-11", client=None))
    original_allowed = list(toolset.allowed_tools)
    specs, handlers = codex_tools(toolset.mcp_servers, toolset.allowed_tools)
    assert "host__nightly__get_turns" in handlers
    assert all(not spec["name"].startswith("mcp__") for spec in specs)
    await handlers["host__nightly__submit_agent_feedback"]({"score": 3, "summary": "Codex result"})
    assert toolset.result.feedback.score == 3

    # Exercise the actual Claude SDK MCP bridge, including schema validation
    # and handler dispatch, rather than only checking a mocked options object.
    server = claude_servers(toolset.mcp_servers)["nightly"]
    assert server["type"] == "sdk"
    async with create_connected_server_and_client_session(server["instance"]) as client:
        listed = await client.list_tools()
        get_turns = next(t for t in listed.tools if t.name == "get_turns")
        assert get_turns.inputSchema["properties"]["page"]["type"] == "integer"
        result = await client.call_tool("get_turns", {"page": 0})
        assert not result.isError
        assert json.loads(result.content[0].text)["total"] == 0
        result = await client.call_tool("submit_agent_feedback", {"score": 4, "summary": "Claude result"})
        assert not result.isError
        assert toolset.result.feedback.score == 4

    assert toolset.allowed_tools == original_allowed
    assert "mcp__nightly__submit_agent_feedback" in toolset.allowed_tools
    specs_again, handlers_again = codex_tools(toolset.mcp_servers, toolset.allowed_tools)
    assert specs_again == specs
    assert handlers_again.keys() == handlers.keys()
