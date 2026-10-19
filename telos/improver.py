"""Self-improvement pipeline: agentic research → candidate prompt → LLM judge evaluation."""
import difflib
import hashlib
import json
import logging
import re
from dataclasses import dataclass

from pontifex import Pontifex

from telos.costs import CostLog
from telos.log import ConversationLog
from telos.pricing import cost_for
from telos.transport.llm.pontifex import complete

log = logging.getLogger("telos.improver")

AUTO_DEPLOY_THRESHOLD = 0.10
"""Min ``score_delta`` (judge wins above 50%) to auto-deploy a candidate."""


@dataclass
class CandidatePrompt:
    sha: str
    content: str
    rationale: str
    score_delta: float  # positive = better than current
    run_id: int | None = None  # links to improvement_runs row


def _unified_diff(current: str, proposed: str, current_sha: str, proposed_sha: str) -> str:
    """Return a unified diff between two prompts. Empty string when inputs are equal."""
    if current == proposed:
        return ""
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"current ({current_sha})",
        tofile=f"proposed ({proposed_sha})",
        n=3,
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Internal toolset — schemas + dispatch for the Improver's research loop
# ---------------------------------------------------------------------------


_GET_RECENT_TURNS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "get_recent_turns",
        "description": "Fetch recent conversation turns with quality signals (corrected, followed_up, ignored).",
        "parameters": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of turns (max 50)", "default": 20}
            },
        },
    },
}

_GET_SIGNAL_SUMMARY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "get_signal_summary",
        "description": "Get aggregate quality stats: correction_rate, follow_up_rate, ignored_rate.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_GET_PROMPT_HISTORY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "get_prompt_history",
        "description": "Get past prompt versions with deployment dates and scores.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_PROPOSE_CANDIDATE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "propose_candidate",
        "description": "Propose a new system prompt. Call only when you have evidence-backed improvements.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The full new system prompt"},
                "rationale": {"type": "string", "description": "What changed and why, referencing specific signals"},
            },
            "required": ["prompt", "rationale"],
        },
    },
}


class _ImproverToolset:
    """Schemas + dispatch for the read-only tools the research LLM may call.

    Mirrors the shape of :class:`telos.tools.ToolRepository` (``schemas`` →
    OpenAI function specs, ``dispatch`` → name + args → result), but is
    instance-bound to one :class:`ConversationLog` so each Improver gets
    its own toolset.
    """

    def __init__(self, log: ConversationLog) -> None:
        self._log = log

    def schemas(self) -> list[dict]:
        return [
            _GET_RECENT_TURNS_SCHEMA,
            _GET_SIGNAL_SUMMARY_SCHEMA,
            _GET_PROMPT_HISTORY_SCHEMA,
            _PROPOSE_CANDIDATE_SCHEMA,
        ]

    def dispatch(self, name: str, args: dict) -> object:
        if name == "get_recent_turns":
            n = min(int(args.get("n", 20)), 50)
            return [
                {
                    "user": t.user_message[:300],
                    "assistant": t.assistant_response[:300],
                    "corrected": t.corrected,
                    "followed_up": t.followed_up,
                    "ignored": t.ignored,
                }
                for t in self._log.recent_turns(n)
            ]
        if name == "get_signal_summary":
            return self._log.signal_summary()
        if name == "get_prompt_history":
            return self._log.prompt_history()
        return {}


class Improver:
    """
    Runs one iteration of the improvement loop:
      1. An agentic Claude call researches the conversation log
      2. It proposes a candidate prompt
      3. An LLM judge evaluates the candidate against the last N turns
      4. Returns the candidate if it wins, None otherwise
    """

    _JUDGE_MODEL = "claude-haiku-4-5"  # cheap, fast judge
    _MAX_RESEARCH_ITERATIONS = 10  # outer tool-call rounds inside _research
    _REPLAY_TURNS = 10  # last-N turns judged when evaluating a candidate
    _JUDGE_WINNER_RE = re.compile(r'"winner"\s*:\s*"([AB])"')

    def __init__(
        self,
        log: ConversationLog,
        costs: CostLog,
        pf: Pontifex,
        model: str = "claude-sonnet-4-6",
        agent_slug: str = "",
    ) -> None:
        self._log = log
        self._costs = costs
        self._pf = pf
        self._model = model
        self._agent_slug = agent_slug
        self._toolset = _ImproverToolset(log)
        # Set by ``run`` so tool-call cost events can reference the active
        # improvement-run row. Reset back to None when the run finishes.
        self._active_run_id: int | None = None

    # ------------------------------------------------------------------
    # Internal: cost accounting
    # ------------------------------------------------------------------

    def _record_response_cost(self, response: dict, source: str, model: str) -> None:
        """Persist an OpenAI-format response's usage to the cost ledger.

        Never raises — instrumentation must not break the improvement loop.
        """
        try:
            usage = response.get("usage") if isinstance(response, dict) else None
            if usage is None:
                return
            in_tok = int(usage.get("prompt_tokens", 0) or 0)
            out_tok = int(usage.get("completion_tokens", 0) or 0)
            self._costs.record_cost(
                source=source,
                model=response.get("model") or model,
                prompt_tokens=in_tok,
                completion_tokens=out_tok,
                cost_usd=0.0 if response.get("billing_mode") == "subscription" else cost_for(model, in_tok, out_tok),
                turn_id=None,
                run_id=self._active_run_id,
            )
        except Exception:
            log.exception("failed to record improver cost event (source=%s)", source)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self, current_prompt: str) -> CandidatePrompt | None:
        """Run 1 iteration. Returns a CandidatePrompt or None.

        Persists every invocation to the ``improvement_runs`` table —
        including the no-op cases the judge currently returns silently
        and any exceptions raised mid-loop.
        """
        current_sha = hashlib.sha256(current_prompt.encode()).hexdigest()[:12]
        run_id = self._log.start_run(
            agent_slug=self._agent_slug,
            current_sha=current_sha,
            model=self._model,
        )
        self._active_run_id = run_id
        try:
            proposed = await self._research(current_prompt)
            if proposed is None:
                self._log.finish_run(
                    run_id,
                    decision="no_improvement",
                    judge_summary="research loop returned no candidate",
                )
                return None

            score_delta = await self._evaluate(current_prompt, proposed["prompt"])
            proposed_sha = hashlib.sha256(proposed["prompt"].encode()).hexdigest()[:12]
            diff = _unified_diff(current_prompt, proposed["prompt"], current_sha, proposed_sha)

            if score_delta <= 0:
                self._log.finish_run(
                    run_id,
                    decision="rejected",
                    proposed_sha=proposed_sha,
                    rationale=proposed.get("rationale"),
                    proposed_diff=diff,
                    score_delta=score_delta,
                    judge_summary="judge preferred the current prompt",
                )
                return None

            candidate = CandidatePrompt(
                sha=proposed_sha,
                content=proposed["prompt"],
                rationale=proposed.get("rationale", ""),
                score_delta=score_delta,
                run_id=run_id,
            )
            # 'pending' until the agent's _on_candidate decides to deploy
            # or stage. The decision will be flipped to 'approved' by the
            # agent when it auto-deploys; otherwise it stays 'pending'
            # until the user acts in the UI.
            self._log.finish_run(
                run_id,
                decision="pending",
                proposed_sha=proposed_sha,
                rationale=candidate.rationale,
                proposed_diff=diff,
                score_delta=score_delta,
                judge_summary=f"candidate wins {(score_delta + 0.5) * 100:.0f}% of judged turns",
            )
            return candidate
        except Exception as exc:
            log.exception("improvement run %s failed", run_id)
            try:
                self._log.finish_run(
                    run_id,
                    decision="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                log.exception("failed to record error outcome on run %s", run_id)
            raise
        finally:
            self._active_run_id = None

    # ------------------------------------------------------------------
    # Step 1: agentic research → candidate proposal
    # ------------------------------------------------------------------

    async def _research(self, current_prompt: str) -> dict | None:
        """Drive an agentic loop that reads conversation history and proposes a candidate.

        The research LLM has access to ``_ImproverToolset`` (read-only views
        over the conversation log + the ``propose_candidate`` exit tool).
        Stops as soon as ``propose_candidate`` is called or the model emits
        a turn with no tool calls. Returns the candidate dict
        (``{"prompt": ..., "rationale": ...}``) or None if the loop ended
        without a proposal.
        """
        tools = self._toolset.schemas()
        messages: list[dict] = [
            {"role": "user", "content": self._system_research_prompt(current_prompt)}
        ]

        proposed: dict | None = None

        for _ in range(self._MAX_RESEARCH_ITERATIONS):
            response = await complete(
                self._pf,
                messages=messages,
                tools=tools,
                max_tokens=4096,
            )
            self._record_response_cost(response, "improver_research", self._model)

            msg = response["choices"][0]["message"]
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": msg.get("tool_calls") or []})

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                break

            tool_results = []
            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                if name == "propose_candidate":
                    proposed = args
                    result_content = json.dumps({"status": "staged"})
                else:
                    result_content = json.dumps(self._toolset.dispatch(name, args))

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_content,
                })

            messages.extend(tool_results)

            if proposed is not None:
                break  # done once a candidate is proposed

        return proposed

    def _system_research_prompt(self, current_prompt: str) -> str:
        return (
            "You are improving a personal AI assistant's system prompt.\n\n"
            f"<current_prompt>\n{current_prompt}\n</current_prompt>\n\n"
            "Use the tools to:\n"
            "1. Fetch recent conversation turns and study the quality signals (corrections, follow-ups, ignored)\n"
            "2. Check signal summary stats for overall trends\n"
            "3. Review prompt history to avoid repeating failed approaches\n"
            "4. Propose an improved prompt as a targeted diff — change only what evidence supports\n\n"
            "Call `propose_candidate` once you have a well-evidenced improvement. "
            "If the data shows no clear improvement opportunity, explain why and do NOT call propose_candidate."
        )

    # ------------------------------------------------------------------
    # Step 2: LLM judge evaluation
    # ------------------------------------------------------------------

    async def _evaluate(self, current_prompt: str, candidate_prompt: str) -> float:
        """Replay last N turns. LLM judge picks winner per turn. Returns score delta.

        Known limitation: the candidate's response is generated *without*
        the tools, Recordatio briefing, and turn-by-turn context that produced
        the recorded response, so the comparison is asymmetric. A real
        eval harness with parallel context belongs in a follow-up
        (see Phase 6 in ``docs/architecture/REDESIGN.md``).
        """
        turns = [
            t for t in self._log.recent_turns(self._REPLAY_TURNS)
            if t.user_message.strip() and t.assistant_response.strip()
        ]
        if not turns:
            return 0.0

        agent_context = current_prompt[:200]

        wins = 0
        for turn in turns:
            # Simulate candidate response
            sim = await complete(
                self._pf,
                messages=[{"role": "user", "content": turn.user_message}],
                system=candidate_prompt,
                max_tokens=512,
            )
            self._record_response_cost(sim, "improver_replay", self._JUDGE_MODEL)
            candidate_response = sim["choices"][0]["message"].get("content") or ""

            # Judge: does the candidate beat the recorded response?
            if await self._candidate_wins(turn.user_message, turn.assistant_response, candidate_response, agent_context):
                wins += 1

        return wins / len(turns) - 0.5  # >0 means candidate wins majority

    async def _candidate_wins(self, user_msg: str, recorded: str, candidate: str, agent_context: str = "") -> bool | None:
        """Ask the judge whether ``candidate`` beats ``recorded``. None on parse failure (counted as a non-win)."""
        result = await complete(
            self._pf,
            messages=[{
                "role": "user",
                "content": (
                    f"User: {user_msg}\n\n"
                    f"Response A:\n{recorded}\n\n"
                    f"Response B:\n{candidate}"
                ),
            }],
            system=(
                'You compare two AI assistant responses. '
                'Reply with exactly one JSON object: {"winner": "A"} or {"winner": "B"}. '
                'Pick the response that is more helpful, specific, and well-suited to the user.\n\n'
                f'Agent context (goal/description): {agent_context}'
            ),
            max_tokens=128,
        )
        self._record_response_cost(result, "improver_judge", self._JUDGE_MODEL)
        text = result["choices"][0]["message"].get("content") or ""
        match = self._JUDGE_WINNER_RE.search(text)
        if not match:
            log.warning("judge returned unparseable verdict; counting as non-win: %r", text[:200])
            return None
        return match.group(1) == "B"
