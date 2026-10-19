"""Task-local reporting clock shared by a routine and all its retries."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RoutineInvocation:
    started_at: datetime

    def metadata(self) -> dict[str, str]:
        return {
            "reporting_date": self.started_at.date().isoformat(),
            "timezone": str(self.started_at.tzinfo),
            "invoked_at": self.started_at.isoformat(),
        }

    def context(self) -> str:
        data = self.metadata()
        return (
            "\n\nRoutine invocation context:\n"
            f"Reporting date: {data['reporting_date']}\n"
            f"Timezone: {data['timezone']}\n"
            f"Invocation timestamp: {data['invoked_at']}\n"
            "Use this reporting date for report headings and as the reference date "
            "for relative dates and deadline arithmetic. Keep it unchanged across retries; "
            "the execution or delivery date may differ."
        )


current_invocation: ContextVar[RoutineInvocation | None] = ContextVar("routine_invocation", default=None)
