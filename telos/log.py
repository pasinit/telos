"""Façade over the turns/prompts/improvement_runs repos in :mod:`telos.core.repos`.

Kept for back-compatibility. New code should construct the underlying
repos directly via :class:`telos.core.persistence.Database`. The
``ConversationLog`` instance also exposes the repos as public
attributes (``log.turns``, ``log.prompts``, ``log.runs``) for
incremental migration.
"""

from pathlib import Path

from telos.core.persistence import Database
from telos.core.repos.execution_events import ExecutionEventRepo
from telos.core.repos.prompts import PromptRepo
from telos.core.repos.runs import RunRepo
from telos.core.repos.turns import Turn, TurnRepo

__all__ = ["Turn", "ConversationLog"]


class ConversationLog:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        self.turns = TurnRepo(self._db)
        self.execution_events = ExecutionEventRepo(self._db)
        self.prompts = PromptRepo(self._db)
        self.runs = RunRepo(self._db)

    # ------------------------------------------------------------------
    # Turns
    # ------------------------------------------------------------------

    def append(self, user_message: str, assistant_response: str, prompt_sha: str,
               *, is_routine: bool = False, trace_id: str | None = None) -> str:
        return self.turns.append(
            user_message,
            assistant_response,
            prompt_sha,
            is_routine=is_routine,
            trace_id=trace_id,
        )

    def update_response(self, turn_id: str, assistant_response: str) -> None:
        self.turns.update_response(turn_id, assistant_response)

    def update_usage(self, turn_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.turns.update_usage(turn_id, prompt_tokens, completion_tokens)

    def add_usage(self, turn_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.turns.add_usage(turn_id, prompt_tokens, completion_tokens)

    def cumulative_usage(self) -> dict:
        return self.turns.cumulative_usage()

    def update_request_id(self, turn_id: str, request_id: str) -> None:
        self.turns.update_request_id(turn_id, request_id)

    def get_request_id(self, turn_id: str) -> str | None:
        return self.turns.get_request_id(turn_id)

    def turn_exists(self, turn_id: str) -> bool:
        return self.turns.turn_exists(turn_id)

    def update_signals(self, turn_id: str, *, corrected: bool, followed_up: bool, ignored: bool) -> None:
        self.turns.update_signals(turn_id, corrected=corrected, followed_up=followed_up, ignored=ignored)

    def last_turn_has_response(self) -> bool:
        return self.turns.last_turn_has_response()

    def recent_turns(self, n: int = 20) -> list[Turn]:
        return self.turns.recent_turns(n)

    def count(self) -> int:
        return self.turns.count()

    def signal_summary(self) -> dict:
        return self.turns.signal_summary()

    def last_turn_id(self) -> str | None:
        return self.turns.last_turn_id()

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def save_prompt(self, content: str) -> str:
        return self.prompts.save_prompt(content)

    def deploy_prompt(self, sha: str) -> None:
        self.prompts.deploy_prompt(sha)

    def prompt_history(self) -> list[dict]:
        return self.prompts.prompt_history()

    # ------------------------------------------------------------------
    # Improvement runs
    # ------------------------------------------------------------------

    def start_run(self, *, agent_slug: str, current_sha: str, model: str | None = None) -> int:
        return self.runs.start_run(agent_slug=agent_slug, current_sha=current_sha, model=model)

    def finish_run(self, run_id: int, **kwargs) -> None:
        self.runs.finish_run(run_id, **kwargs)

    def list_runs(self, **kwargs) -> list[dict]:
        return self.runs.list_runs(**kwargs)

    def get_run(self, run_id: int) -> dict | None:
        return self.runs.get_run(run_id)
