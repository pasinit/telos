"""``send_email`` tool — separate from the routine CRUD tools.

The tool calls the deployment's send-email callback in a worker thread.
``None`` is accepted so deployments without SMTP can still register the
tool — it returns "not configured" at call time instead of vanishing
from the schema.
"""

import asyncio
import logging
from collections.abc import Callable

from pydantic import BaseModel, Field

from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for

log = logging.getLogger(__name__)


class SendEmailInput(BaseModel):
    to: str = Field(description="Recipient email address.")
    subject: str = Field(description="Subject line.")
    body: str = Field(description="Plain-text body.")


_EMAIL_NOT_CONFIGURED = ToolResult(
    text="Email is not configured on this Telos instance.",
    is_error=True,
)


class SendEmailTool:
    """``send_email`` — fire the deployment's email callback in a worker thread."""

    name = "send_email"
    description = "Send an email via the deployment's SMTP relay."

    def __init__(self, send_email_fn: Callable[[str, str, str], None] | None) -> None:
        self._send = send_email_fn

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: SendEmailInput, ctx: ToolContext) -> ToolResult:
        if self._send is None:
            return _EMAIL_NOT_CONFIGURED
        # ``send_email_fn`` is a synchronous callback (it usually wraps
        # ``smtplib``); offload so the chat loop stays responsive.
        await asyncio.to_thread(self._send, inp.to, inp.subject, inp.body)
        return ToolResult(text=f"Email sent to {inp.to}.")
