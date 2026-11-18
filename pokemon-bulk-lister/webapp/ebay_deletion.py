"""eBay Marketplace Account Deletion notifications.

eBay requires every production keyset to either subscribe to these
notifications or apply for an exemption. Subscribing means eBay can reach
one public HTTPS address, this one:

  GET  /api/ebay/deletion?challenge_code=X
       Sent once when the endpoint is saved in the eBay developer portal.
       Answered with {"challengeResponse": sha256_hex(X + token + endpoint)}
       where token is EBAY_VERIFICATION_TOKEN (the same string typed into the
       portal) and endpoint is EBAY_DELETION_ENDPOINT (the exact URL saved
       there).

  POST /api/ebay/deletion
       One JSON notification per eBay user who asked eBay to delete their
       data. The X-EBAY-SIGNATURE header is checked against eBay's public key
       (Notification API getPublicKey, fetched with an application token from
       EBAY_CLIENT_ID / EBAY_CLIENT_SECRET and cached for an hour), the way
       eBay's own event-notification SDKs do it:
         signature valid    -> 204, and the user's data is forgotten
         signature invalid  -> 412, nothing happens
         check not possible -> 500, so eBay sends it again later

What there is to forget: this app keeps no data keyed by an eBay buyer or
seller id. The stored eBay authorizations (the operator's, and each account's
own keyset in Advanced settings) are keyed by the app's own account, and
sales read back from eBay keep no buyer names. So a verified notice is
acknowledged and logged by its notificationId only; the user's name and ids
are never written to the log. If the app ever starts storing eBay user data,
delete it in `_forget_ebay_user`.

The route takes no login and ignores the session cookie. connections.py lets
exactly this path through the login and cross-origin checks; the host
allowlist still applies.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time
from typing import Any
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request

DELETION_PATH = "/api/ebay/deletion"
DEFAULT_ENDPOINT = "https://bulk.dfconsulting.tech/api/ebay/deletion"
TOPIC = "MARKETPLACE_ACCOUNT_DELETION"

PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"
APP_SCOPE = "https://api.ebay.com/oauth/api_scope"
KEY_TTL = 3600          # eBay asks that public keys be cached, about an hour
HTTP_TIMEOUT = 8

ebay_deletion_bp = Blueprint("ebay_deletion", __name__)

_lock = threading.Lock()
_keys: dict[str, tuple[float, Any]] = {}          # kid -> (expires, public key)
_app_token: dict[str, Any] = {"value": None, "expires": 0.0}


class VerificationUnavailable(RuntimeError):
    """The signature could not be checked (no keyset, eBay unreachable)."""


def _log(msg: str) -> None:
    print(f"[ebay-deletion] {msg}", file=sys.stderr)


def _endpoint() -> str:
    return (os.getenv("EBAY_DELETION_ENDPOINT") or DEFAULT_ENDPOINT).strip()


def challenge_response(challenge_code: str, token: str, endpoint: str) -> str:
    """eBay's order: challengeCode + verificationToken + endpoint."""
    h = hashlib.sha256()
    h.update(challenge_code.encode("utf-8"))
    h.update(token.encode("utf-8"))
    h.update(endpoint.encode("utf-8"))
    return h.hexdigest()


# ----------------------------------------------------------------------
# Signature check
# ----------------------------------------------------------------------

def _base_url() -> str:
    env = (os.getenv("EBAY_ENV") or "production").lower()
    return SANDBOX_BASE if env == "sandbox" else PRODUCTION_BASE


def _application_token() -> str:
    """Client-credentials token for the Notification API. Never logged."""
    import requests

    with _lock:
        if _app_token["value"] and time.time() < _app_token["expires"] - 60:
            return _app_token["value"]
    client_id = os.getenv("EBAY_CLIENT_ID") or ""
    client_secret = os.getenv("EBAY_CLIENT_SECRET") or ""
    if not client_id or not client_secret:
        raise VerificationUnavailable("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            f"{_base_url()}/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": APP_SCOPE},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        raise VerificationUnavailable(f"token request failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise VerificationUnavailable(f"token request answered {resp.status_code}")
    payload = resp.json()
    with _lock:
        _app_token["value"] = payload.get("access_token")
        _app_token["expires"] = time.time() + int(payload.get("expires_in", 7200))
        return _app_token["value"]


def _load_key(pem_or_b64: str):
    """eBay sends the PEM on one line; the PEM loader needs line breaks."""
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    body = (pem_or_b64.replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", ""))
    body = "".join(body.split())
    return load_der_public_key(base64.b64decode(body))


def _public_key(kid: str):
    import requests

    now = time.time()
    with _lock:
        hit = _keys.get(kid)
        if hit and hit[0] > now:
            return hit[1]
    try:
        resp = requests.get(
            f"{_base_url()}/commerce/notification/v1/public_key/{quote(kid, safe='')}",
            headers={"Authorization": f"Bearer {_application_token()}"},
            timeout=HTTP_TIMEOUT,
        )
    except VerificationUnavailable:
        raise
    except Exception as exc:
        raise VerificationUnavailable(f"getPublicKey failed: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise VerificationUnavailable(f"getPublicKey answered {resp.status_code}")
    try:
        key = _load_key(resp.json()["key"])
    except Exception:
        raise VerificationUnavailable("getPublicKey returned a key that could not be read") from None
    with _lock:
        _keys[kid] = (now + KEY_TTL, key)
    return key


def verify_signature(raw_body: bytes, parsed: Any, header: str) -> bool:
    """True when X-EBAY-SIGNATURE matches the body. False when it does not.
    Raises VerificationUnavailable when it cannot be checked at all."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        sig = json.loads(base64.b64decode(header))
        kid = str(sig["kid"])
        signature = base64.b64decode(sig["signature"])
    except Exception:
        return False
    digest = str(sig.get("digest") or "SHA1").upper().replace("-", "")
    algo = hashes.SHA256() if digest == "SHA256" else hashes.SHA1()
    key = _public_key(kid)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise VerificationUnavailable("eBay's public key is not an EC key")
    # eBay's SDKs verify JSON.stringify(parsed body); the raw body normally
    # is exactly that. Try the raw bytes first, then the compact form.
    candidates = [raw_body]
    compact = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if compact != raw_body:
        candidates.append(compact)
    for message in candidates:
        try:
            key.verify(signature, message, ec.ECDSA(algo))
            return True
        except InvalidSignature:
            continue
    return False


# ----------------------------------------------------------------------
# Acting on a notice
# ----------------------------------------------------------------------

def _forget_ebay_user(user_id: str, username: str, eias_token: str) -> int:
    """Delete everything stored about this eBay user. Returns rows removed.

    Nothing in the app is keyed by an eBay user today (see the module
    docstring), so there is nothing to remove. Add deletions here if that
    changes."""
    return 0


# ----------------------------------------------------------------------
# Route
# ----------------------------------------------------------------------

@ebay_deletion_bp.route(DELETION_PATH, methods=["GET", "POST"])
def ebay_account_deletion():
    if request.method == "GET":
        code = request.args.get("challenge_code", "")
        if not code:
            return jsonify({"error": "challenge_code is required"}), 400
        token = os.getenv("EBAY_VERIFICATION_TOKEN") or ""
        if not token:
            _log("challenge received but EBAY_VERIFICATION_TOKEN is not set")
            return jsonify({"error": "endpoint not configured"}), 503
        # jsonify writes UTF-8 with no byte order mark, as eBay requires.
        return jsonify({"challengeResponse": challenge_response(code, token, _endpoint())}), 200

    raw = request.get_data(cache=True) or b""
    try:
        payload = json.loads(raw)
        notification = payload["notification"]
        data = notification.get("data") or {}
        topic = (payload.get("metadata") or {}).get("topic")
    except Exception:
        return jsonify({"error": "expected an eBay notification"}), 400
    notification_id = str(notification.get("notificationId") or "?")[:80]

    header = request.headers.get("X-EBAY-SIGNATURE") or ""
    if not header:
        _log(f"notification {notification_id}: no signature, refused")
        return jsonify({"error": "missing signature"}), 412
    try:
        ok = verify_signature(raw, payload, header)
    except VerificationUnavailable as exc:
        _log(f"notification {notification_id}: could not verify ({exc}); eBay will retry")
        return jsonify({"error": "could not verify now"}), 500
    except Exception as exc:
        _log(f"notification {notification_id}: verification error {type(exc).__name__}; eBay will retry")
        return jsonify({"error": "could not verify now"}), 500
    if not ok:
        _log(f"notification {notification_id}: signature did not match, ignored")
        return jsonify({"error": "signature did not match"}), 412

    if topic == TOPIC:
        removed = _forget_ebay_user(str(data.get("userId") or ""), str(data.get("username") or ""),
                                    str(data.get("eiasToken") or ""))
        _log(f"notification {notification_id}: verified, {removed} stored record(s) removed")
    else:
        _log(f"notification {notification_id}: verified, topic {str(topic)[:60]!r} ignored")
    return Response(status=204)
