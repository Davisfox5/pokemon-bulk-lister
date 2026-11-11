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
import threading
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
from webapp.models import (
    AlertRule,
    EmailVerification,
    PasswordReset,
    User,
    get_session,
    utcnow,
)

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
        from webapp.runtime import DATA_ROOT, SERVERLESS
        if SERVERLESS:
            # A generated key would differ per instance and vanish with it:
            # every login would break at random.
            raise RuntimeError("Set the SECRET_KEY environment variable (a long random string).")
        path = DATA_ROOT / "output" / ".secret_key"
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


RESET_LINK_MINUTES = 60
RESET_RESEND_SECONDS = 60
MIN_PASSWORD = 8


def _stamp(user: User) -> str:
    """Ties a session to the password it was opened with. Changing the
    password changes the stamp, which signs every other browser out."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def start_session(user: User) -> None:
    session["uid"] = user.id
    session["pw"] = _stamp(user)
    session.permanent = True


def current_user_id() -> int | None:
    """The logged-in user's id, or None. A session opened before the
    account's password was last changed no longer counts."""
    uid = session.get("uid")
    if not uid:
        return None
    s = get_session()
    try:
        user = s.get(User, uid)
        # A cookie with no stamp (from before stamps existed) is not accepted:
        # there is no telling which password it was opened with. Everyone
        # logs in once more after the upgrade.
        ok = user is not None and session.get("pw") == _stamp(user)
    finally:
        s.close()
    if not ok:
        session.pop("uid", None)
        session.pop("pw", None)
        return None
    return uid


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
        uid = current_user_id()
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
        if not current_user_id():
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
        start_session(user)
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
    """The link from the email.

    Opening the link is not enough by itself: the browser must also be logged
    in to the account the link was issued for. Otherwise someone could
    register another person's address with their own password and wait for
    the real owner to click the link, ending up with a confirmed account (and,
    for an address in ADMIN_EMAILS, the operator role) that the owner cannot
    get into. A logged-out visitor is sent to log in and brought back.
    """
    token = request.args.get("token") or ""
    uid = current_user_id()
    if not uid:
        return redirect(url_for("auth.login_page", next=f"/verify-email?token={token}"))
    s = get_session()
    try:
        row = s.query(EmailVerification).filter_by(token_hash=_hash(token)).one_or_none() if token else None
        if row is None:
            return redirect("/portfolio?verify=invalid")
        if row.user_id != uid:
            return redirect("/portfolio?verify=wrong_account")
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
        start_session(user)
    finally:
        s.close()
    target = request.args.get("next") or "/portfolio"
    # Only a path on this site. "//host" and "/\\host" are other sites to a browser.
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        target = "/portfolio"
    return _respond(target)


# ----------------------------------------------------------------------
# Forgotten password
# ----------------------------------------------------------------------

def in_background(fn) -> None:
    """Run `fn` off the request. Tests swap this to run it inline."""
    threading.Thread(target=fn, daemon=True).start()


FORGOT_SENT = ("If an account exists for that address, a link to choose a new password "
               "is on its way. It works once and lasts an hour.")


@auth_bp.route("/forgot-password", methods=["GET"])
def forgot_page():
    return render_template("forgot.html", mail="smtp" if mailer.configured() else "console")


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot():
    """Send a reset link. The answer is the same whether or not the address
    has an account, so this cannot be used to find out who is registered."""
    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or "").strip().lower()
    s = get_session()
    try:
        user = s.query(User).filter_by(email=email).one_or_none() if email else None
        if user is not None:
            row = s.query(PasswordReset).filter_by(user_id=user.id).one_or_none()
            recent = row and row.sent_at and utcnow() - row.sent_at < timedelta(seconds=RESET_RESEND_SECONDS)
            if not recent:
                token = secrets.token_urlsafe(32)
                if row is None:
                    row = PasswordReset(user_id=user.id)
                    s.add(row)
                row.token_hash, row.sent_at = _hash(token), utcnow()
                s.commit()
                link = f"{request.host_url.rstrip('/')}/reset-password?token={token}"
                to = user.email

                def send() -> None:
                    try:
                        deliver(
                            to, "Choose a new password for Card Portfolio",
                            "Open this link to choose a new password:\n\n"
                            f"{link}\n\n"
                            f"It works once and expires in {RESET_LINK_MINUTES} minutes. "
                            "If you did not ask for this, ignore this message; your password "
                            "has not changed.",
                        )
                    except Exception as exc:
                        print(f"[auth] could not send reset link to {to}: {exc}", file=sys.stderr)

                # Sent after the response: how long the mail server takes
                # must not show which addresses have accounts.
                in_background(send)
    finally:
        s.close()
    if request.is_json:
        return jsonify({"ok": True, "message": FORGOT_SENT})
    return render_template("forgot.html", sent=FORGOT_SENT,
                           mail="smtp" if mailer.configured() else "console")


def _reset_row(s, token: str):
    """The live reset row for a token, or None if unknown, used or expired."""
    row = s.query(PasswordReset).filter_by(token_hash=_hash(token)).one_or_none() if token else None
    if row is None or row.sent_at is None:
        return None
    if utcnow() - row.sent_at > timedelta(minutes=RESET_LINK_MINUTES):
        return None
    return row


@auth_bp.route("/reset-password", methods=["GET"])
def reset_page():
    token = request.args.get("token") or ""
    s = get_session()
    try:
        ok = _reset_row(s, token) is not None
    finally:
        s.close()
    if not ok:
        return render_template(
            "forgot.html", mail="smtp" if mailer.configured() else "console",
            error="That link has expired or was already used. Ask for a new one."), 400
    return render_template("reset.html", token=token)


@auth_bp.route("/reset-password", methods=["POST"])
def reset():
    data = request.get_json(silent=True) or request.form
    token, password = data.get("token") or "", data.get("password") or ""
    if len(password) < MIN_PASSWORD:
        return render_template(
            "reset.html", token=token,
            error=f"password must be at least {MIN_PASSWORD} characters"), 400
    s = get_session()
    try:
        row = _reset_row(s, token)
        if row is None:
            return render_template(
                "forgot.html", mail="smtp" if mailer.configured() else "console",
                error="That link has expired or was already used. Ask for a new one."), 400
        user = s.get(User, row.user_id)
        user.password_hash = generate_password_hash(password, method="pbkdf2:sha256")
        row.token_hash = None     # single use
        # Opening the link proved they receive mail at this address, which is
        # all that confirming it asks for.
        proof = s.query(EmailVerification).filter_by(user_id=user.id).one_or_none()
        if proof is None:
            proof = EmailVerification(user_id=user.id)
            s.add(proof)
        if proof.verified_at is None:
            proof.verified_at = utcnow()
        proof.token_hash = None
        s.commit()
        # The new password changes the session stamp: every browser logged in
        # with the old one is signed out. This one gets a fresh session.
        start_session(user)
    finally:
        s.close()
    return redirect("/portfolio?reset=done")


@auth_bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("pw", None)
    session.pop("uid", None)
    return redirect("/login")
