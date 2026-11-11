"""Connections: status, eBay consent flow, account check, portal sign-in."""
import json
import time
from contextlib import contextmanager

import pytest
from flask import Flask

from lib import portal_uploader
from webapp import connections

ENV_KEYS = (
    "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REDIRECT_URI", "EBAY_ENV",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "ANTHROPIC_API_KEY",
    "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET",
    "EBAY_FULFILLMENT_POLICY_ID", "EBAY_PAYMENT_POLICY_ID", "EBAY_RETURN_POLICY_ID",
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EBAY_USER_TOKEN_PATH", str(tmp_path / "ebay_token.json"))
    monkeypatch.setenv("WHATNOT_STATE_PATH", str(tmp_path / "whatnot_state.json"))
    monkeypatch.setenv("TCGPLAYER_STATE_PATH", str(tmp_path / "tcgplayer_state.json"))
    monkeypatch.setattr(connections, "_captures", {})
    app = Flask(__name__)
    app.before_request(connections.same_origin_only)
    app.register_blueprint(connections.connections_bp)
    return app.test_client()


def _ebay_env(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id-123")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-secret-456")
    monkeypatch.setenv("EBAY_REDIRECT_URI", "My-RuName")


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._payload


def test_status_says_what_is_missing_and_never_echoes_a_secret(client, monkeypatch):
    data = client.get("/api/connections").get_json()
    assert data["ebay"]["missing"] == ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REDIRECT_URI"]
    assert not data["ebay"]["ready"] and not data["stripe"]["ready"] and not data["whatnot"]["ready"]
    assert data["stripe"]["mode"] is None

    _ebay_env(monkeypatch)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_supersecret")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_alsosecret")
    r = client.get("/api/connections")
    data = r.get_json()
    assert data["ebay"]["configured"] and not data["ebay"]["authorized"]
    assert (data["stripe"]["mode"], data["stripe"]["webhook"]) == ("test", True)
    body = r.get_data(as_text=True)
    for secret in ("supersecret", "alsosecret", "cert-secret-456"):
        assert secret not in body

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    assert client.get("/api/connections").get_json()["stripe"]["mode"] == "live"


def test_requests_from_another_site_are_refused(client):
    url = "/api/connections/ebay/disconnect"
    assert client.post(url, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(url, headers={"Origin": "null"}).status_code == 403
    assert client.post(url, headers={"Origin": "http://localhost"}).status_code == 200
    assert client.post(url).status_code == 200            # curl, scripts, webhooks
    assert client.get("/api/connections", headers={"Origin": "https://evil.example"}).status_code == 200


def test_ebay_consent_and_authorize(client, monkeypatch, tmp_path):
    assert client.post("/api/connections/ebay/consent-url").status_code == 400
    _ebay_env(monkeypatch)
    url = client.post("/api/connections/ebay/consent-url").get_json()["url"]
    assert url.startswith("https://auth.ebay.com/oauth2/authorize?")
    assert "client_id=app-id-123" in url and "redirect_uri=My-RuName" in url
    assert "sell.inventory" in url

    posted = {}

    def fake_post(self, url, headers=None, data=None, timeout=None):
        posted.update(url=url, data=data)
        if data["code"] != "good-code":
            return _Resp(400, {"error": "invalid_grant"})
        return _Resp(200, {"access_token": "at", "expires_in": 7200,
                           "refresh_token": "rt-xyz", "refresh_token_expires_in": 47304000})

    monkeypatch.setattr("requests.Session.post", fake_post)
    assert client.post("/api/connections/ebay/authorize", json={}).status_code == 400
    assert client.post("/api/connections/ebay/authorize",
                       json={"redirect": "https://x.example/accepted?oops=1"}).status_code == 400
    assert client.post("/api/connections/ebay/authorize",
                       json={"redirect": "https://x.example/a?code=bad-code"}).status_code == 400
    assert not (tmp_path / "ebay_token.json").exists()

    r = client.post("/api/connections/ebay/authorize",
                    json={"redirect": "https://x.example/accepted?code=good-code&expires_in=299"})
    assert r.status_code == 200 and r.get_json()["authorized"] and r.get_json()["ready"]
    assert posted["url"].endswith("/identity/v1/oauth2/token")
    assert posted["data"]["redirect_uri"] == "My-RuName"
    saved = json.loads((tmp_path / "ebay_token.json").read_text())
    assert saved["refresh_token"] == "rt-xyz"
    assert "rt-xyz" not in r.get_data(as_text=True)

    assert client.post("/api/connections/ebay/disconnect").get_json()["authorized"] is False
    assert not (tmp_path / "ebay_token.json").exists()


def _authorized(monkeypatch, tmp_path):
    _ebay_env(monkeypatch)
    (tmp_path / "ebay_token.json").write_text(json.dumps({"refresh_token": "rt"}))
    monkeypatch.setattr("requests.Session.post", lambda self, url, **kw: _Resp(
        200, {"access_token": "at", "expires_in": 7200}))


def test_ebay_check_reports_each_requirement(client, monkeypatch, tmp_path):
    r = client.post("/api/connections/ebay/check").get_json()
    assert r["ok"] is False and "not connected" in r["steps"][0]["detail"]

    _authorized(monkeypatch, tmp_path)
    calls = []
    policies = {"fulfillment_policy": {"fulfillmentPolicies": [{"fulfillmentPolicyId": "F1"}]},
                "payment_policy": {"paymentPolicies": [{"paymentPolicyId": "P1"}]},
                "return_policy": {"returnPolicies": []}}

    def fake_request(self, method, url, **kw):
        calls.append((method, url))
        for name, payload in policies.items():
            if url.endswith(f"/sell/account/v1/{name}"):
                return _Resp(200, payload)
        if url.endswith("/sell/inventory/v1/location"):
            return _Resp(200, {"locations": [{"merchantLocationKey": "home"}]})
        return _Resp(404, {})

    monkeypatch.setattr("requests.Session.request", fake_request)
    r = client.post("/api/connections/ebay/check").get_json()
    steps = {s["name"]: s for s in r["steps"]}
    assert r["ok"] is False
    assert steps["Sign-in"]["ok"]
    assert not steps["Business policies"]["ok"] and "no return business policy" in steps["Business policies"]["detail"]
    assert steps["Inventory location"]["ok"] and "home" in steps["Inventory location"]["detail"]
    assert not steps["Photo hosting"]["ok"]
    assert all(method == "GET" for method, _ in calls)     # the check changes nothing on eBay

    policies["return_policy"] = {"returnPolicies": [{"returnPolicyId": "R1"}]}
    for key in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        monkeypatch.setenv(key, "x")
    r = client.post("/api/connections/ebay/check").get_json()
    assert r["ok"] is True and "return R1" in r["steps"][1]["detail"]


def test_stripe_check_without_a_key_says_what_to_do(client):
    r = client.post("/api/connections/stripe/check").get_json()
    assert r["ok"] is False and "STRIPE_SECRET_KEY" in r["detail"]


# ---------------------------------------------------------------- portal sign-in

class _FakeBrowser:
    """Just enough of Playwright for LoginCapture: a page that stays open
    until `close_window` is set, and a context that can save its session."""

    def __init__(self):
        self.closed = False
        self.close_window = False
        self.goto_error = None
        outer = self

        class Page:
            def goto(self, url, **kw):
                outer.url = url
                if outer.goto_error:
                    raise RuntimeError(outer.goto_error)

            def wait_for_timeout(self, ms):
                if outer.close_window:
                    raise RuntimeError("Target page, context or browser has been closed")
                time.sleep(0.01)

        class Context:
            def add_init_script(self, script): pass
            def new_page(self): return Page()
            def storage_state(self, path): open(path, "w").write('{"cookies": [{"name": "sess"}]}')

        self.context = Context()

    def new_context(self, **kw): return self.context
    def close(self): self.closed = True


def _fake_playwright(monkeypatch, browser=None, launch_error=None):
    browser = browser or _FakeBrowser()

    @contextmanager
    def fake_sync_playwright():
        class Chromium:
            def launch(self, **kw):
                browser.launch_kwargs = kw
                if launch_error:
                    raise RuntimeError(launch_error)
                return browser
        yield type("PW", (), {"chromium": Chromium()})()

    monkeypatch.setattr(portal_uploader, "sync_playwright", fake_sync_playwright)
    return browser


def _wait_for(client, site, states, tries=200):
    for _ in range(tries):
        state = client.get("/api/connections").get_json()[site]["login"]["state"]
        if state in states:
            return state
        time.sleep(0.01)
    raise AssertionError(f"{site} login never reached {states}, stuck at {state}")


def test_portal_sign_in_saves_the_session_only_when_the_user_says_so(client, monkeypatch, tmp_path):
    browser = _fake_playwright(monkeypatch)
    state_file = tmp_path / "whatnot_state.json"

    assert client.post("/api/connections/portal/whatnot/login/finish").status_code == 409
    assert client.post("/api/connections/portal/whatnot/login").status_code == 200
    _wait_for(client, "whatnot", ("waiting",))
    assert browser.url == "https://www.whatnot.com/login"
    assert browser.launch_kwargs["headless"] is False      # a window the person can see
    assert client.post("/api/connections/portal/whatnot/login").status_code == 409
    assert not state_file.exists()                          # nothing saved while waiting

    r = client.post("/api/connections/portal/whatnot/login/finish")
    assert r.status_code == 200 and r.get_json()["session"] is True
    assert json.loads(state_file.read_text())["cookies"][0]["name"] == "sess"
    assert browser.closed
    assert client.get("/api/connections").get_json()["tcgplayer"]["session"] is False

    assert client.post("/api/connections/portal/whatnot/disconnect").get_json()["session"] is False
    assert not state_file.exists()


def test_portal_sign_in_cancelled_or_closed_saves_nothing(client, monkeypatch, tmp_path):
    browser = _fake_playwright(monkeypatch)
    client.post("/api/connections/portal/tcgplayer/login")
    _wait_for(client, "tcgplayer", ("waiting",))
    r = client.post("/api/connections/portal/tcgplayer/login/cancel").get_json()
    assert r["login"]["state"] == "cancelled" and r["session"] is False and browser.closed

    browser2 = _fake_playwright(monkeypatch)
    client.post("/api/connections/portal/tcgplayer/login")
    _wait_for(client, "tcgplayer", ("waiting",))
    browser2.close_window = True                            # the person closed the window
    assert _wait_for(client, "tcgplayer", ("cancelled",)) == "cancelled"
    assert client.post("/api/connections/portal/tcgplayer/login/finish").status_code == 409
    assert not (tmp_path / "tcgplayer_state.json").exists()


def test_portal_sign_in_reports_a_browser_that_will_not_open(client, monkeypatch):
    _fake_playwright(monkeypatch, launch_error="Missing X server or $DISPLAY")
    client.post("/api/connections/portal/whatnot/login")
    _wait_for(client, "whatnot", ("error",))
    status = client.get("/api/connections").get_json()["whatnot"]
    assert "Missing X server" in status["login"]["detail"] and status["session"] is False


def test_portal_routes_reject_unknown_sites_and_missing_sessions(client):
    for action in ("login", "check", "disconnect"):
        assert client.post(f"/api/connections/portal/etsy/{action}").status_code == 404
    r = client.post("/api/connections/portal/whatnot/check").get_json()
    assert r["ok"] is False and "sign in first" in r["detail"]


def test_a_rebound_hostname_is_refused_even_when_origin_matches_it(client, monkeypatch):
    # DNS rebinding: evil.example now resolves to 127.0.0.1, so the browser
    # sends a Host and an Origin that agree with each other.
    spoofed = {"Host": "evil.example:5050", "Origin": "http://evil.example:5050"}
    assert client.post("/api/connections/ebay/disconnect", headers=spoofed).status_code == 403
    assert client.get("/api/connections", headers={"Host": "evil.example:5050"}).status_code == 403
    for host in ("localhost:5050", "127.0.0.1:5050", "[::1]:5050"):
        assert client.get("/api/connections", headers={"Host": host}).status_code == 200
    monkeypatch.setenv("ALLOWED_HOSTS", "cards.example.com")
    assert client.get("/api/connections", headers={"Host": "cards.example.com"}).status_code == 200


def test_ebay_errors_and_files_do_not_expose_tokens(client, monkeypatch, tmp_path):
    import stat

    _ebay_env(monkeypatch)
    monkeypatch.setattr("requests.Session.post", lambda self, url, **kw: _Resp(
        200, {"access_token": "v^1.1#LIVE_ACCESS_TOKEN", "expires_in": 7200}))
    r = client.post("/api/connections/ebay/authorize", json={"redirect": "https://x/a?code=c"})
    assert r.status_code == 400 and "LIVE_ACCESS_TOKEN" not in r.get_data(as_text=True)
    assert "access_token" in r.get_json()["error"]          # the field name is fine

    monkeypatch.setattr("requests.Session.post", lambda self, url, **kw: _Resp(
        200, {"access_token": "at", "expires_in": 7200, "refresh_token": "rt"}))
    assert client.post("/api/connections/ebay/authorize",
                       json={"redirect": "https://x/a?code=c"}).status_code == 200
    mode = stat.S_IMODE((tmp_path / "ebay_token.json").stat().st_mode)
    assert mode == 0o600


def test_portal_sign_in_follows_a_popup_and_saves_owner_only(client, monkeypatch, tmp_path):
    import stat

    browser = _fake_playwright(monkeypatch)
    popup = type("Popup", (), {"is_closed": lambda self: False,
                               "wait_for_timeout": lambda self, ms: time.sleep(0.01)})()
    browser.context.pages = [popup]
    client.post("/api/connections/portal/whatnot/login")
    _wait_for(client, "whatnot", ("waiting",))
    browser.close_window = True          # the first tab closes; sign-in continues in the popup
    time.sleep(0.1)
    assert client.get("/api/connections").get_json()["whatnot"]["login"]["state"] == "waiting"
    assert client.post("/api/connections/portal/whatnot/login/finish").status_code == 200
    state_file = tmp_path / "whatnot_state.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert not state_file.with_suffix(".tmp").exists()


def test_a_browser_that_hangs_while_saving_does_not_block_the_next_sign_in(monkeypatch, tmp_path):
    browser = _fake_playwright(monkeypatch)
    browser.context.storage_state = lambda path: time.sleep(1.0)     # never finishes in time
    capture = portal_uploader.LoginCapture("whatnot", tmp_path / "s.json")
    capture.start()
    for _ in range(200):
        if capture.status()["state"] == "waiting":
            break
        time.sleep(0.01)
    status = capture.finish(timeout=0.1)
    assert status["state"] == "error" and not capture.active()
    capture.start()                                                    # a fresh attempt is allowed
    capture.cancel()
