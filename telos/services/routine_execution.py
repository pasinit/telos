"""Routine execution service.

Encapsulates the two callbacks that ``RoutineRunner`` calls on each fire:
running a routine message through the agent, and (optionally) sending the
result by email.

Was inlined as ``_execute_routine`` and ``_execute_routine_with_email`` in
:mod:`telos.api.server`.
"""
import asyncio
import logging
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from telos.local_time import local_now
from telos.log import ConversationLog
from telos.routine_invocation import RoutineInvocation, current_invocation
from telos.services.agent_runtime import AgentRuntime
from telos.services.email_notifications import EmailNotificationService
from telos.services.routine_context import (
    RecallFn,
    delivery_history_block,
    history_path,
    ledger_summary_block,
    reserved_recall,
    reserved_recall_block,
)

log = logging.getLogger(__name__)
_current_turn: ContextVar[str | None] = ContextVar("routine_execution_turn", default=None)


# Bounded retry for *transient* routine stream failures. An unattended daily
# brief is idempotent-enough that simply re-running the turn is safe, and the
# failures retried here are transient by nature: a mid-SSE server disconnect
# (surfaced by ``agent_runtime_v2`` as ``stream_error`` reason
# ``transport:<ExcName>`` — e.g. ``transport:RemoteProtocolError``) or a stream
# that ended without a terminal event (reason ``truncated``). Observed twice in
# production (jobber 2026-08-11, homey 2026-08-12) crashing the whole run and
# delivering ZERO output — only a FAILED email. A single transient disconnect
# should not cost the user their core daily routine.
#
# NON-transient failures are never retried: a genuine upstream ``error`` event
# (reason is the model's own error reason) and HTTP status errors
# (``http_<code>``) are real and re-running them just burns tokens and delays
# the FAILURE notice — a 404 missing-config or a real model error will not
# succeed on a second attempt.
ROUTINE_STREAM_MAX_RETRIES = 2
ROUTINE_STREAM_RETRY_BACKOFF_S = 2.0


def _is_retryable_stream_error(reason: str) -> bool:
    """Whether a ``RoutineStreamError`` reason denotes a transient failure.

    Retryable: transport-layer disconnects (``transport:`` prefix, e.g. a
    mid-SSE ``RemoteProtocolError``), streams that ended without a terminal
    event (``truncated``), and completions that finished cleanly but produced
    no output at all (``empty_output`` — see :meth:`RoutineExecutor.execute`).
    An empty completion is transient in the same way a truncation is: the turn
    is a fresh, self-contained request, so simply re-running it usually yields
    the real brief (observed: homey 2026-08-16, an empty assistant response
    that succeeded on a manual re-run 20 minutes later). Everything else —
    upstream ``error`` events and ``http_<code>`` status errors — is treated as
    terminal and NOT retried.
    """
    return reason.startswith("transport:") or reason in ("truncated", "empty_output")


class RoutineStreamError(RuntimeError):
    """The routine turn's event stream failed before completing.

    Raised by :meth:`RoutineExecutor.execute` when the agent stream emits a
    ``stream_error`` event (transport failure, upstream error event, or a
    stream that ended without a terminal event — see
    :mod:`telos.services.agent_runtime_v2`). Carries whatever partial text
    arrived so the failure notification can include it; the partial output
    must never be presented as the routine's result.
    """

    def __init__(self, reason: str, partial: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.partial = partial


class RoutineExecutor:
    """Run routine messages and dispatch optional email notifications.

    Takes plain callables rather than Protocol wrappers for the registry
    operations it needs. This keeps the service from importing
    ``telos.api.registry.AgentRegistry`` directly (services must not depend
    on the UI layer) while still avoiding the noise of a duck-typed
    abstract base. The caller (the lifespan in :mod:`telos.api.app`) binds
    ``_registry.get_user_email`` and wraps the agent-name lookup to give
    the executor exactly the two operations it uses.
    """

    def __init__(
        self,
        agent_runtime: AgentRuntime,
        email_service: EmailNotificationService,
        get_user_email_fn: Callable[[str], str | None],
        get_agent_name_fn: Callable[[str, str], str],
        base_url: str,
        storage_root: Path | None = None,
        recall_fn: RecallFn | None = None,
    ) -> None:
        self._agent_runtime = agent_runtime
        self._email_service = email_service
        self._get_user_email = get_user_email_fn
        self._get_agent_name = get_agent_name_fn
        self._base_url = base_url
        self._storage_root = storage_root
        self._recall_fn = recall_fn

    async def execute(self, user_id: str, agent_slug: str, message: str) -> str:
        """Execute a routine message against the agent and return the joined text.

        Raises:
            RoutineStreamError: the stream emitted a ``stream_error`` event —
                the turn did not complete and the joined text is only the
                partial output that arrived before the failure; OR the stream
                completed cleanly but yielded no text at all (reason
                ``empty_output``). A stream that ends without a ``stream_error``
                yet produces only empty/whitespace-only text is a silent no-op
                run: :meth:`execute_with_email` would otherwise email an empty
                body and ``RoutineRunner`` would record a "successful" run with
                zero output (homey 2026-08-16). Surfacing it as a
                (retryable — see :func:`_is_retryable_stream_error`)
                ``RoutineStreamError`` routes it through the same bounded-retry +
                FAILURE-notice machinery instead. A scheduled routine must never
                end successfully with no deliverable.
        """
        _current_turn.set(None)
        invocation = current_invocation.get()
        if invocation is not None:
            message += invocation.context()
        if self._storage_root is not None:
            history = history_path(self._storage_root, user_id, agent_slug)
            recipient = self._get_user_email(user_id) if history.exists() else None
            for block in (await delivery_history_block(history, recipient), ledger_summary_block(history)):
                if block:
                    message += "\n\n" + block
        recall = await reserved_recall(self._recall_fn, user_id, agent_slug)
        if recall is not None:
            message += "\n\n" + reserved_recall_block(recall)
        agent = await self._agent_runtime.get(user_id, agent_slug)
        ctx = self._agent_runtime.make_context(user_id, is_routine=True)
        chunks: list[str] = []
        stream_error: dict[str, Any] | None = None
        try:
            async for kind, data in agent.astream(message, ctx):
                if kind == "turn_id" and isinstance(data, dict):
                    _current_turn.set(data.get("turn_id"))
                    if invocation is not None:
                        self._record(user_id, agent_slug, "routine_invocation", invocation.metadata())
                    if recall is not None:
                        self._record(user_id, agent_slug, "required_recall", recall)
                if kind == "text":
                    chunks.append(data)
                elif kind == "stream_error" and stream_error is None:
                    stream_error = data if isinstance(data, dict) else {"reason": str(data)}
            text = "".join(chunks)
            if stream_error is not None:
                raise RoutineStreamError(str(stream_error.get("reason") or "stream_error"), text)
            if not text.strip():
                raise RoutineStreamError("empty_output", text)
        except (Exception, asyncio.CancelledError) as exc:
            # Evidence belongs to this turn/attempt, not the whole routine:
            # retryable failures may still be followed by a successful turn.
            payload = dict(stream_error or {})
            payload["reason"] = (
                exc.reason if isinstance(exc, RoutineStreamError)
                else "cancelled" if isinstance(exc, asyncio.CancelledError)
                else f"exception:{type(exc).__name__}"
            )
            payload["response_chars"] = sum(len(chunk) for chunk in chunks)
            self._record(user_id, agent_slug, "turn_failed", payload)
            raise
        self._record(user_id, agent_slug, "turn_completed", {"response_chars": len(text)})
        return text

    async def _execute_with_retry(self, user_id: str, agent_slug: str, message: str, routine: Any) -> str:
        """Run :meth:`execute`, retrying only *transient* stream failures.

        A transient failure (``transport:*`` / ``truncated`` — see
        :func:`_is_retryable_stream_error`) is retried up to
        ``ROUTINE_STREAM_MAX_RETRIES`` times with a short escalating backoff.
        A non-transient failure (or an exhausted retry budget) re-raises the
        last :class:`RoutineStreamError` so the caller falls through to the
        FAILURE-notice path. Retrying is safe here because a routine turn is a
        fresh, self-contained request whose partial output is discarded.
        """
        attempt = 0
        while True:
            try:
                return await self.execute(user_id, agent_slug, message)
            except RoutineStreamError as exc:
                attempt += 1
                if attempt > ROUTINE_STREAM_MAX_RETRIES or not _is_retryable_stream_error(exc.reason):
                    raise
                backoff = ROUTINE_STREAM_RETRY_BACKOFF_S * attempt
                log.warning(
                    "routine %s (%s/%s) transient stream failure %r — retry %d/%d after %.1fs",
                    routine.id, user_id, agent_slug, exc.reason,
                    attempt, ROUTINE_STREAM_MAX_RETRIES, backoff,
                )
                await asyncio.sleep(backoff)

    async def execute_with_email(self, user_id: str, agent_slug: str, message: str, routine: Any) -> str:
        token = _current_turn.set(None)
        invocation_token = current_invocation.set(current_invocation.get() or RoutineInvocation(local_now()))
        try:
            return await self._execute_and_notify(user_id, agent_slug, message, routine)
        finally:
            current_invocation.reset(invocation_token)
            _current_turn.reset(token)

    async def _execute_and_notify(self, user_id: str, agent_slug: str, message: str, routine: Any) -> str:
        """Run :meth:`execute` then, if the routine opts in, email the response.

        A transient stream failure (transport disconnect / truncation) is
        retried a bounded number of times before giving up — see
        :meth:`_execute_with_retry` — so one flaky mid-SSE disconnect no longer
        costs the user their whole daily brief. Only once retries are exhausted
        (or the failure is non-transient) is the run emailed as an explicit
        FAILURE notice (partial output clearly marked as such) and re-raised, so
        ``RoutineRunner`` logs the run as failed instead of persisting the
        partial text as the routine's result.
        """
        invocation = current_invocation.get()
        assert invocation is not None
        date_str = invocation.started_at.strftime("%d %b %Y")
        try:
            response = await self._execute_with_retry(user_id, agent_slug, message, routine)
        except RoutineStreamError as exc:
            log.warning(
                "routine %s (%s/%s) stream failed: %s — partial %d chars",
                routine.id, user_id, agent_slug, exc.reason, len(exc.partial),
            )
            self._notify_failed(user_id, agent_slug, routine, exc.reason, exc.partial, date_str)
            raise

        if routine.notify_email and (user_email := self._get_user_email(user_id)):
            agent_name = self._get_agent_name(user_id, agent_slug)
            body_with_link = (
                f"{response}\n\n---\n\n"
                f"<a href=\"{self._chat_url(agent_slug)}\">Continue in chat →</a>"
            )
            if self._already_emailed(user_id, agent_slug, user_email):
                return response
            self._send_notification(user_id, agent_slug,
                to=user_email,
                subject=f"{agent_name}: {routine.name} — {date_str}",
                body=body_with_link,
            )

        return response

    def notify_failure(self, user_id: str, agent_slug: str, routine: Any, reason: str, partial: str = "", *,
                       date_str: str, turn_id: str | None = None) -> None:
        """Send the FAILED notice for a run executed elsewhere (Cardea), recorded on ``turn_id`` when given."""
        token = _current_turn.set(turn_id)
        try:
            self._notify_failed(user_id, agent_slug, routine, reason, partial, date_str)
        finally:
            _current_turn.reset(token)

    def _notify_failed(self, user_id, agent_slug, routine, reason, partial, date_str):
        if not routine.notify_email or not (user_email := self._get_user_email(user_id)):
            return
        agent_name = self._get_agent_name(user_id, agent_slug)
        partial_block = (
            f"<hr><p><em>Partial output before the failure:</em></p>\n{partial}"
            if partial.strip()
            else ""
        )
        body = (
            f"<p><strong>This run failed before completing</strong> "
            f"(reason: {reason}). No result was produced.</p>"
            f"{partial_block}\n\n---\n\n"
            f"<a href=\"{self._chat_url(agent_slug)}\">Continue in chat →</a>"
        )
        self._send_notification(user_id, agent_slug,
            to=user_email,
            subject=f"{agent_name}: {routine.name} FAILED — {date_str}",
            body=body,
        )

    def _events(self, user_id, slug):
        if self._storage_root is None or not _current_turn.get():
            return None
        return ConversationLog(self._storage_root / user_id / slug / "history.db").execution_events

    def _record(self, user_id, slug, kind, payload):
        try:
            events = self._events(user_id, slug)
            if events is not None:
                events.append(_current_turn.get(), kind, payload)
        except Exception:
            log.exception("Could not persist routine execution evidence")

    def _already_emailed(self, user_id, slug, recipient):
        events = self._events(user_id, slug)
        if events is None:
            return False
        calls = set()
        for offset in range(0, 1000, 20):
            page = events.list(_current_turn.get(), offset=offset, limit=20)
            for event in page:
                payload = event["payload"]
                if event["kind"] == "tool_call" and payload.get("name") == "send_email":
                    if payload.get("input", {}).get("to", "").casefold() == recipient.casefold():
                        calls.add(payload.get("call_id"))
                if (event["kind"] == "tool_result" and payload.get("call_id") in calls
                        and payload.get("is_error") is False):
                    return True
            if len(page) < 20:
                break
        return False

    def _send_notification(self, user_id, slug, *, to, subject, body):
        outcome = self._email_service.send_routine_notification(to=to, subject=subject, body=body)
        status = "accepted_by_smtp" if outcome is True else "failed_or_unconfigured" if outcome is False else "unknown"
        self._record(user_id, slug, "email_delivery", {
            "to": to, "subject": subject, "body": body, "status": status,
            "notification_completed_at": local_now().isoformat(),
        })

    def _chat_url(self, agent_slug: str) -> str:
        """Absolute (or root-relative when no base URL is configured) link to
        the agent's chat page."""
        base = self._base_url.rstrip("/") if self._base_url else ""
        return f"{base}/agents/{agent_slug}" if base else f"/agents/{agent_slug}"


__all__ = ["RoutineExecutor", "RoutineStreamError"]
