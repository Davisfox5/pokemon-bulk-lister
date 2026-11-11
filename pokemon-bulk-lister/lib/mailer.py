"""Outgoing email. One job today: the address-verification link.

Configured with SMTP_* in .env. With no SMTP_HOST set, nothing is sent: the
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
    return bool(os.getenv("SMTP_HOST"))


def send(to: str, subject: str, body: str) -> str:
    """Send one plain-text message. Returns "smtp" or "console" (how it
    went out). Raises on an SMTP failure so the caller can tell the user."""
    if not configured():
        print(f"[mail] SMTP is not configured; message for {to} follows\n"
              f"        Subject: {subject}\n"
              + "\n".join("        " + line for line in body.splitlines()),
              file=sys.stderr)
        return "console"

    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "no-reply@localhost"
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
