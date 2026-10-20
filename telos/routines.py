"""Scheduled routines for Telos agents.

Storage is in :mod:`telos.core.repos.routines`; this module keeps the
``RoutineStore`` façade and the APScheduler-backed ``RoutineRunner``.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from telos.core.persistence import Database
from telos.core.repos.routines import Routine, RoutineRepo, RoutineResult
from telos.local_time import local_now, local_timezone
from telos.routine_invocation import RoutineInvocation, current_invocation

__all__ = [
    "Routine",
    "RoutineResult",
    "RoutineStore",
    "RoutineRunner",
    "_format_interval",
    "scope_note",
]

log = logging.getLogger("telos.routines")


class RoutineStore:
    """Façade over :class:`RoutineRepo` — preserves the legacy public API."""

    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        self.repo = RoutineRepo(self._db)

    def create(self, user_id: str, agent_slug: str, description: str,
               name: str = "", interval_seconds: int = 3600,
               time_of_day: str | None = None,
               one_shot: bool = False, notify_email: bool = False,
               enabled: bool = True) -> Routine:
        return self.repo.create(
            user_id, agent_slug, description, name=name,
            interval_seconds=interval_seconds, time_of_day=time_of_day,
            one_shot=one_shot, notify_email=notify_email, enabled=enabled,
        )

    def list_for_agent(self, user_id: str, agent_slug: str) -> list[Routine]:
        return self.repo.list_for_agent(user_id, agent_slug)

    def list_all_enabled(self) -> list[Routine]:
        return self.repo.list_all_enabled()

    def get(self, routine_id: str) -> Routine | None:
        return self.repo.get(routine_id)

    def delete(self, routine_id: str) -> None:
        self.repo.delete(routine_id)

    def toggle(self, routine_id: str, enabled: bool) -> None:
        self.repo.toggle(routine_id, enabled)

    def set_notify_email(self, routine_id: str, notify: bool) -> None:
        self.repo.set_notify_email(routine_id, notify)

    def update(self, routine_id: str, **kwargs) -> None:
        self.repo.update(routine_id, **kwargs)

    def mark_run(self, routine_id: str) -> None:
        self.repo.mark_run(routine_id)

    def save_result(self, routine_id: str, response: str) -> None:
        self.repo.save_result(routine_id, response)

    def recent_results(self, user_id: str, agent_slug: str, limit: int = 10) -> list[RoutineResult]:
        return self.repo.recent_results(user_id, agent_slug, limit)


def _format_interval(seconds: int) -> str:
    """Human-readable interval string."""
    if seconds < 60:
        return f"every {seconds}s"
    if seconds < 3600:
        m = seconds // 60
        return f"every {m} minute{'s' if m != 1 else ''}"
    if seconds < 86400:
        h = seconds // 3600
        return f"every {h} hour{'s' if h != 1 else ''}"
    d = seconds // 86400
    return f"every {d} day{'s' if d != 1 else ''}"


def scope_note(routine: Routine, routines: list[Routine]) -> str:
    """Fence one routine's run off from the agent's other enabled routines in ``routines``.

    Routine turns see the agent's history and prior email deliveries,
    which include the sibling routines' runs. Without a boundary the
    agent treats those as part of its job and performs them too — homey
    2026-10-14: both daily searches ran, and both emails went out, on
    each of the two routine runs. Returns "" when there are no siblings.
    """
    siblings = [
        r for r in routines
        if r.id != routine.id and r.enabled and r.user_id == routine.user_id and r.agent_slug == routine.agent_slug
    ]
    if not siblings:
        return ""
    names = "\n".join(f"- {r.name or r.description.splitlines()[0][:120]}" for r in siblings)
    return (
        "\n\nScope of this run: perform ONLY the routine described above and send only its own email. "
        "The following routines run separately on their own schedule; do not perform their searches "
        "or send their emails in this run, even though their past runs appear in your history:\n" + names
    )


# Type for the callback that executes a routine against an agent
# (user_id, agent_slug, message, routine) -> response
ExecuteCallback = Callable[[str, str, str, "Routine"], Awaitable[str]]


class RoutineRunner:
    """Manages scheduled routine execution using APScheduler."""

    def __init__(
        self,
        store: RoutineStore,
        execute_fn: ExecuteCallback,
        owned_elsewhere: Callable[[Routine], bool] | None = None,
    ) -> None:
        self._store = store
        self._execute_fn = execute_fn
        # True when another executor (Cardea, in live mode) runs this routine's slots.
        self._owned_elsewhere = owned_elsewhere
        self._scheduler = AsyncIOScheduler(timezone=local_timezone())
        self._jobs: dict[str, Any] = {}  # routine_id -> job
        self._in_flight: set[str] = set()  # routine ids currently executing

    def start(self) -> None:
        """Start the scheduler and restore all enabled routines."""
        self._scheduler.start()
        routines = self._store.list_all_enabled()
        for routine in routines:
            self._register(routine)
        # Check for missed routines that should have run during downtime
        now = datetime.now(UTC)
        for routine in routines:
            if routine.one_shot or not routine.last_run:
                continue
            last = datetime.fromisoformat(routine.last_run)
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            missed_by = (now - last).total_seconds() - routine.interval_seconds
            if missed_by > 0:
                log.info("routine %s missed by %.0fs — executing now", routine.id, missed_by)
                asyncio.ensure_future(self._run_routine(routine))
        log.info("routine runner started, restored %d routines", len(routines))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        self._jobs.clear()

    def add(self, routine: Routine) -> None:
        """Register a new routine with the scheduler."""
        self._register(routine)

    def get_next_run(self, routine_id: str) -> str | None:
        """Get the next scheduled run time for a routine."""
        job = self._jobs.get(routine_id)
        if job and job.next_run_time:
            return job.next_run_time.astimezone(local_timezone()).strftime("%Y-%m-%d %H:%M")
        return None

    def remove(self, routine_id: str) -> None:
        """Remove a routine from the scheduler."""
        if routine_id in self._jobs:
            try:
                self._jobs[routine_id].remove()
            except Exception:
                pass  # job may already be removed (e.g. one-shot after firing)
            del self._jobs[routine_id]

    def _register(self, routine: Routine) -> None:
        if routine.id in self._jobs:
            return

        if routine.one_shot:
            run_at = local_now() + timedelta(seconds=routine.interval_seconds)
            log.info("one-shot routine %s scheduled to run at %s (in %ds)", routine.id, run_at, routine.interval_seconds)
            job = self._scheduler.add_job(
                self._run_routine, "date",
                run_date=run_at,
                args=[routine],
                id=routine.id,
                replace_existing=True,
                misfire_grace_time=None,
            )
        elif routine.time_of_day and routine.interval_seconds >= 86400:
            parts = routine.time_of_day.split(":")
            hour, minute = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            job = self._scheduler.add_job(
                self._run_routine, "cron",
                hour=hour, minute=minute,
                args=[routine],
                id=routine.id,
                replace_existing=True,
                misfire_grace_time=None,
            )
        else:
            job = self._scheduler.add_job(
                self._run_routine, "interval",
                seconds=routine.interval_seconds,
                args=[routine],
                id=routine.id,
                replace_existing=True,
                misfire_grace_time=None,
            )

        self._jobs[routine.id] = job
        label = _format_interval(routine.interval_seconds)
        log.info("registered routine %s: %r %s%s", routine.id, routine.description,
                 label, " (one-shot)" if routine.one_shot else "")

    def _scope_note(self, routine: Routine) -> str:
        """Fence this run off from the agent's other routines (see :func:`scope_note`)."""
        try:
            siblings = self._store.list_for_agent(routine.user_id, routine.agent_slug)
        except Exception:
            log.exception("sibling routine lookup failed for %s", routine.id)
            siblings = []
        return scope_note(routine, siblings)

    async def _run_routine(self, routine: Routine) -> None:
        """Execute a routine against the agent."""
        # The job args carry a snapshot of the routine row; re-read it so
        # (a) a row deleted behind the scheduler's back (e.g. while the
        # MCP delete path lacked a live runner) skips and unschedules
        # instead of firing forever, and (b) edits to the description /
        # notify flags since registration take effect.
        current = self._store.get(routine.id)
        if current is None:
            log.warning(
                "routine %s no longer exists in the store — unscheduling stale job",
                routine.id,
            )
            self.remove(routine.id)
            return
        routine = current
        if self._owned_elsewhere is not None and self._owned_elsewhere(routine):
            log.info("routine %s is executed by cardea — skipping local run", routine.id)
            # Advance the run clock anyway: a stale ``last_run`` would make the
            # ``start()`` catch-up re-run this slot locally after a rollback.
            self._store.mark_run(routine.id)
            return

        # Single-flight: a routine already executing (a slow run overlapping
        # its own interval tick, or the ``start()`` catch-up path racing the
        # regular job) must not be launched again. Without this a crashed or
        # long turn would fan out into redundant, contradictory same-period
        # runs with no de-duplication guard.
        if routine.id in self._in_flight:
            log.info("routine %s already in flight — skipping duplicate launch", routine.id)
            return
        self._in_flight.add(routine.id)

        log.info("executing routine %s: %r", routine.id, routine.description)
        invocation_token = current_invocation.set(RoutineInvocation(local_now()))
        try:
            message = routine.description + self._scope_note(routine)
            response = await self._execute_fn(routine.user_id, routine.agent_slug, message, routine)
            self._store.save_result(routine.id, response)
            log.info("routine %s completed, response=%d chars", routine.id, len(response))
        except Exception:
            log.exception("routine %s failed", routine.id)
        finally:
            current_invocation.reset(invocation_token)
            # Advance the run clock for *every* attempt, success or failure.
            # If a failed run left ``last_run`` stale, ``start()`` would treat
            # the routine as perpetually missed and re-fire it on every
            # scheduler start (on top of the normal interval job).
            self._store.mark_run(routine.id)
            self._in_flight.discard(routine.id)

        if routine.one_shot:
            self.remove(routine.id)
            self._store.delete(routine.id)
            log.info("one-shot routine %s auto-deleted", routine.id)
