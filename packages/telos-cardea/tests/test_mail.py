import smtplib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from telos_cardea import mail
from telos_cardea._cardea import OperationContext, OperationRejected
from telos_cardea.config import Config

from conftest import EFFECT_KEY, assert_input, assert_output

ARGS = {"to": "owner@example.com", "subject": "3 new flats", "body": "# Today\n\n- **Flat 1** £2,000 pcm"}
MESSAGE_ID = f"<{EFFECT_KEY}@example.org>"
MakeContext = Callable[..., OperationContext]


class FakeSMTP:
    """Records the SMTP conversation; ``fail`` maps a stage to the exception it raises."""

    instances: list["FakeSMTP"] = []
    fail: dict[str, BaseException] = {}

    def __init__(self, host: str, port: int, timeout: float) -> None:
        if "connect" in self.fail:
            raise self.fail["connect"]
        self.address = (host, port)
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def _stage(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def starttls(self) -> None:
        self._stage("starttls")

    def login(self, user: str, password: str) -> None:
        self._stage("login")

    def send_message(self, msg: EmailMessage) -> None:
        self._stage("send_message")
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    FakeSMTP.fail = {}
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_arguments_match_schema() -> None:
    assert_input("mail.send", ARGS)


async def test_send_uses_effect_key_as_message_id(config: Config, make_context: MakeContext) -> None:
    output = await mail.send(dict(ARGS), make_context(), config=config)
    assert output == {"message_id": MESSAGE_ID, "to": "owner@example.com", "status": "sent"}
    assert_output("mail.send", output)
    smtp = FakeSMTP.instances[0]
    assert smtp.address == ("smtp-relay.brevo.com", 587)
    assert smtp.calls == ["starttls", "login", "send_message", "quit"]
    msg = smtp.sent[0]
    assert msg["Message-ID"] == MESSAGE_ID
    assert msg["To"] == "owner@example.com"
    assert msg["From"] == "Telos <telos@example.org>"
    html = msg.get_body(("html",))
    assert html is not None and "<strong>Flat 1</strong>" in html.get_content()


async def test_retry_with_same_effect_key_reuses_message_id(config: Config, make_context: MakeContext) -> None:
    first = await mail.send(dict(ARGS), make_context(), config=config)
    second = await mail.send({**ARGS, "subject": "reworded"}, make_context(), config=config)
    assert first["message_id"] == second["message_id"] == MESSAGE_ID


@pytest.mark.parametrize("to", ["not-an-address", "a@b", "Eve <eve@example.com>", "x@example.com, y@example.com"])
async def test_invalid_recipient_is_rejected_without_connecting(config: Config, make_context: MakeContext, to: str) -> None:
    with pytest.raises(OperationRejected, match="invalid recipient"):
        await mail.send({**ARGS, "to": to}, make_context(), config=config)
    assert FakeSMTP.instances == []


async def test_unconfigured_smtp_is_rejected(make_context: MakeContext) -> None:
    with pytest.raises(OperationRejected, match="not configured"):
        await mail.send(dict(ARGS), make_context(), config=Config())


REJECTED_BEFORE_DATA: list[tuple[str, BaseException]] = [
    ("connect", ConnectionRefusedError()),
    ("starttls", smtplib.SMTPNotSupportedError("STARTTLS extension not supported")),
    ("login", smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")),
    ("send_message", smtplib.SMTPSenderRefused(550, b"5.7.1 sender rejected", "telos@example.org")),
    ("send_message", smtplib.SMTPRecipientsRefused({"owner@example.com": (550, b"5.1.1 user unknown")})),
    ("send_message", smtplib.SMTPDataError(554, b"5.7.1 message rejected")),
]


@pytest.mark.parametrize(("stage", "error"), REJECTED_BEFORE_DATA, ids=lambda v: v if isinstance(v, str) else type(v).__name__)
async def test_certain_refusals_map_to_operation_rejected(config: Config, make_context: MakeContext, stage: str,
                                                          error: BaseException) -> None:
    FakeSMTP.fail = {stage: error}
    with pytest.raises(OperationRejected):
        await mail.send(dict(ARGS), make_context(), config=config)


@pytest.mark.parametrize("error", [smtplib.SMTPServerDisconnected("lost"), TimeoutError(), smtplib.SMTPDataError(451, b"try later")],
                         ids=lambda e: type(e).__name__)
async def test_uncertain_failures_propagate_as_unknown(config: Config, make_context: MakeContext, error: BaseException) -> None:
    FakeSMTP.fail = {"send_message": error}
    with pytest.raises(type(error)) as raised:
        await mail.send(dict(ARGS), make_context(), config=config)
    assert not isinstance(raised.value, OperationRejected)


async def test_preview_never_connects(make_context: MakeContext) -> None:
    output = await mail.preview(dict(ARGS), make_context(effect_key="r1"))
    assert output == {"preview": True, "to": "owner@example.com", "subject": "3 new flats", "message_id": "<r1@preview>"}
    assert_output("mail.preview", output)
    assert FakeSMTP.instances == []
    with pytest.raises(OperationRejected):
        await mail.preview({**ARGS, "to": "nobody"}, make_context())


# ── reconciler ──────────────────────────────────────────────────────────


def brevo(response: httpx.Response, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def event(name: str, message_id: str = MESSAGE_ID) -> dict[str, Any]:
    return {"email": "owner@example.com", "date": "2026-10-15T07:00:01Z", "messageId": message_id, "event": name}


async def test_reconcile_sent(config: Config, make_context: MakeContext) -> None:
    seen: list[httpx.Request] = []
    client = brevo(httpx.Response(200, json={"events": [event("delivered"), event("requests")]}), seen)
    result = await mail.reconcile(dict(ARGS), make_context(), config=config, client=client)
    assert result.applied is True
    assert result.output == {"message_id": MESSAGE_ID, "to": "owner@example.com", "status": "delivered"}
    assert_output("mail.send", result.output)
    request = seen[0]
    assert request.url.path == "/v3/smtp/statistics/events"
    assert request.url.params["messageId"] == MESSAGE_ID
    assert request.headers["api-key"] == "brevo-key"


async def test_reconcile_accepts_message_id_without_brackets(config: Config, make_context: MakeContext) -> None:
    client = brevo(httpx.Response(200, json={"events": [event("requests", MESSAGE_ID.strip("<>"))]}), [])
    assert (await mail.reconcile(dict(ARGS), make_context(), config=config, client=client)).applied is True


async def test_reconcile_not_found_after_grace_is_not_applied(config: Config, make_context: MakeContext) -> None:
    deadline = datetime.now(UTC) - mail.RECONCILE_GRACE - timedelta(seconds=1)
    bodies: list[dict[str, Any]] = [{"events": []}, {}]
    for body in bodies:
        client = brevo(httpx.Response(200, json=body), [])
        result = await mail.reconcile(dict(ARGS), make_context(deadline=deadline), config=config, client=client)
        assert result.applied is False and result.output is None


async def test_reconcile_not_found_within_grace_stays_uncertain(config: Config, make_context: MakeContext) -> None:
    client = brevo(httpx.Response(200, json={"events": []}), [])
    with pytest.raises(RuntimeError, match="too early"):
        await mail.reconcile(dict(ARGS), make_context(), config=config, client=client)


async def test_reconcile_ignores_events_for_other_messages(config: Config, make_context: MakeContext) -> None:
    deadline = datetime.now(UTC) - timedelta(days=1)
    client = brevo(httpx.Response(200, json={"events": [event("delivered", "<other@example.org>")]}), [])
    assert (await mail.reconcile(dict(ARGS), make_context(deadline=deadline), config=config, client=client)).applied is False


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="oops"),
    httpx.Response(401, json={"code": "unauthorized", "message": "Key not found"}),
    httpx.Response(400, json={"code": "invalid_parameter", "message": "bad"}),
    httpx.Response(200, json={"events": "nope"}),
], ids=["500", "401", "400", "malformed"])
async def test_reconcile_error_leaves_effect_uncertain(config: Config, make_context: MakeContext, response: httpx.Response) -> None:
    deadline = datetime.now(UTC) - timedelta(days=1)
    with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
        await mail.reconcile(dict(ARGS), make_context(deadline=deadline), config=config, client=brevo(response, []))


async def test_reconcile_without_api_key_raises(make_context: MakeContext, config: Config) -> None:
    with pytest.raises(RuntimeError):
        await mail.reconcile(dict(ARGS), make_context(), config=Config(smtp=config.smtp))
