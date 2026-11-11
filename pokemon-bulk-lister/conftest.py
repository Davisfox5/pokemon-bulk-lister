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


# TEST_DATABASE_URL=postgresql://... runs the whole suite against Postgres
# instead of SQLite files. Tests still ask for a fresh SQLite file each; here
# that becomes "the same Postgres database, emptied".
import os  # noqa: E402

_PG_TEST_URL = os.getenv("TEST_DATABASE_URL")
if _PG_TEST_URL:
    os.environ["DATABASE_URL"] = _PG_TEST_URL

    def _route_tests_to_postgres() -> None:
        import psycopg

        from webapp import db as bench_db
        from webapp import models

        real_init_models, real_init_db = models.init_models, bench_db.init_db
        seen = {"bench": None}

        def empty() -> None:
            if models._engine is not None:   # noqa: SLF001
                models._engine.dispose()     # noqa: SLF001
            while not bench_db._pg_idle.empty():   # noqa: SLF001
                bench_db._pg_idle.get_nowait()[1].close()   # noqa: SLF001
            with psycopg.connect(bench_db.pg_url(), autocommit=True) as conn:
                conn.execute("DROP SCHEMA public CASCADE")
                conn.execute("CREATE SCHEMA public")

        def init_models(url=None):
            if url is not None:
                if url != seen.get("url"):      # the same file again is a relaunch
                    empty()
                    seen["bench"] = None
                seen["url"] = url
                return real_init_models(_PG_TEST_URL)
            return real_init_models()

        def init_db(db_path=bench_db.DEFAULT_DB_PATH):
            if seen["bench"] not in (None, db_path):
                with bench_db.connect() as conn:
                    conn.execute("DROP TABLE IF EXISTS cards")
                    conn.execute("DROP TABLE IF EXISTS grids")
            seen["bench"] = db_path
            return real_init_db(db_path)

        models.init_models, bench_db.init_db = init_models, init_db

    _route_tests_to_postgres()


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
    monkeypatch.setattr(auth, "in_background", lambda fn: fn())
    monkeypatch.setattr(auth, "deliver", lambda to, subject, body: (
        box.append({"to": to, "subject": subject, "body": body}), "console")[1])
    yield box


def verify_link(outbox: list, email: str) -> str:
    """Path of the newest verification link mailed to `email`."""
    body = next(m["body"] for m in reversed(outbox) if m["to"] == email)
    return re.search(r"(/verify-email\?token=\S+)", body).group(1)
