"""Google Drive for a signed-in account: connect, browse, import spreadsheets.

Each account connects its own Drive, read-only (lib/google_drive.py). The
refresh token Google hands back is kept in `app_state`, encrypted with a key
made from the app's secret key, so neither the database nor a backup of it
holds a usable token. No route returns one.

Photos are imported by the scan bench (webapp/app.py, `/api/drive/import/photo`),
which uses `drive_for` from here. Spreadsheets go through the same importers
as an uploaded file (webapp/listing_import.py).

Setup: a Google Cloud OAuth client (type "Web application") with
`<this site>/api/drive/callback` as an authorized redirect URI, the Drive API
enabled, and GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
from typing import Optional

from flask import Blueprint, current_app, jsonify, redirect, request, session

from lib import google_drive
from webapp import runtime
from webapp.auth import current_user_id, login_required

drive_bp = Blueprint("drive", __name__)

MAX_SHEET_BYTES = 40 * 1024 * 1024


def configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def _client() -> tuple[str, str]:
    return os.getenv("GOOGLE_CLIENT_ID", ""), os.getenv("GOOGLE_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return request.host_url.rstrip("/") + "/api/drive/callback"


def _fernet():
    from cryptography.fernet import Fernet

    secret = current_app.secret_key
    secret = secret.encode() if isinstance(secret, str) else secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"google-drive:" + secret).digest()))


def _key(uid: int) -> str:
    return f"google-token:user-{uid}"


def _saved_token(uid: int) -> Optional[str]:
    row = runtime.state_get(_key(uid))
    if not row:
        return None
    try:
        return _fernet().decrypt(row["sealed"].encode()).decode()
    except Exception:
        # Sealed under a different secret key: unreadable. Connect again.
        print(f"[drive] stored Google sign-in for user {uid} could not be read", file=sys.stderr)
        return None


def connected(uid: int) -> bool:
    return _saved_token(uid) is not None


def drive_for(uid: int) -> google_drive.Drive:
    """A Drive client for this account. Raises DriveNotAuthorized when the
    account has not connected Drive or Google has withdrawn the sign-in."""
    if not configured():
        raise google_drive.DriveError("Google Drive is not set up on this install")
    token = _saved_token(uid)
    if token is None:
        raise google_drive.DriveNotAuthorized("Google Drive is not connected for this account")
    client_id, client_secret = _client()
    try:
        return google_drive.Drive(google_drive.access_token(client_id, client_secret, token))
    except google_drive.DriveNotAuthorized:
        runtime.state_set(_key(uid), None)       # it will never work again
        raise


def error_response(exc: Exception):
    """The JSON answer for a Drive failure. 409 with reconnect:true when the
    account has to connect again."""
    if isinstance(exc, google_drive.DriveNotAuthorized):
        return jsonify({"error": str(exc), "reconnect": True}), 409
    if isinstance(exc, google_drive.DriveError):
        return jsonify({"error": str(exc)}), 400
    return jsonify({"error": f"could not reach Google Drive ({exc})"}), 502


def _local_path(target: Optional[str]) -> str:
    target = target or "/portfolio"
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/portfolio"
    return target.split("?")[0]


@drive_bp.route("/api/drive/status")
@login_required
def status():
    return jsonify({"configured": configured(), "connected": configured() and connected(current_user_id())})


@drive_bp.route("/api/drive/connect")
@login_required
def connect():
    """Send the browser to Google's consent page. ?next= is the page to
    come back to."""
    back = _local_path(request.args.get("next"))
    if not configured():
        return redirect(f"{back}?drive=not-set-up")
    state = secrets.token_urlsafe(24)
    session["drive_state"], session["drive_next"] = state, back
    return redirect(google_drive.consent_url(_client()[0], _redirect_uri(), state))


@drive_bp.route("/api/drive/callback")
@login_required
def callback():
    back = _local_path(session.pop("drive_next", None))
    expected = session.pop("drive_state", None)
    sent = request.args.get("state") or ""
    # The state proves this answer belongs to a sign-in this browser started.
    if not expected or not hmac.compare_digest(expected, sent):
        return redirect(f"{back}?drive=failed")
    if request.args.get("error") or not request.args.get("code"):
        return redirect(f"{back}?drive=declined")
    client_id, client_secret = _client()
    try:
        tokens = google_drive.exchange_code(client_id, client_secret, _redirect_uri(), request.args["code"])
    except Exception as exc:
        print(f"[drive] sign-in failed: {exc}", file=sys.stderr)
        return redirect(f"{back}?drive=failed")
    runtime.state_set(_key(current_user_id()),
                      {"sealed": _fernet().encrypt(tokens["refresh_token"].encode()).decode()})
    return redirect(f"{back}?drive=connected")


@drive_bp.route("/api/drive/disconnect", methods=["POST"])
@login_required
def disconnect():
    uid = current_user_id()
    token = _saved_token(uid)
    if token:
        google_drive.revoke(token)
    runtime.state_set(_key(uid), None)
    return jsonify({"configured": configured(), "connected": False})


@drive_bp.route("/api/drive/files")
@login_required
def files():
    """?folder=<id|root|shared-with-me>&kind=photos|sheets&page=<token>"""
    try:
        drive = drive_for(current_user_id())
        return jsonify(drive.list(request.args.get("folder") or "root",
                                  request.args.get("kind") or "photos",
                                  request.args.get("page") or None))
    except Exception as exc:
        return error_response(exc)


@drive_bp.route("/api/drive/import/sheet", methods=["POST"])
@login_required
def import_sheet():
    """Body: {file_id, source: tcgplayer|whatnot|cards, dry_run?}. Reads a
    Google Sheet (its first sheet) or a CSV file from Drive and hands it to
    the same importer an uploaded file goes through."""
    from webapp import listing_import

    body = request.get_json(silent=True) or {}
    source = str(body.get("source") or "").lower()
    if source not in listing_import.CSV_SOURCES:
        return jsonify({"error": f"source must be one of: {', '.join(listing_import.CSV_SOURCES)}"}), 400
    uid = current_user_id()
    try:
        text, info = drive_for(uid).csv_text(str(body.get("file_id") or ""), MAX_SHEET_BYTES)
    except Exception as exc:
        return error_response(exc)
    return listing_import.import_text(uid, source, text, bool(body.get("dry_run")),
                                      extra={"file": info.get("name")})
