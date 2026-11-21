"""Owner notice email (11 "Owner notice"): the SMTP adapter, message text and settings rules."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr, ValidationError

from app.adapters.email import MailError, OutgoingEmail
from app.adapters.email import smtp as smtp_module
from app.adapters.email.smtp import SmtpMailer
from app.modules.notices import messages
from tests.conftest import make_settings


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL; records what the mailer does."""

    instances: ClassVar[list[FakeSMTP]] = []
    refuse: ClassVar[dict[str, int]] = {}
    connect_error: Exception | None = None
    send_error: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float, context: Any = None) -> None:
        if FakeSMTP.connect_error is not None:
            raise FakeSMTP.connect_error
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def starttls(self, context: Any) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login {user}")

    def send_message(self, message: EmailMessage) -> None:
        if FakeSMTP.send_error is not None:
            raise FakeSMTP.send_error
        to = str(message["To"])
        if to in FakeSMTP.refuse:
            raise smtplib.SMTPRecipientsRefused({to: (FakeSMTP.refuse[to], b"no")})
        self.sent.append(message)

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances, FakeSMTP.refuse = [], {}
    FakeSMTP.connect_error = FakeSMTP.send_error = None
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtp_module.smtplib, "SMTP_SSL", FakeSMTP)


def mail(to: str, n: int = 0) -> OutgoingEmail:
    return OutgoingEmail(to, "[op] Subject", "Body\n", f"notice-x-{n}@example.test")


async def test_starttls_login_headers_and_refused_recipients() -> None:
    mailer = SmtpMailer("relay", 587, "op <n@example.test>", username="u", password="p")
    FakeSMTP.refuse = {"bad@example.test": 550}
    result = await mailer.send([mail("a@example.test"), mail("bad@example.test", 1)])
    assert (result.delivered, result.refused) == (1, 1)
    client = FakeSMTP.instances[0]
    assert client.calls == ["starttls", "login u", "quit"]
    message = client.sent[0]
    assert message["Message-ID"] == "<notice-x-0@example.test>"
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["From"] == "op <n@example.test>"


async def test_plain_relay_and_failure_classes() -> None:
    plain = SmtpMailer("mailpit", 1025, "n@example.test", tls="none")
    await plain.send([mail("a@example.test")])
    assert FakeSMTP.instances[-1].calls == ["quit"]  # no TLS, no login
    assert (await plain.send([])).delivered == 0

    FakeSMTP.connect_error = ConnectionRefusedError()
    with pytest.raises(MailError) as down:
        await plain.send([mail("a@example.test")])
    assert (down.value.code, down.value.transient) == ("smtp_unavailable", True)
    FakeSMTP.connect_error = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(MailError) as auth:
        await plain.send([mail("a@example.test")])
    assert (auth.value.code, auth.value.transient) == ("smtp_auth_failed", False)
    FakeSMTP.connect_error = None
    FakeSMTP.refuse = {"a@example.test": 451}  # greylisted: try the whole notice again later
    with pytest.raises(MailError) as deferred:
        await plain.send([mail("a@example.test")])
    assert deferred.value.transient
    FakeSMTP.refuse = {}
    FakeSMTP.send_error = smtplib.SMTPDataError(554, b"policy")
    with pytest.raises(MailError) as rejected:
        await plain.send([mail("a@example.test")])
    assert (rejected.value.code, rejected.value.transient) == ("smtp_rejected", False)


async def test_header_injection_is_refused() -> None:
    mailer = SmtpMailer("relay", 587, "n@example.test")
    with pytest.raises(ValueError):
        await mailer.send([OutgoingEmail("a@example.test", "x\r\nBcc: evil@example.test", "b", "id@x")])


def test_message_text_keeps_user_names_out_of_headers() -> None:
    ctx = messages.Context(
        origin="https://app.example.test",
        workspace_id="w1",
        workspace_name="Team\r\nBcc: x@example.test",
        projects=[("p1", "core\nlib")],
        support="help@example.test",
    )
    body = messages.body("site_suspended", {"reason": "operator"}, ctx)
    assert "\r" not in body and "Team  Bcc" not in body
    assert "- core lib: https://app.example.test/p/p1" in body
    assert "took it down" in body and "Support: help@example.test" in body
    subject = messages.subject("billing_past_due", "op\nname")
    assert "\n" not in subject and subject.startswith("[op name] ")
    for kind in messages.SUBJECTS:
        assert "https://app.example.test" in messages.body(kind, {}, ctx) or kind == "dispute_lost"


def test_smtp_settings_rules() -> None:
    assert make_settings(smtp_host="").smtp_host is None  # blank means unset
    ok = make_settings(smtp_host="relay", smtp_from="op <n@example.test>")
    assert (ok.smtp_port, ok.smtp_tls) == (587, "starttls")
    with pytest.raises(ValidationError):
        make_settings(smtp_host="relay")  # needs a sender
    with pytest.raises(ValidationError):
        make_settings(smtp_host="relay", smtp_from="n@example.test", smtp_username="u")
    with pytest.raises(ValidationError):
        make_settings(
            smtp_host="relay",
            smtp_from="n@example.test",
            smtp_tls="none",
            smtp_username="u",
            smtp_password=SecretStr("p"),
        )
