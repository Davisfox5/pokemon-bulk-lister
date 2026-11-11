"""eBay user-token OAuth (authorization-code grant + refresh).

The pricing path (`lib/ebay_client.py`) uses the *client-credentials* grant,
which only unlocks read/buy scopes. Creating live listings via the Sell APIs
needs a *user* access token minted from the **authorization-code** grant with
the ``sell.inventory`` / ``sell.account`` scopes — i.e. the seller has to log
in once and consent.

Flow (run once, via ``python -m webapp.setup_ebay``):
  1. Build a consent URL and open it; seller signs in and approves.
  2. eBay redirects to your RuName's accepted URL with ``?code=...``.
  3. Exchange that code for an access token + an ~18-month refresh token.
  4. Cache the refresh token at ``EBAY_USER_TOKEN_PATH``.

After that, ``EbayUserAuth.access_token()`` silently refreshes access tokens
from the stored refresh token — no further logins until the refresh token
itself expires.

Credentials come from the same keyset as the pricing client
(``EBAY_CLIENT_ID`` / ``EBAY_CLIENT_SECRET``) plus a redirect identifier
(``EBAY_REDIRECT_URI``, the *RuName*, not a raw URL).
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests


PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"
PRODUCTION_AUTH = "https://auth.ebay.com/oauth2/authorize"
SANDBOX_AUTH = "https://auth.sandbox.ebay.com/oauth2/authorize"

# Scopes required to create + publish listings and read the account's
# business policies / inventory locations.
USER_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
]


class EbayUserAuthError(RuntimeError):
    pass


class EbayUserNotAuthorized(EbayUserAuthError):
    """No cached refresh token — the consent flow hasn't been run yet."""


class EbayUserAuth:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        env: Optional[str] = None,
        token_path: Optional[str] = None,
        timeout: int = 20,
    ) -> None:
        self.client_id = client_id or os.getenv("EBAY_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("EBAY_CLIENT_SECRET", "")
        # eBay's "redirect_uri" for the auth-code grant is the RuName, not a URL.
        self.redirect_uri = redirect_uri or os.getenv("EBAY_REDIRECT_URI", "")
        env = (env or os.getenv("EBAY_ENV", "production")).lower()
        self.is_sandbox = env == "sandbox"
        self.base_url = SANDBOX_BASE if self.is_sandbox else PRODUCTION_BASE
        self.auth_url = SANDBOX_AUTH if self.is_sandbox else PRODUCTION_AUTH
        self.token_path = Path(
            token_path or os.getenv("EBAY_USER_TOKEN_PATH", "output/cache/ebay_user_token.json")
        )
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._session = requests.Session()
        self._access_token: Optional[str] = None
        self._access_expiry: float = 0.0
        self._refresh_token: Optional[str] = None
        self._load_refresh_token()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_refresh_token(self) -> None:
        if not self.token_path.exists():
            return
        try:
            data = json.loads(self.token_path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._refresh_token = data.get("refresh_token")

    def _save_refresh_token(self, refresh_token: str, expires_in: Optional[int]) -> None:
        payload = {
            "refresh_token": refresh_token,
            "saved_at": int(time.time()),
            "refresh_expires_in": expires_in,
            "env": "sandbox" if self.is_sandbox else "production",
        }
        # Owner-only from the first byte: this is a long-lived credential.
        fd = os.open(self.token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
        try:
            self.token_path.chmod(0o600)   # tighten a file an older build wrote
        except OSError:
            pass

    def is_authorized(self) -> bool:
        return bool(self._refresh_token)

    def _basic_header(self) -> str:
        if not self.client_id or not self.client_secret:
            raise EbayUserAuthError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
        return base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

    # ------------------------------------------------------------------
    # Consent flow (interactive, run once)
    # ------------------------------------------------------------------

    def build_consent_url(self, scopes: Optional[list[str]] = None, state: str = "pokemon-lister") -> str:
        if not self.client_id:
            raise EbayUserAuthError("EBAY_CLIENT_ID not set")
        if not self.redirect_uri:
            raise EbayUserAuthError(
                "EBAY_REDIRECT_URI (your RuName) not set — create one under "
                "developer.ebay.com -> User Tokens -> Get a Token from eBay via Your Application."
            )
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes or USER_SCOPES),
            "state": state,
        }
        return f"{self.auth_url}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def extract_code(redirect_response: str) -> str:
        """Pull the ``code`` from a pasted redirect URL (or accept a bare code)."""
        redirect_response = redirect_response.strip()
        if "code=" in redirect_response:
            parsed = urllib.parse.urlparse(redirect_response)
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [None])[0]
            if not code:
                raise EbayUserAuthError("no ?code= found in the pasted redirect URL")
            return code
        # Looks like a URL but carries no code → the wrong thing was pasted.
        if "://" in redirect_response or "?" in redirect_response:
            raise EbayUserAuthError("no ?code= found in the pasted redirect URL")
        # Otherwise assume the user pasted the bare code.
        return urllib.parse.unquote(redirect_response)

    def exchange_code(self, code: str) -> dict:
        """Trade an authorization code for access + refresh tokens; persist refresh."""
        resp = self._session.post(
            f"{self.base_url}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {self._basic_header()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise EbayUserAuthError(f"code exchange failed {resp.status_code}: {resp.text}")
        payload = resp.json()
        refresh = payload.get("refresh_token")
        if not refresh:
            # Name the fields only: the payload holds a live access token,
            # and this message can end up in an HTTP response or a log.
            raise EbayUserAuthError(
                f"no refresh_token in eBay's response (fields: {sorted(payload)})")
        self._refresh_token = refresh
        self._access_token = payload.get("access_token")
        self._access_expiry = time.time() + int(payload.get("expires_in", 7200))
        self._save_refresh_token(refresh, payload.get("refresh_token_expires_in"))
        return payload

    # ------------------------------------------------------------------
    # Access token minting
    # ------------------------------------------------------------------

    def access_token(self) -> str:
        if self._access_token and time.time() < self._access_expiry - 60:
            return self._access_token
        if not self._refresh_token:
            raise EbayUserNotAuthorized(
                f"No eBay user token at {self.token_path}. Run:\n"
                "    python -m webapp.setup_ebay\n"
                "to sign in and authorize listing access once."
            )
        resp = self._session.post(
            f"{self.base_url}/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {self._basic_header()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": " ".join(USER_SCOPES),
            },
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise EbayUserAuthError(
                f"refresh failed {resp.status_code}: {resp.text} — "
                f"the refresh token may have expired; rerun python -m webapp.setup_ebay"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._access_expiry = time.time() + int(payload.get("expires_in", 7200))
        return self._access_token
