"""The privacy policy is public under a real domain; nothing else becomes so."""
import pytest

from webapp import connections, models


@pytest.fixture
def anon(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICE_REFRESH_ENABLED", "0")
    monkeypatch.setenv("ALLOWED_HOSTS", "bulk.dfconsulting.tech")
    monkeypatch.setenv("ADMIN_EMAILS", "op@example.com")
    from webapp import app as mod
    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    return mod.app.test_client()


PUBLIC = {"Host": "bulk.dfconsulting.tech"}


def test_privacy_policy_is_readable_without_a_session(anon):
    r = anon.get("/privacy", headers=PUBLIC)
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    for words in ("DTF Services LLC", "St. Augustine", "davisfox5@gmail.com", "OAuth tokens",
                  "Sell APIs", "does not sell user data", "/api/ebay/deletion", "eBay buyers"):
        assert words in page
    assert connections.PRIVACY_PATH == "/privacy"


def test_other_pages_and_apis_stay_behind_the_login(anon):
    r = anon.get("/", headers=PUBLIC)
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")
    assert anon.get("/api/cards", headers=PUBLIC).status_code == 401
    assert anon.get("/api/connections", headers=PUBLIC).status_code == 401
    # Look-alike paths are not public.
    for path in ("/privacy/extra", "/privacy-old"):
        r = anon.get(path, headers=PUBLIC)
        assert r.status_code == 302 and r.headers["Location"].startswith("/login")
    # The Origin check still guards state-changing bench routes.
    r = anon.post("/api/cards/listing", headers={**PUBLIC, "Origin": "https://evil.example"}, json={})
    assert r.status_code == 403
