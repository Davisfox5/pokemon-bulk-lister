"""Outgoing email: address confirmation and password reset links.

Two ways to send, either one is enough:
  RESEND_API_KEY   Resend's HTTPS API (free tier). The way to go on a
                   serverless host, where one HTTPS request is all it takes.
                   MAIL_FROM must be an address on a domain verified there.
  SMTP_*           any SMTP server.

With neither set, nothing is sent: the
message is printed to the server console instead. On a local install that is
the right behaviour, since whoever can read the console is the operator, and
it still proves the person registering can reach the machine. On a shared
install, set SMTP_HOST so the link only goes to the mailbox.
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage


def configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY") or os.getenv("SMTP_HOST"))


def _sender() -> str:
    return os.getenv("MAIL_FROM") or os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "no-reply@localhost"


def _send_resend(to: str, subject: str, body: str) -> None:
    import requests

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={"from": _sender(), "to": [to], "subject": subject, "text": body},
        timeout=20,
    )
    if r.status_code >= 300:
        try:
            reason = r.json().get("message") or r.text
        except ValueError:
            reason = r.text
        raise RuntimeError(f"mail service refused the message ({r.status_code}): {str(reason)[:300]}")


def send(to: str, subject: str, body: str) -> str:
    """Send one plain-text message. Returns "smtp" or "console" (how it
    went out). Raises on an SMTP failure so the caller can tell the user."""
    if not configured():
        print(f"[mail] no mail service is configured; message for {to} follows\n"
              f"        Subject: {subject}\n"
              + "\n".join("        " + line for line in body.splitlines()),
              file=sys.stderr)
        return "console"

    if os.getenv("RESEND_API_KEY"):
        _send_resend(to, subject, body)
        return "smtp"

    msg = EmailMessage()
    msg["From"] = _sender()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as smtp:
            if user:
                smtp.login(user, password or "")
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            if os.getenv("SMTP_STARTTLS", "1") == "1":
                smtp.starttls(context=ssl.create_default_context())
            if user:
                smtp.login(user, password or "")
            smtp.send_message(msg)
    return "smtp"
