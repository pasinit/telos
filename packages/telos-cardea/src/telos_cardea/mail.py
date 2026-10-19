"""``mail.send`` (effect) and its Brevo reconciler ``mail.send.reconcile``.

Sending mirrors ``telos.mailer.send_email`` plus the markdown-to-HTML alternative
part from ``telos.services.email_notifications``. The RFC 5322 ``Message-ID`` is
derived from Cardea's ``effect_key`` so a retry after "not applied" reuses the
same identity, and the reconciler can look the message up at Brevo.
"""

import asyncio
import re
import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import httpx
import mistune

from telos_cardea._cardea import Arguments, OperationContext, OperationRejected, Output, Reconciliation
from telos_cardea.config import Config, SmtpConfig, load_config

# Brevo may take a moment to index a just-accepted message; until then an empty
# events list does not prove the message was never sent.
RECONCILE_GRACE = timedelta(minutes=10)
# Every Brevo event type implies the relay accepted the message (ADR-026 "applied").
BREVO_EVENTS = frozenset({
    "requests", "delivered", "opened", "clicks", "loadedByProxy", "deferred", "softBounces", "hardBounces",
    "bounces", "spam", "invalid", "blocked", "unsubscribed", "error",
})

_ADDRESS_RE = re.compile(r"^[^@\s<>\"(),;:]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_HTML_BLOCK_RE = re.compile(r"<(?:p|h[1-6]|ul|ol|div|table)\b", re.IGNORECASE)
_markdown = mistune.create_markdown(plugins=["table", "strikethrough"])


def valid_address(address: str) -> bool:
    name, parsed = parseaddr(address)
    return not name and parsed == address.strip() and bool(_ADDRESS_RE.fullmatch(parsed))


def message_id(effect_key: str, sender: str) -> str:
    """``<effect_key@sender-domain>``: stable per effect, unique per sending domain."""
    domain = parseaddr(sender)[1].rsplit("@", 1)[-1].strip().lower()
    if not domain:
        raise ValueError("sender address has no domain")
    return f"<{effect_key}@{domain}>"


def md_to_html(text: str) -> str:
    """Same rendering Telos uses: HTML bodies pass through, markdown is rendered."""
    if _HTML_BLOCK_RE.search(text):
        return text
    return str(_markdown(text))


def build_message(smtp: SmtpConfig, to: str, subject: str, body: str, msg_id: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp.sender
    msg["To"] = to
    msg["Message-ID"] = msg_id
    msg.set_content(body)
    msg.add_alternative(md_to_html(body), subtype="html")
    return msg


def _deliver(smtp: SmtpConfig, msg: EmailMessage) -> None:
    """Send, raising ``OperationRejected`` only where the relay certainly kept nothing.

    Everything before DATA (connect, TLS, auth, MAIL FROM, RCPT TO) cannot have
    produced an email, and neither can an explicit 5xx reply to the message body.
    A dropped connection or timeout during/after DATA stays uncertain and
    propagates, so Cardea marks the effect ``outcome_unknown``.
    """
    try:
        client = smtplib.SMTP(smtp.host, smtp.port, timeout=30)
    except OSError as error:
        raise OperationRejected(f"SMTP connect failed: {type(error).__name__}") from error
    with client:
        try:
            client.starttls()
            if smtp.user or smtp.password:
                client.login(smtp.user, smtp.password)
        except (smtplib.SMTPException, OSError) as error:
            raise OperationRejected(f"SMTP session refused before DATA: {_describe(error)}") from error
        try:
            client.send_message(msg)
        except smtplib.SMTPRecipientsRefused as error:
            raise OperationRejected(f"recipient refused: {_describe(error)}") from error
        except smtplib.SMTPSenderRefused as error:
            raise OperationRejected(f"sender refused: {_describe(error)}") from error
        except smtplib.SMTPDataError as error:
            if error.smtp_code >= 500:
                raise OperationRejected(f"message refused: {error.smtp_code}") from error
            raise


def _describe(error: BaseException) -> str:
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = sorted({code for code, _ in error.recipients.values()})
        return "codes " + ",".join(str(c) for c in codes)
    code = getattr(error, "smtp_code", None)
    return f"{type(error).__name__} {code}" if code else type(error).__name__


async def send(arguments: Arguments, context: OperationContext, *, config: Config | None = None) -> Output:
    """Entry point ``mail.send``."""
    config = config or load_config()
    smtp = config.smtp
    to = str(arguments["to"]).strip()
    subject = str(arguments["subject"])
    body = str(arguments["body"])
    if not valid_address(to):
        raise OperationRejected("invalid recipient address")
    if not subject.strip() or not body.strip():
        raise OperationRejected("subject and body are required")
    if not smtp.host or not smtp.sender:
        raise OperationRejected("SMTP is not configured")
    try:
        msg_id = message_id(context.effect_key, smtp.sender)
    except ValueError as error:
        raise OperationRejected("SMTP sender address is invalid") from error
    await asyncio.to_thread(_deliver, smtp, build_message(smtp, to, subject, body, msg_id))
    return {"message_id": msg_id, "to": to, "status": "sent"}


async def preview(arguments: Arguments, context: OperationContext) -> Output:
    """Entry point ``mail.preview`` (read): validates like ``mail.send`` but never sends.

    Telos's shadow mode exposes this instead of ``mail.send`` so a routine's
    outputs can be compared with real runs without emailing anyone. It needs
    no SMTP configuration.
    """
    to = str(arguments["to"]).strip()
    subject = str(arguments["subject"])
    if not valid_address(to):
        raise OperationRejected("invalid recipient address")
    if not subject.strip() or not str(arguments["body"]).strip():
        raise OperationRejected("subject and body are required")
    return {"preview": True, "to": to, "subject": subject, "message_id": f"<{context.effect_key}@preview>"}


async def reconcile(arguments: Arguments, context: OperationContext, *, config: Config | None = None,
                    client: httpx.AsyncClient | None = None, now: datetime | None = None) -> Reconciliation:
    """Entry point ``mail.send.reconcile``: ask Brevo whether the Message-ID was sent.

    - any event for the Message-ID: applied (the relay accepted the message);
    - no events once the grace period after the deadline has passed: not applied;
    - an API error, or no events yet within the grace period: raise, leaving the
      effect uncertain so Cardea's loop asks again later.
    """
    config = config or load_config()
    if not config.brevo_api_key:
        raise RuntimeError("BREVO_API_KEY is not configured")
    to = str(arguments["to"]).strip()
    msg_id = message_id(context.effect_key, config.smtp.sender)
    events = await _brevo_events(config, msg_id, client)
    if events:
        latest = events[0].get("event")
        status = str(latest) if latest in BREVO_EVENTS else "accepted"
        return Reconciliation(applied=True, output={"message_id": msg_id, "to": to, "status": status})
    if (now or datetime.now(UTC)) < context.deadline + RECONCILE_GRACE:
        raise RuntimeError("Brevo has no events yet; too early to conclude the message was not sent")
    return Reconciliation(applied=False)


async def _brevo_events(config: Config, msg_id: str, client: httpx.AsyncClient | None) -> list[dict[str, Any]]:
    # https://developers.brevo.com/reference/get-email-event-report
    params: dict[str, str | int] = {"messageId": msg_id, "days": 90, "limit": 100, "sort": "desc"}
    headers = {"api-key": config.brevo_api_key, "accept": "application/json"}
    url = config.brevo_api_url.rstrip("/") + "/smtp/statistics/events"
    if client is None:
        async with httpx.AsyncClient(timeout=20) as own:
            response = await own.get(url, params=params, headers=headers)
    else:
        response = await client.get(url, params=params, headers=headers)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Brevo returned an unrecognised events response")
    events = data.get("events", [])
    if not isinstance(events, list):
        raise RuntimeError("Brevo returned an unrecognised events response")
    return [e for e in events if isinstance(e, dict) and e.get("messageId") in (msg_id, msg_id.strip("<>"))]
