"""Send the summary over Gmail SMTP with an app password.

stdlib only. Gmail rejects a normal account password here, so SMTP_APP_PASSWORD
must be a 16-character app password with 2FA enabled on the account.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import EnvSettings

HOST = "smtp.gmail.com"
PORT = 587
TIMEOUT = 30

log = logging.getLogger(__name__)


class MailError(Exception):
    """SMTP refused us. The message explains what to fix."""


def _explain(exc: smtplib.SMTPException) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "Gmail rejected the login. SMTP_APP_PASSWORD must be a 16-character "
            "app password from myaccount.google.com/apppasswords, not the account "
            "password, and 2FA must be on."
        )
    return str(exc)


def verify(env: EnvSettings) -> None:
    """Log in and hang up. Proves the credentials work without sending anything."""
    try:
        with smtplib.SMTP(HOST, PORT, timeout=TIMEOUT) as server:
            server.starttls()
            server.login(env.smtp_user, env.smtp_app_password)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(_explain(exc) if isinstance(exc, smtplib.SMTPException) else str(exc)) from exc


def send(env: EnvSettings, subject: str, html: str, text: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env.smtp_user
    message["To"] = env.mail_to
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(HOST, PORT, timeout=TIMEOUT) as server:
            server.starttls()
            server.login(env.smtp_user, env.smtp_app_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(_explain(exc) if isinstance(exc, smtplib.SMTPException) else str(exc)) from exc
    log.info("summary emailed to %s", env.mail_to)
