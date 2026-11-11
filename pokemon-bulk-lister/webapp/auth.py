"""Session auth: register / login / logout, email verification, operator.

Plain Flask signed-cookie sessions + werkzeug password hashing. `login_required`
guards the portfolio layer; the scan-bench routes stay open (local tool).

Email verification: registering mails a single-use link (lib/mailer.py; with
no SMTP configured it is printed to the server console). Until the link is
opened the account can keep a library and build decks, but cannot list, buy,
get paid, or act as operator: `verified_required` guards those. This is what
makes ADMIN_EMAILS safe: naming an address there is not enough, the person
must also prove they receive mail at it.
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
import secrets
import sys
from datetime import timedelta
from pathlib import Path

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from lib import mailer
from webapp.models import AlertRule, EmailVerification, User, get_session, utcnow

ROOT = Path(__file__).resolve().parent.parent

auth_bp = Blueprint("auth", __name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Default sell-signal: suggest selling when a card is up ≥25% over 30 days.
SELL_SIGNAL_PCT = float(os.getenv("SELL_SIGNAL_PCT", "25"))
SELL_SIGNAL_WINDOW_DAYS = int(os.getenv("SELL_SIGNAL_WINDOW_DAYS", "30"))


def ensure_secret_key(app) -> None:
    """Stable secret so sessions survive restarts. Env wins; else a keyfile."""
    key = os.getenv("SECRET_KEY")
    if not key:
        path = ROOT / "output" / ".secret_key"
        if path.exists():
            key = path.read_text().strip()
        else:
            key = secrets.token_hex(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key)
            path.chmod(0o600)
    app.secret_key = key


# Who runs this install: may see every seller's balance, record and trigger
# payouts. Comma separated; override with ADMIN_EMAILS. An address only counts
# once its owner has verified it.
DEFAULT_ADMIN_EMAILS = "davisfox5@gmail.com"

VERIFY_LINK_HOURS = 24
VERIFY_RESEND_SECONDS = 60


def current_user_id() -> int | None:
    return session.get("uid")


def admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS") or DEFAULT_ADMIN_EMAILS
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_verified(s, uid) -> bool:
    if not uid:
        return False
    row = s.query(EmailVerification).filter_by(user_id=uid).one_or_none()
    return bool(row and row.verified_at)


def is_operator(s, uid) -> bool:
    """A verified account whose address is in ADMIN_EMAILS. There is no
    fallback: registering first, or registering someone else's address
    without access to its mailbox, grants nothing."""
    if not uid:
        return False
    user = s.get(User, uid)
    return bool(user and user.email.lower() in admin_emails() and is_verified(s, uid))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def send_verification(s, user: User, base_url: str) -> str:
    """Issue a fresh link (the previous one stops working) and send it.
    Returns how it went out: "smtp" or "console". Commits."""
    token = secrets.token_urlsafe(32)
    row = s.query(EmailVerification).filter_by(user_id=user.id).one_or_none()
    if row is None:
        row = EmailVerification(user_id=user.id)
        s.add(row)
    row.token_hash = _hash(token)
    row.sent_at = utcnow()
    s.commit()
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    return deliver(
        user.email,
        "Confirm your email for Card Portfolio",
        "Open this link to confirm this address belongs to you:\n\n"
        f"{link}\n\n"
        f"It works once and expires in {VERIFY_LINK_HOURS} hours. "
        "If you did not create an account, ignore this message.",
    )


def deliver(to: str, subject: str, body: str) -> str:
    """Seam for tests; production goes straight to the mailer."""
    return mailer.send(to, subject, body)


def verified_required(fn):
    """login_required, plus the account's email must be verified."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        uid = session.get("uid")
        if not uid:
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("auth.login_page", next=request.path))
        s = get_session()
        try:
            ok = is_verified(s, uid)
        finally:
            s.close()
        if not ok:
            message = ("Confirm your email address first. Use the link we sent you, "
                       "or ask for a new one from the Library page.")
            if request.path.startswith("/api/"):
                return jsonify({"error": message, "code": "email_unverified"}), 403
            return redirect("/portfolio?verify=needed")
        return fn(*args, **kwargs)

    return wrapper


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("auth.login_page", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def _credentials_from_request() -> tuple[str, str]:
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    return email, password


def _respond(ok_redirect: str, error: str | None = None, template: str | None = None):
    """Uniform JSON/HTML responses for form + fetch clients."""
    wants_json = request.is_json or request.accept_mimetypes.best == "application/json"
    if error:
        if wants_json:
            return jsonify({"error": error}), 400
        return render_template(template, error=error), 400
    if wants_json:
        return jsonify({"ok": True, "redirect": ok_redirect})
    return redirect(ok_redirect)


@auth_bp.route("/register", methods=["GET"])
def register_page():
    return render_template("register.html")


@auth_bp.route("/register", methods=["POST"])
def register():
    email, password = _credentials_from_request()
    if not _EMAIL_RE.match(email):
        return _respond("", "enter a valid email address", "register.html")
    if len(password) < 8:
        return _respond("", "password must be at least 8 characters", "register.html")

    s = get_session()
    try:
        if s.query(User).filter_by(email=email).one_or_none():
            return _respond("", "an account with that email already exists", "register.html")
        # pbkdf2 explicitly: werkzeug's scrypt default needs OpenSSL scrypt,
        # which this deployment's Python (3.9) lacks.
        user = User(email=email, password_hash=generate_password_hash(password, method="pbkdf2:sha256"))
        s.add(user)
        s.flush()
        # Every account starts with the sell-signal rule; users can delete it.
        s.add(AlertRule(
            user_id=user.id,
            kind="sell_signal",
            threshold=SELL_SIGNAL_PCT,
            window_days=SELL_SIGNAL_WINDOW_DAYS,
        ))
        s.commit()
        session["uid"] = user.id
        session.permanent = True
        try:
            send_verification(s, user, request.host_url)
        except Exception as exc:
            # The account exists; they can ask for the link again once mail works.
            print(f"[auth] could not send verification to {user.email}: {exc}", file=sys.stderr)
    finally:
        s.close()
    return _respond("/portfolio?verify=sent")


@auth_bp.route("/verify-email")
def verify_email():
    """The link from the email. A GET that changes state, which is fine here:
    the token is a secret only the mailbox owner has, and it works once."""
    token = request.args.get("token") or ""
    s = get_session()
    try:
        row = s.query(EmailVerification).filter_by(token_hash=_hash(token)).one_or_none() if token else None
        if row is None:
            return redirect("/portfolio?verify=invalid")
        if row.verified_at is None:
            if row.sent_at is None or utcnow() - row.sent_at > timedelta(hours=VERIFY_LINK_HOURS):
                return redirect("/portfolio?verify=expired")
            row.verified_at = utcnow()
        row.token_hash = None   # single use
        s.commit()
    finally:
        s.close()
    return redirect("/portfolio?verify=done")


@auth_bp.route("/api/account")
@login_required
def account():
    uid = current_user_id()
    s = get_session()
    try:
        user = s.get(User, uid)
        if user is None:
            session.pop("uid", None)
            return jsonify({"error": "login required"}), 401
        out = {
            "email": user.email,
            "verified": is_verified(s, uid),
            "is_operator": is_operator(s, uid),
            # An admin address that has not been confirmed yet: say so, since
            # the operator tools stay hidden until it is.
            "admin_pending": user.email.lower() in admin_emails() and not is_verified(s, uid),
            "mail": "smtp" if mailer.configured() else "console",
        }
    finally:
        s.close()
    return jsonify(out)


@auth_bp.route("/api/account/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    uid = current_user_id()
    s = get_session()
    try:
        user = s.get(User, uid)
        if is_verified(s, uid):
            return jsonify({"verified": True})
        row = s.query(EmailVerification).filter_by(user_id=uid).one_or_none()
        if row and row.sent_at and utcnow() - row.sent_at < timedelta(seconds=VERIFY_RESEND_SECONDS):
            return jsonify({"error": "a link was sent a moment ago; wait a minute before asking again"}), 429
        try:
            how = send_verification(s, user, request.host_url)
        except Exception as exc:
            print(f"[auth] could not send verification to {user.email}: {exc}", file=sys.stderr)
            return jsonify({"error": "the email could not be sent; check the SMTP settings"}), 502
    finally:
        s.close()
    return jsonify({"sent": True, "via": how})


@auth_bp.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@auth_bp.route("/login", methods=["POST"])
def login():
    email, password = _credentials_from_request()
    s = get_session()
    try:
        user = s.query(User).filter_by(email=email).one_or_none()
        if user is None or not check_password_hash(user.password_hash, password):
            return _respond("", "invalid email or password", "login.html")
        session["uid"] = user.id
        session.permanent = True
    finally:
        s.close()
    target = request.args.get("next") or "/portfolio"
    if not target.startswith("/"):
        target = "/portfolio"
    return _respond(target)


@auth_bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("uid", None)
    return redirect("/login")
