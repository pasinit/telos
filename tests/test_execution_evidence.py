import json

import pytest

from telos.log import ConversationLog
from telos.services.nightly.schemas import AgentFeedback
from telos.services.nightly.tools import ToolContext, build_agent_toolset


def test_evidence_survives_reload_redacts_credentials_and_rejects_wrong_turn(tmp_path):
    path = tmp_path / "history.db"
    history = ConversationLog(path)
    turn = history.append("request", "response", "sha")
    history.execution_events.append(turn, "tool_call", {"input": {"api_key": "secret", "body": "full email"}})
    evidence = ConversationLog(path).execution_events.page(turn)
    assert evidence["events"][0]["payload"]["input"] == {"api_key": "[redacted]", "body": "full email"}
    with pytest.raises(ValueError, match="existing turn"):
        history.execution_events.append("other", "tool_result", {})


async def test_evaluator_reads_evidence_only_for_selected_turns(tmp_path):
    history = ConversationLog(tmp_path / "history.db")
    turn = history.append("request", "response", "sha")
    history.execution_events.append(turn, "email_delivery", {"body": "actual email", "status": "accepted_by_smtp"})
    toolset = build_agent_toolset(ToolContext(user_id="u", slug="a", day="2026-09-12", client=None,
                                 turns=history.recent_turns(), event_reader=history.execution_events.page))
    read = toolset.handlers["get_execution_events"]
    result = await read({"turn_id": turn, "offset": 0, "limit": 5})
    assert json.loads(result["content"][0]["text"])["events"][0]["payload"]["body"] == "actual email"
    assert (await read({"turn_id": "other"}))["is_error"]
    assert AgentFeedback(score=4, summary="Old feedback").task_completion == "unknown"
