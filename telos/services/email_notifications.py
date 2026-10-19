"""Email notification service — thin wrapper around ``telos.mailer.send_email``
that renders markdown bodies into HTML for the alternate part.
"""
import logging
import re

import mistune

from telos.mailer import EmailConfig, send_email

log = logging.getLogger("telos.api")

_md = mistune.create_markdown(plugins=["table", "strikethrough"])
_HTML_BLOCK_RE = re.compile(r"<(?:p|h[1-6]|ul|ol|div|table)\b", re.IGNORECASE)


def _md_to_html(text: str) -> str:
    # Agents that emit HTML directly (e.g. Jobber) must pass through unmangled.
    # Pre-7K behaviour vendored from the removed telos/ui/markdown.py.
    if _HTML_BLOCK_RE.search(text):
        return text
    return _md(text)


class EmailNotificationService:
    """Send routine notifications and agent-triggered emails.

    Constructed with an :class:`~telos.mailer.EmailConfig` — the caller
    (the lifespan in :mod:`telos.api.app`) builds it from the ``Settings``
    SMTP fields and passes it in.

    Two methods, two error policies:

    - :meth:`send_routine_notification` swallows exceptions because routine
      notifications are background work; failing once shouldn't crash the
      scheduler.
    - :meth:`send_agent_email` propagates exceptions so the tool caller (the
      ``send_email`` tool) can report the failure back to the agent.
    """

    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    def send_routine_notification(self, to: str, subject: str, body: str) -> bool:
        """Send a background routine notification, logging failures instead of surfacing them."""
        if not self._config.host or not to:
            return False
        try:
            send_email(self._config, to=to, subject=subject, body=body, html_body=_md_to_html(body))
            log.info("email sent to %s: %s", to, subject)
            return True
        except Exception:
            log.exception("failed to send email notification")
            return False

    def send_agent_email(self, to: str, subject: str, body: str) -> None:
        """Send an agent-requested email and report failures back to the tool caller."""
        send_email(self._config, to=to, subject=subject, body=body, html_body=_md_to_html(body))


__all__ = ["EmailNotificationService"]
