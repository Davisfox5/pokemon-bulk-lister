"""Where the app is running, and where its files and small bits of state live.

On your own machine everything is a file under this directory, as it always
was. On a serverless host (Vercel) there is no disk that lasts: the code
directory is read-only and /tmp is wiped whenever an instance is recycled.
There:

  files     card photos and page photos are kept in Vercel Blob. /tmp is only
            a working copy; `ensure_local` fetches a file back when a new
            instance needs to read it.
  state     job progress and the eBay tokens are rows in the database
            (`app_state`); tokens are encrypted with a key derived from
            SECRET_KEY.

Everything here is a no-op or a plain file operation when not serverless, so
the local install behaves exactly as before.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent

# Vercel sets VERCEL=1 in every build and function.
SERVERLESS = bool(os.getenv("VERCEL")) or os.getenv("SERVERLESS") == "1"

# Paths saved in the database ("output/crops/page01_r0c0.jpg") are relative
# to this directory.
DATA_ROOT = Path(os.getenv("DATA_ROOT") or ("/tmp/bulk-lister" if SERVERLESS else ROOT))


def data_path(rel: str | Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else DATA_ROOT / p


def rel_path(path: Path) -> str:
    return str(Path(path).relative_to(DATA_ROOT))


# ----------------------------------------------------------------------
# Small persistent state (database)
# ----------------------------------------------------------------------

def state_get(key: str) -> Optional[dict]:
    from webapp.models import AppState, get_session

    s = get_session()
    try:
        row = s.get(AppState, key)
        return json.loads(row.value) if row else None
    finally:
        s.close()


def state_set(key: str, value: Optional[dict]) -> None:
    from webapp.models import AppState, get_session, utcnow

    s = get_session()
    try:
        row = s.get(AppState, key)
        if value is None:
            if row is not None:
                s.delete(row)
        elif row is None:
            s.add(AppState(key=key, value=json.dumps(value), updated_at=utcnow()))
        else:
            row.value, row.updated_at = json.dumps(value), utcnow()
        s.commit()
    finally:
        s.close()


def state_claim(key: str, mutate) -> Optional[dict]:
    """Read a state row, let `mutate(state)` return the new state (or None to
    leave it), and write it only if nobody else changed the row meanwhile.
    Returns the new state when this caller won, else None. This is how one
    of several concurrent requests takes a job's next slice."""
    from sqlalchemy import text

    from webapp.models import AppState, get_session, utcnow

    s = get_session()
    try:
        row = s.get(AppState, key)
        if row is None:
            return None
        before = row.value
        s.rollback()
        after = mutate(json.loads(before))
        if after is None:
            return None
        won = s.execute(
            text("UPDATE app_state SET value = :new, updated_at = :at WHERE key = :key AND value = :old"),
            {"new": json.dumps(after), "at": utcnow(), "key": key, "old": before}).rowcount
        s.commit()
        return after if won == 1 else None
    finally:
        s.close()


# ----------------------------------------------------------------------
# Files that must outlive an instance (Vercel Blob)
# ----------------------------------------------------------------------

def blob_enabled() -> bool:
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("BLOB_STORE_ID"))


def _blob():
    from vercel.blob import sync as blob

    return blob


_CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                  ".webp": "image/webp", ".csv": "text/csv", ".json": "application/json"}


def save_file(path: Path) -> Optional[str]:
    """Keep a file that was just written under DATA_ROOT. Returns its public
    URL when it went to Blob, else None (it is already on a lasting disk)."""
    if not blob_enabled():
        return None
    from webapp.models import StoredFile, get_session

    rel = rel_path(path)
    result = _blob().put_blob(
        rel, Path(path).read_bytes(), access="public", allow_overwrite=True,
        content_type=_CONTENT_TYPES.get(Path(path).suffix.lower()))
    s = get_session()
    try:
        row = s.get(StoredFile, rel)
        if row is None:
            s.add(StoredFile(path=rel, url=result.url))
        else:
            row.url = result.url
        s.commit()
    finally:
        s.close()
    return result.url


def file_url(rel: str) -> Optional[str]:
    """Public URL of a stored file, or None if it only exists on local disk."""
    if not blob_enabled():
        return None
    from webapp.models import StoredFile, get_session

    s = get_session()
    try:
        row = s.get(StoredFile, str(rel))
        return row.url if row else None
    finally:
        s.close()


def ensure_local(path: Path) -> bool:
    """Make sure a stored file is on this instance's disk. True if it is."""
    path = Path(path)
    if path.exists():
        return True
    if not blob_enabled():
        return False
    try:
        url = file_url(rel_path(path))
    except ValueError:          # not under DATA_ROOT, so never stored
        return False
    if not url:
        return False
    try:
        body = _blob().get_blob(url, access="public").body
    except Exception as exc:
        print(f"[storage] could not fetch {path.name}: {exc}", file=sys.stderr)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return True


def stored_stems(prefix: str) -> set[str]:
    """Lower-cased file stems already stored under a path prefix."""
    if not blob_enabled():
        return set()
    from webapp.models import StoredFile, get_session

    s = get_session()
    try:
        rows = s.query(StoredFile.path).filter(StoredFile.path.like(f"{prefix}%")).all()
        return {Path(r[0]).stem.lower() for r in rows}
    finally:
        s.close()


# ----------------------------------------------------------------------
# eBay tokens
# ----------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    secret = os.getenv("SECRET_KEY") or ""
    if not secret:
        raise RuntimeError("SECRET_KEY must be set to store eBay tokens on a serverless host")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def make_ebay_auth(token_key: str, token_path: Path, **kwargs):
    """An EbayUserAuth whose refresh token is kept in a file locally and in
    the database (encrypted) on a serverless host. `token_key` names it there."""
    from lib.ebay_oauth import EbayUserAuth

    if not SERVERLESS:
        return EbayUserAuth(token_path=str(token_path), **kwargs)

    class StoredTokenAuth(EbayUserAuth):
        def _load_refresh_token(self) -> None:
            row = state_get(f"ebay-token:{token_key}")
            if not row:
                return
            try:
                data = json.loads(_fernet().decrypt(row["sealed"].encode()))
            except Exception:
                # Sealed with a different SECRET_KEY: unreadable, so the
                # account has to be connected again.
                print(f"[ebay] stored token for {token_key} could not be read; reconnect eBay",
                      file=sys.stderr)
                return
            self._refresh_token = data.get("refresh_token")
            scopes = data.get("scopes")
            if isinstance(scopes, list) and scopes:
                self._scopes = [str(x) for x in scopes]

        def _save_refresh_token(self, refresh_token: str, expires_in: Optional[int]) -> None:
            payload = {
                "refresh_token": refresh_token, "saved_at": int(time.time()),
                "refresh_expires_in": expires_in,
                "env": "sandbox" if self.is_sandbox else "production", "scopes": self._scopes,
            }
            state_set(f"ebay-token:{token_key}",
                      {"sealed": _fernet().encrypt(json.dumps(payload).encode()).decode()})

    return StoredTokenAuth(token_path=str(token_path), **kwargs)


def forget_ebay_token(token_key: str, token_path: Path) -> None:
    Path(token_path).unlink(missing_ok=True)
    if SERVERLESS:
        state_set(f"ebay-token:{token_key}", None)


# ----------------------------------------------------------------------
# Job progress
# ----------------------------------------------------------------------

class JobState(dict):
    """Progress of a background job. A plain dict in memory locally. On a
    serverless host a status request may land on a different instance than
    the one doing the work, so it is also written to the database (at most
    once a second while it changes) and read from there."""

    def __init__(self, name: str, **initial: Any) -> None:
        super().__init__(initial)
        self.name = name
        self._written = 0.0

    def _changed(self, force: bool = False) -> None:
        if not SERVERLESS:
            return
        now = time.monotonic()
        if force or now - self._written >= 1.0:
            self._written = now
            try:
                state_set(f"job:{self.name}", {**self, "beat": time.time()})
            except Exception as exc:
                print(f"[jobs] could not save {self.name} progress: {exc}", file=sys.stderr)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._changed(force=key == "running")

    def update(self, *args, **kwargs) -> None:
        super().update(*args, **kwargs)
        self._changed(force=True)

    def flush(self) -> None:
        self._changed(force=True)

    def current(self) -> dict:
        """What to report. On a serverless host: what the database says."""
        if not SERVERLESS:
            return dict(self)
        try:
            saved = state_get(f"job:{self.name}")
        except Exception:
            saved = None
        return saved if saved is not None else dict(self)
