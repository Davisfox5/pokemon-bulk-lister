"""Pytest config shared by the suite.

The portal uploaders import ``playwright.sync_api`` at module load. CI doesn't
install Playwright (it'd pull a full browser for no benefit in unit tests), so
we register a lightweight stub *before* any test imports those modules. This
lets us exercise the uploaders' configuration and guard logic without the real
package.
"""
from __future__ import annotations

import sys
import types


def _stub_playwright() -> None:
    if "playwright.sync_api" in sys.modules:
        return
    pw = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    for name in ("Browser", "BrowserContext", "Page", "Playwright"):
        setattr(sync_api, name, type(name, (), {}))

    class _TimeoutError(Exception):
        pass

    sync_api.TimeoutError = _TimeoutError
    sync_api.sync_playwright = lambda: None  # never called in unit tests
    pw.sync_api = sync_api
    sys.modules["playwright"] = pw
    sys.modules["playwright.sync_api"] = sync_api


_stub_playwright()


import re  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def mail_outbox(monkeypatch):
    """Capture outgoing mail instead of sending it. Tests read the
    verification link out of it, the way a person reads their inbox."""
    try:
        from webapp import auth
    except Exception:   # a test slice without the web layer's dependencies
        yield []
        return
    box: list[dict] = []
    monkeypatch.setattr(auth, "deliver", lambda to, subject, body: (
        box.append({"to": to, "subject": subject, "body": body}), "console")[1])
    yield box


def verify_link(outbox: list, email: str) -> str:
    """Path of the newest verification link mailed to `email`."""
    body = next(m["body"] for m in reversed(outbox) if m["to"] == email)
    return re.search(r"(/verify-email\?token=\S+)", body).group(1)
