"""Google Drive for a signed-in account: connect, browse, import spreadsheets.

Each account connects its own Drive, read-only (lib/google_drive.py). The
refresh token Google hands back is kept in `app_state`, encrypted with a key
made from the app's secret key, so neither the database nor a backup of it
holds a usable token. No route returns one.

Photos are imported by the scan bench (webapp/app.py, `/api/drive/import/photo`),
which uses `drive_for` from here. Spreadsheets go through the same importers
as an uploaded file (webapp/listing_import.py).

An install can also have a Google service account (GOOGLE_SA_EMAIL,
GOOGLE_SA_PRIVATE_KEY). It needs no sign-in: the operator shares a Drive
folder with the service account's address and imports from it. Only the
operator gets it.

Setup for per-account sign-in: a Google Cloud OAuth client (type "Web application") with
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
    """Per-account sign-in (OAuth) is set up."""
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def service_account() -> Optional[tuple[str, str]]:
    """(email, private key) of the install's Google service account, if set."""
    email, key = os.getenv("GOOGLE_SA_EMAIL", "").strip(), os.getenv("GOOGLE_SA_PRIVATE_KEY", "")
    return (email, key) if email and key.strip() else None


def _uses_service_account(uid: int) -> bool:
    """The service account stands for the install, so it is the operator's
    alone: what is shared with it must not be browsable by every account.
    An operator who connected their own Drive uses that instead."""
    from webapp.auth import is_operator
    from webapp.models import get_session

    if service_account() is None or (configured() and connected(uid)):
        return False
    s = get_session()
    try:
        return is_operator(s, uid)
    finally:
        s.close()


_sa_token: dict = {"value": None, "until": 0.0, "for": None}


def _service_account_drive() -> google_drive.Drive:
    import time

    email, key = service_account()
    if _sa_token["for"] != email or time.time() >= _sa_token["until"]:
        _sa_token.update(value=google_drive.service_account_token(email, key),
                         until=time.time() + 3000, **{"for": email})
    return google_drive.Drive(_sa_token["value"])


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
    if _uses_service_account(uid):
        return _service_account_drive()
    if not configured():
        raise google_drive.DriveError(
            "Google Drive on this install is the operator's only" if service_account()
            else "Google Drive is not set up on this install")
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


def _folder_key(uid: int) -> str:
    return f"drive-folder:user-{uid}"


@drive_bp.route("/api/drive/status")
@login_required
def status():
    uid = current_user_id()
    # The folder this account opened by link last time; the picker starts there.
    start = runtime.state_get(_folder_key(uid))
    if _uses_service_account(uid):
        # The address is what the operator shares a private Drive folder with.
        return jsonify({"configured": True, "connected": True, "mode": "service_account",
                        "share_with": service_account()[0], "start_folder": start})
    return jsonify({"configured": configured(), "connected": configured() and connected(uid),
                    "mode": "oauth", "start_folder": start})


@drive_bp.route("/api/drive/folder", methods=["POST", "DELETE"])
@login_required
def folder_link():
    """POST {link}: open a folder by its Drive link and remember it as this
    account's starting point. The folder has to be one the sign-in can open:
    the account's own, one shared with it, or one shared as "anyone with the
    link". DELETE forgets it."""
    uid = current_user_id()
    if request.method == "DELETE":
        runtime.state_set(_folder_key(uid), None)
        return jsonify({"start_folder": None})
    body = request.get_json(silent=True) or {}
    try:
        found = drive_for(uid).folder(google_drive.folder_id_from_link(str(body.get("link") or "")))
    except Exception as exc:
        return error_response(exc)
    runtime.state_set(_folder_key(uid), found)
    return jsonify({"start_folder": found})


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
    return jsonify({"configured": configured(), "connected": False, "mode": "oauth"})


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


@drive_bp.route("/api/drive/picker")
@login_required
def picker_config():
    """What Google's own file picker needs to open for this account: a
    short-lived access token for the account's read-only Drive sign-in, the
    Google Cloud project number, and the browser API key if one is set.

    The token goes only to the signed-in owner's own browser, lasts about an
    hour and can only read. The long-lived sign-in never leaves the server."""
    uid = current_user_id()
    if _uses_service_account(uid) or not configured():
        return jsonify({"error": "Google's picker needs a Google sign-in on this account"}), 400
    saved = _saved_token(uid)
    if saved is None:
        return jsonify({"error": "Google Drive is not connected for this account", "reconnect": True}), 409
    client_id, client_secret = _client()
    try:
        token = google_drive.access_token(client_id, client_secret, saved)
    except google_drive.DriveNotAuthorized as exc:
        runtime.state_set(_key(uid), None)
        return error_response(exc)
    except Exception as exc:
        return error_response(exc)
    response = jsonify({
        "token": token,
        # A client id is "<project number>-<random>.apps.googleusercontent.com".
        "app_id": client_id.split("-")[0],
        "api_key": os.getenv("GOOGLE_API_KEY") or None,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@drive_bp.route("/api/drive/folder-files")
@login_required
def folder_files():
    """?folder=<id>&kind=photos|sheets: every such file directly inside a
    folder that was chosen in the picker."""
    try:
        drive = drive_for(current_user_id())
        folder_id = google_drive.folder_id_from_link(request.args.get("folder") or "")
        info = drive.folder(folder_id)
        return jsonify({"folder": info, "files": drive.list_all(folder_id, request.args.get("kind") or "photos")})
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
