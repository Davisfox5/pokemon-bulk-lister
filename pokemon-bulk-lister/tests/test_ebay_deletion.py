"""eBay marketplace account deletion endpoint: challenge, signed notices,
and that it is the one bench path reachable without a login."""
import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask

from webapp import connections, ebay_deletion

ENDPOINT = "https://bulk.dfconsulting.tech/api/ebay/deletion"
TOKEN = "tok_abcdefghijklmnopqrstuvwxyz-0123456789"

NOTICE = {
    "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0", "deprecated": False},
    "notification": {
        "notificationId": "49feeaeb-4bb7-4a4f-b1f5-2b1b0a0b0001",
        "eventDate": "2025-09-19T20:43:59.462Z",
        "publishDate": "2025-09-19T20:43:59.679Z",
        "publishAttemptCount": 1,
        "data": {"username": "someseller", "userId": "ma8vp1jySJC", "eiasToken": "nY+sHZ2PrBmdj6wVnY+seQ=="},
    },
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in ("EBAY_VERIFICATION_TOKEN", "EBAY_DELETION_ENDPOINT", "EBAY_CLIENT_ID",
                "EBAY_CLIENT_SECRET", "EBAY_ENV"):
        monkeypatch.delenv(key, raising=False)
    ebay_deletion._keys.clear()
    ebay_deletion._app_token.update(value=None, expires=0.0)


@pytest.fixture
def client():
    app = Flask(__name__)
    app.before_request(connections.same_origin_only)
    app.register_blueprint(ebay_deletion.ebay_deletion_bp)
    return app.test_client()


@pytest.fixture
def signer(monkeypatch):
    """A key pair standing in for eBay's, served by a fake getPublicKey."""
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    one_line = "".join(pem.splitlines())   # eBay sends the PEM on one line
    calls = {"token": 0, "key": 0}

    class _R:
        def __init__(self, status, payload):
            self.status_code, self._p = status, payload

        def json(self):
            return self._p

    def fake_post(url, **kw):
        calls["token"] += 1
        assert url.endswith("/identity/v1/oauth2/token")
        assert kw["data"]["grant_type"] == "client_credentials"
        return _R(200, {"access_token": "app-token", "expires_in": 7200})

    def fake_get(url, headers=None, **kw):
        calls["key"] += 1
        assert "/commerce/notification/v1/public_key/kid-1" in url
        assert headers["Authorization"] == "Bearer app-token"
        return _R(200, {"algorithm": "ECDSA", "digest": "SHA1", "key": one_line})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("EBAY_CLIENT_ID", "app-id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "cert-secret")

    def sign(body: bytes, key=private) -> str:
        sig = key.sign(body, ec.ECDSA(hashes.SHA1()))
        header = {"alg": "ECDSA", "kid": "kid-1", "signature": base64.b64encode(sig).decode(), "digest": "SHA1"}
        return base64.b64encode(json.dumps(header).encode()).decode()

    sign.calls = calls
    return sign


def test_challenge_is_sha256_of_code_token_endpoint(client, monkeypatch):
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    r = client.get("/api/ebay/deletion?challenge_code=abc123")
    assert r.status_code == 200
    assert r.mimetype == "application/json"
    assert not r.data.startswith(b"\xef\xbb\xbf")        # no byte order mark
    want = hashlib.sha256(("abc123" + TOKEN + ENDPOINT).encode()).hexdigest()
    assert r.get_json() == {"challengeResponse": want}


def test_challenge_uses_configured_endpoint(client, monkeypatch):
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    monkeypatch.setenv("EBAY_DELETION_ENDPOINT", "https://cards.example.com/api/ebay/deletion")
    r = client.get("/api/ebay/deletion?challenge_code=xyz")
    want = hashlib.sha256(("xyz" + TOKEN + "https://cards.example.com/api/ebay/deletion").encode()).hexdigest()
    assert r.get_json()["challengeResponse"] == want


def test_challenge_without_token_fails_and_leaks_nothing(client):
    r = client.get("/api/ebay/deletion?challenge_code=abc123")
    assert r.status_code == 503
    assert "challengeResponse" not in r.get_json()


def test_missing_challenge_code_is_400(client, monkeypatch):
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    assert client.get("/api/ebay/deletion").status_code == 400
    assert TOKEN not in client.get("/api/ebay/deletion").get_data(as_text=True)


def test_signed_notice_is_acknowledged_and_never_logs_the_user(client, signer, capsys):
    body = json.dumps(NOTICE, separators=(",", ":")).encode()
    r = client.post("/api/ebay/deletion", data=body, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(body)})
    assert 200 <= r.status_code < 300
    err = capsys.readouterr().err
    assert "verified" in err and NOTICE["notification"]["notificationId"] in err
    for secret in ("someseller", "ma8vp1jySJC", "nY+sHZ2P", "app-token", "cert-secret"):
        assert secret not in err

    # The public key and app token are cached, not fetched per notice.
    r = client.post("/api/ebay/deletion", data=body, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(body)})
    assert r.status_code == 204
    assert signer.calls == {"token": 1, "key": 1}


def test_notice_signed_over_compact_json_verifies_when_sent_pretty(client, signer):
    # eBay's SDKs verify JSON.stringify(body); accept a body sent with spacing.
    compact = json.dumps(NOTICE, separators=(",", ":")).encode()
    pretty = json.dumps(NOTICE, indent=2).encode()
    r = client.post("/api/ebay/deletion", data=pretty, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(compact)})
    assert r.status_code == 204


def test_forged_or_unsigned_notice_is_refused(client, signer):
    body = json.dumps(NOTICE, separators=(",", ":")).encode()
    other = ec.generate_private_key(ec.SECP256R1())
    r = client.post("/api/ebay/deletion", data=body, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(body, key=other)})
    assert r.status_code == 412
    tampered = body.replace(b"someseller", b"otherguy")
    r = client.post("/api/ebay/deletion", data=tampered, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(body)})
    assert r.status_code == 412
    assert client.post("/api/ebay/deletion", data=body, content_type="application/json").status_code == 412
    assert client.post("/api/ebay/deletion", data=body, content_type="application/json",
                       headers={"X-EBAY-SIGNATURE": "not base64 json"}).status_code == 412


def test_notice_that_cannot_be_checked_is_left_for_ebay_to_retry(client):
    # No keyset configured: eBay gets a 5xx and sends the notice again later.
    body = json.dumps(NOTICE).encode()
    header = base64.b64encode(json.dumps({"kid": "kid-1", "signature": "AAAA"}).encode()).decode()
    r = client.post("/api/ebay/deletion", data=body, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": header})
    assert r.status_code == 500


def test_not_a_notification_is_400(client):
    assert client.post("/api/ebay/deletion", data=b"nope", content_type="application/json").status_code == 400


def test_cross_origin_post_from_ebay_passes_the_origin_check(client, signer):
    # Only this exact path skips the Origin check; the host check still applies.
    body = json.dumps(NOTICE, separators=(",", ":")).encode()
    r = client.post("/api/ebay/deletion", data=body, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(body), "Origin": "https://ebay.com"})
    assert r.status_code == 204
    r = client.post("/api/ebay/deletion", data=body, content_type="application/json",
                    headers={"X-EBAY-SIGNATURE": signer(body), "Host": "evil.example"})
    assert r.status_code == 403


def test_paths_agree():
    assert ebay_deletion.DELETION_PATH == connections.EBAY_DELETION_PATH


def test_reachable_without_login_under_a_real_domain_while_other_api_paths_401(
        tmp_path, monkeypatch, signer):
    from webapp import auth, models

    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    monkeypatch.setenv("ADMIN_EMAILS", "op@example.com")
    monkeypatch.setenv("ALLOWED_HOSTS", "bulk.dfconsulting.tech")
    monkeypatch.setenv("EBAY_VERIFICATION_TOKEN", TOKEN)
    templates = Path(__file__).resolve().parent.parent / "webapp" / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.secret_key = "test"
    app.before_request(connections.same_origin_only)
    app.before_request(connections.operator_only_when_public)
    app.register_blueprint(connections.connections_bp)
    app.register_blueprint(auth.auth_bp)
    app.register_blueprint(ebay_deletion.ebay_deletion_bp)
    app.add_url_rule("/api/cards", "cards", lambda: "[]")
    app.add_url_rule("/api/ebay/other", "other", lambda: "x", methods=["GET", "POST"])
    app.add_url_rule("/api/ebay/deletion/extra", "extra", lambda: "x", methods=["GET", "POST"])
    public = {"Host": "bulk.dfconsulting.tech"}
    anon = app.test_client()

    r = anon.get("/api/ebay/deletion?challenge_code=c1", headers=public)
    assert r.status_code == 200
    assert r.get_json()["challengeResponse"] == hashlib.sha256(("c1" + TOKEN + ENDPOINT).encode()).hexdigest()

    body = json.dumps(NOTICE, separators=(",", ":")).encode()
    r = anon.post("/api/ebay/deletion", data=body, content_type="application/json",
                  headers={**public, "X-EBAY-SIGNATURE": signer(body)})
    assert r.status_code == 204

    # Everything else stays behind the login, including look-alike paths.
    assert anon.get("/api/cards", headers=public).status_code == 401
    assert anon.get("/api/connections", headers=public).status_code == 401
    assert anon.post("/api/connections/ebay/disconnect", headers=public).status_code == 401
    assert anon.get("/api/ebay/other", headers=public).status_code == 401
    assert anon.get("/api/ebay/deletion/extra", headers=public).status_code == 401
    # And the Origin check still guards other state-changing routes.
    assert anon.post("/api/ebay/deletion/extra", headers={**public, "Origin": "https://evil.example"}).status_code == 403
