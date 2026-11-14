"""How this app signs in to the Claude API.

Two ways, the first that is configured wins:

1. A key: ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN). The SDK reads it.
2. No key at all (Workload Identity Federation). The host proves which
   deployment this is with a short-lived identity token, and Anthropic swaps
   it for a short-lived access token. Nothing secret is stored anywhere.
   Needs ANTHROPIC_FEDERATION_RULE_ID and ANTHROPIC_ORGANIZATION_ID
   (ANTHROPIC_SERVICE_ACCOUNT_ID and ANTHROPIC_WORKSPACE_ID are optional),
   plus an identity token from the host.

On Vercel the identity token arrives with every request, in the
`x-vercel-oidc-token` header; `remember_identity_token` keeps the latest one.
`vercel dev` and builds put it in VERCEL_OIDC_TOKEN instead. Any other host
can supply one through ANTHROPIC_IDENTITY_TOKEN / ANTHROPIC_IDENTITY_TOKEN_FILE,
which the SDK reads by itself.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

_lock = threading.Lock()
_identity_token: Optional[str] = None
_client: Any = None


def remember_identity_token(token: Optional[str]) -> None:
    """Keep the host's identity token for this deployment (it is the same
    for every request until the host rotates it)."""
    global _identity_token
    if token:
        with _lock:
            _identity_token = token


def _host_identity_token() -> str:
    token = _identity_token or os.getenv("VERCEL_OIDC_TOKEN")
    if not token:
        raise RuntimeError(
            "no identity token from the host yet: on Vercel it comes with each request "
            "(is OIDC federation enabled for the project?)")
    return token


def has_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def federated() -> bool:
    """Keyless sign-in through the host's identity token is set up here."""
    return (not has_key()
            and bool(os.getenv("ANTHROPIC_FEDERATION_RULE_ID") and os.getenv("ANTHROPIC_ORGANIZATION_ID"))
            and not (os.getenv("ANTHROPIC_IDENTITY_TOKEN") or os.getenv("ANTHROPIC_IDENTITY_TOKEN_FILE")))


def configured() -> bool:
    return has_key() or bool(os.getenv("ANTHROPIC_FEDERATION_RULE_ID") and os.getenv("ANTHROPIC_ORGANIZATION_ID"))


def client():
    """An Anthropic client signed in whichever way is configured."""
    import anthropic  # deferred so the webapp can start without the SDK installed

    if not federated():
        return anthropic.Anthropic()
    global _client
    with _lock:
        if _client is None:
            from anthropic.lib.credentials import WorkloadIdentityCredentials

            # One client for the process: it caches the access token and
            # exchanges again, with the latest identity token, when it expires.
            _client = anthropic.Anthropic(credentials=WorkloadIdentityCredentials(
                identity_token_provider=_host_identity_token,
                federation_rule_id=os.environ["ANTHROPIC_FEDERATION_RULE_ID"],
                organization_id=os.environ["ANTHROPIC_ORGANIZATION_ID"],
                service_account_id=os.getenv("ANTHROPIC_SERVICE_ACCOUNT_ID") or None,
                workspace_id=os.getenv("ANTHROPIC_WORKSPACE_ID") or None,
            ))
        return _client
