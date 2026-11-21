"""SMTP delivery through the standard library, run in a thread so the event loop never blocks.

TLS modes: `starttls` (port 587, the default), `tls` (implicit TLS, port 465) and `none` (plain SMTP, for
a relay on the same host or network such as the local Mailpit). Certificates are verified with the
system's trust store. A connection, timeout or 4xx answer is transient; a 5xx answer for the whole
session (authentication, sender) is permanent; a refused recipient is counted and skipped.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate
from typing import Literal

from app.adapters.email import MailError, OutgoingEmail, SendResult

TlsMode = Literal["starttls", "tls", "none"]
TIMEOUT_SECONDS = 30.0


class SmtpMailer:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        *,
        tls: TlsMode = "starttls",
        username: str | None = None,
        password: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.tls = tls
        self.username = username
        self.password = password
        self.timeout = timeout

    async def send(self, messages: list[OutgoingEmail]) -> SendResult:
        if not messages:
            return SendResult(0, 0)
        return await asyncio.to_thread(self._send, messages)

    def _message(self, mail: OutgoingEmail) -> EmailMessage:
        # email.policy.default (EmailMessage) refuses CR/LF in header values, so no header can be injected.
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = mail.to
        message["Subject"] = mail.subject
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = f"<{mail.message_id}>"
        message["Auto-Submitted"] = "auto-generated"  # RFC 3834: no auto-replies to notices
        message.set_content(mail.body)
        return message

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self.tls == "tls":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            )
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            if self.tls == "starttls":
                client.starttls(context=context)
        if self.username is not None and self.password is not None:
            client.login(self.username, self.password)
        return client

    def _send(self, messages: list[OutgoingEmail]) -> SendResult:
        delivered = refused = 0
        try:
            client = self._connect()
        except smtplib.SMTPAuthenticationError as error:
            raise MailError("smtp_auth_failed", transient=False) from error
        except smtplib.SMTPResponseException as error:
            raise MailError("smtp_rejected", transient=error.smtp_code < 500) from error
        except (OSError, smtplib.SMTPException) as error:
            raise MailError("smtp_unavailable", transient=True) from error
        try:
            for mail in messages:
                try:
                    client.send_message(self._message(mail))
                    delivered += 1
                except smtplib.SMTPRecipientsRefused as error:
                    codes = [code for code, _ in error.recipients.values()]
                    if any(code < 500 for code in codes):
                        raise MailError("smtp_recipient_deferred", transient=True) from error
                    refused += 1
                except smtplib.SMTPSenderRefused as error:
                    raise MailError("smtp_sender_refused", transient=error.smtp_code < 500) from error
                except smtplib.SMTPResponseException as error:
                    raise MailError("smtp_rejected", transient=error.smtp_code < 500) from error
                except (OSError, smtplib.SMTPException) as error:
                    raise MailError("smtp_unavailable", transient=True) from error
        finally:
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()
        return SendResult(delivered, refused)
