"""Outgoing email (owner notices, 11 "Owner notice"; 22).

`Mailer` sends plain-text messages; `SmtpMailer` talks SMTP to the operator's relay (Mailpit locally) and
`RecordingMailer` is the replay fake. Messages carry a stable Message-ID per notice and recipient, so a
notice sent again after a crash between sending and recording is recognisable as the same message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str
    message_id: str  # without angle brackets, e.g. "notice-<id>-<n>@example.com"


class MailError(Exception):
    """A send that failed as a whole. `code` is a short reason safe to store; `transient` errors retry."""

    def __init__(self, code: str, *, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


@dataclass(frozen=True)
class SendResult:
    delivered: int
    refused: int  # recipients the relay refused permanently (bad address); never retried


class Mailer(Protocol):
    async def send(self, messages: list[OutgoingEmail]) -> SendResult: ...


class RecordingMailer:
    """Replay fake: keeps every message; `fail` makes the next send raise."""

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []
        self.fail: MailError | None = None
        self.refuse: set[str] = set()

    async def send(self, messages: list[OutgoingEmail]) -> SendResult:
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        delivered = [m for m in messages if m.to not in self.refuse]
        self.sent.extend(delivered)
        return SendResult(delivered=len(delivered), refused=len(messages) - len(delivered))
