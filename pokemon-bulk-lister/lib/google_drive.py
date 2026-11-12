"""Read-only access to a user's Google Drive: sign-in, browse, download.

Google's documented endpoints:

  consent   GET  https://accounts.google.com/o/oauth2/v2/auth
                 client_id, redirect_uri, response_type=code, scope, state,
                 access_type=offline (to get a refresh token), prompt=consent
  tokens    POST https://oauth2.googleapis.com/token
                 grant_type=authorization_code | refresh_token
  revoke    POST https://oauth2.googleapis.com/revoke?token=...
  list      GET  https://www.googleapis.com/drive/v3/files
                 q, fields, orderBy, pageSize, pageToken
  download  GET  .../drive/v3/files/{id}?alt=media
  export    GET  .../drive/v3/files/{id}/export?mimeType=text/csv
                 (a Google Sheet as CSV: its first sheet, up to 10 MB)

Two ways in. A person signs in with OAuth and the app sees their Drive. Or
a service account (GOOGLE_SA_EMAIL / GOOGLE_SA_PRIVATE_KEY) acts as itself
and sees only what was shared with its email address.

The only scope asked for is drive.readonly. Nothing here can change, share
or delete a file. Not run against Google in this environment (no client id
was available); the tests use stand-in HTTP responses in these shapes.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"

FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"
SHARED = "shared-with-me"             # a pseudo folder id: files others shared
KINDS = {
    "photos": "mimeType contains 'image/'",
    "sheets": f"(mimeType = '{SHEET}' or mimeType = 'text/csv')",
}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class DriveError(RuntimeError):
    pass


class DriveNotAuthorized(DriveError):
    """The saved sign-in no longer works (revoked, or expired). Connect again."""


def consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "state": state,
        # offline + consent: Google only hands out a refresh token on a fresh consent.
        "access_type": "offline", "prompt": "consent",
    })


def _token_request(data: dict, http: Any, timeout: int) -> dict:
    resp = (http or requests).post(TOKEN_URL, data=data, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200:
        # Google's error names are safe to show; token values are never echoed.
        reason = body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}"
        if body.get("error") == "invalid_grant":
            raise DriveNotAuthorized(f"Google no longer accepts this sign-in ({reason}); connect Google Drive again")
        raise DriveError(f"Google refused the sign-in: {reason}")
    return body


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str,
                  http: Any = None, timeout: int = 20) -> dict:
    """Authorization code -> {"refresh_token", "access_token", ...}."""
    body = _token_request({
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect_uri}, http, timeout)
    if not body.get("refresh_token"):
        raise DriveError("Google did not return a refresh token; remove this app's access in your "
                         "Google account and connect again")
    return body


def access_token(client_id: str, client_secret: str, refresh_token: str,
                 http: Any = None, timeout: int = 20) -> str:
    body = _token_request({
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": client_id, "client_secret": client_secret}, http, timeout)
    return body["access_token"]


def service_account_token(email: str, private_key_pem: str, http: Any = None, timeout: int = 20) -> str:
    """An access token for a Google service account (the JWT bearer grant:
    a claim set signed with the account's private key, traded at the token
    endpoint). The account acts as itself, with no user impersonated, so it
    only sees files and folders that were shared with its email address."""
    import base64
    import json
    import time

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    # Environment variables often carry the key with literal "\n" in it.
    pem = private_key_pem.strip().strip('"').replace("\\n", "\n").encode()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as exc:
        raise DriveError("the service account private key could not be read (it should be the PEM text "
                         "from the key file, starting with -----BEGIN PRIVATE KEY-----)") from exc
    now = int(time.time())
    signing_input = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + b"." + b64(json.dumps({
        "iss": email, "scope": SCOPE, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}).encode())
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    body = _token_request({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": (signing_input + b"." + b64(signature)).decode()}, http, timeout)
    return body["access_token"]


def revoke(token: str, http: Any = None, timeout: int = 20) -> None:
    try:
        (http or requests).post(REVOKE_URL, params={"token": token}, timeout=timeout)
    except requests.RequestException:
        pass          # the local copy is forgotten either way


class Drive:
    def __init__(self, token: str, http: Any = None, timeout: int = 60) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = http or requests
        self._timeout = timeout

    def _get(self, url: str, params: Optional[dict] = None):
        resp = self._http.get(url, headers=self._headers, params=params, timeout=self._timeout)
        if resp.status_code == 401:
            raise DriveNotAuthorized("Google no longer accepts this sign-in; connect Google Drive again")
        if resp.status_code == 404:
            raise DriveError("that file was not found in your Drive")
        if resp.status_code == 403 and ("has not been used in project" in resp.text
                                        or "accessNotConfigured" in resp.text):
            raise DriveError("The Google Drive API is switched off in the Google Cloud project these "
                             "credentials belong to. In Google Cloud Console, open that project, go to "
                             "APIs & Services, Library, find Google Drive API and click Enable.")
        if resp.status_code != 200:
            raise DriveError(f"Google Drive answered {resp.status_code}: {resp.text[:200]}")
        return resp

    def list(self, folder: str = "root", kind: str = "photos", page_token: Optional[str] = None) -> dict:
        """One page of a folder: its sub-folders and the files of `kind`."""
        if kind not in KINDS:
            raise DriveError("kind must be photos or sheets")
        if folder != SHARED and not _ID.match(folder):
            raise DriveError("not a Drive folder id")
        where = "sharedWithMe = true" if folder == SHARED else f"'{folder}' in parents"
        params = {
            "q": f"{where} and trashed = false and (mimeType = '{FOLDER}' or {KINDS[kind]})",
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, thumbnailLink)",
            "orderBy": "folder, name", "pageSize": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        body = self._get(f"{API}/files", params).json()
        entries = body.get("files") or []
        return {
            "folders": [{"id": f["id"], "name": f.get("name") or ""} for f in entries if f.get("mimeType") == FOLDER],
            "files": [{"id": f["id"], "name": f.get("name") or "", "mime": f.get("mimeType"),
                       "size": int(f["size"]) if f.get("size") else None,
                       "modified": f.get("modifiedTime"), "thumb": f.get("thumbnailLink")}
                      for f in entries if f.get("mimeType") != FOLDER],
            "next": body.get("nextPageToken"),
        }

    def meta(self, file_id: str) -> dict:
        if not _ID.match(file_id):
            raise DriveError("not a Drive file id")
        return self._get(f"{API}/files/{file_id}", {"fields": "id, name, mimeType, size"}).json()

    def download(self, file_id: str, max_bytes: int) -> tuple[bytes, dict]:
        """A file's bytes and its metadata. Refuses anything over max_bytes."""
        info = self.meta(file_id)
        if info.get("size") and int(info["size"]) > max_bytes:
            raise DriveError(f"{info.get('name')} is over {max_bytes // (1024 * 1024)} MB")
        body = self._get(f"{API}/files/{file_id}", {"alt": "media"}).content
        if len(body) > max_bytes:
            raise DriveError(f"{info.get('name')} is over {max_bytes // (1024 * 1024)} MB")
        return body, info

    def csv_text(self, file_id: str, max_bytes: int) -> tuple[str, dict]:
        """A spreadsheet as CSV text: a Google Sheet's first sheet, or a .csv file."""
        info = self.meta(file_id)
        if info.get("mimeType") == SHEET:
            body = self._get(f"{API}/files/{file_id}/export", {"mimeType": "text/csv"}).content
        elif info.get("mimeType") == "text/csv" or str(info.get("name", "")).lower().endswith(".csv"):
            body = self._get(f"{API}/files/{file_id}", {"alt": "media"}).content
        else:
            raise DriveError(f"{info.get('name')} is not a Google Sheet or a CSV file")
        if len(body) > max_bytes:
            raise DriveError(f"{info.get('name')} is over {max_bytes // (1024 * 1024)} MB")
        return body.decode("utf-8", errors="replace"), info
