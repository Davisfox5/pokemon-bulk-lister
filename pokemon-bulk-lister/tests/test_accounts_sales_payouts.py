"""Email verification and the operator rule, sales read back from eBay and
from CSV exports, and automatic seller payouts."""
import io
import json
from pathlib import Path

import pytest
import stripe as stripe_sdk
from flask import Flask

from conftest import verify_link
from lib import ebay_orders
from lib.ebay_oauth import CONSENT_SCOPES, SALES_SCOPE, USER_SCOPES, EbayUserAuth
from webapp import auth, catalog, marketplace, models, payouts, refunds, sales_sync
from webapp import db as bench_db
from tests.test_stripe_flow import FakeStripe


@pytest.fixture
def app(tmp_path, monkeypatch, mail_outbox):
    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    from webapp.library import library_bp

    bench = str(tmp_path / "bench.sqlite")
    bench_db.init_db(bench)
    monkeypatch.setattr(catalog, "BENCH_DB_PATH", bench)
    monkeypatch.setattr(sales_sync, "BENCH_DB_PATH", bench)
    monkeypatch.setattr(sales_sync, "_state", {"last_sync_at": None, "last_result": None, "last_error": None})
    monkeypatch.setenv("ADMIN_EMAILS", "op@example.com")
    monkeypatch.setattr(marketplace, "_stripe", lambda: None)

    templates = Path(__file__).resolve().parent.parent / "webapp" / "templates"
    flask_app = Flask(__name__, template_folder=str(templates))
    flask_app.secret_key = "test"
    for bp in (auth.auth_bp, catalog.catalog_bp, marketplace.market_bp, library_bp,
               sales_sync.sales_bp, payouts.payouts_bp, refunds.refunds_bp):
        flask_app.register_blueprint(bp)
    flask_app.outbox = mail_outbox
    flask_app.bench_path = bench
    return flask_app


def _register(app, email):
    c = app.test_client()
    r = c.post("/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return c


def _user(app, email):
    c = _register(app, email)
    assert "verify=done" in c.get(verify_link(app.outbox, email)).headers["Location"]
    return c


def _uid(email):
    s = models.get_session()
    try:
        return s.query(models.User).filter_by(email=email).one().id
    finally:
        s.close()


def _add(c, **kw):
    body = {"name": "Dragapult ex", "set_code": "sv6", "card_number": "130"}
    body.update(kw)
    r = c.post("/api/inventory/add", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _events(c, kind):
    return c.get(f"/api/library/events?kind={kind}").get_json()


# ====================================================================== email verification

def test_registering_mails_a_link_and_unverified_accounts_cannot_trade(app):
    c = _register(app, "new@example.com")
    assert app.outbox[-1]["to"] == "new@example.com"
    account = c.get("/api/account").get_json()
    assert (account["verified"], account["is_operator"]) == (False, False)

    # A library is fine without verifying...
    item = _add(c)
    assert c.get("/api/inventory").get_json()["items"]
    # ...selling, buying and payouts are not.
    for r in (c.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 5}),
              c.post("/api/inventory/list-for-sale", json={"item_ids": [item["id"]]}),
              c.post("/api/market/listings/1/buy"),
              c.post("/api/payouts/connect")):
        assert r.status_code == 403 and r.get_json()["code"] == "email_unverified"

    assert "verify=done" in c.get(verify_link(app.outbox, "new@example.com")).headers["Location"]
    assert c.get("/api/account").get_json()["verified"] is True
    assert c.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 5}).status_code == 200


def test_verification_links_are_single_use_expire_and_are_not_stored_in_clear(app):
    c = _register(app, "a@example.com")
    link = verify_link(app.outbox, "a@example.com")
    token = link.split("token=")[1]
    s = models.get_session()
    try:
        row = s.query(models.EmailVerification).one()
        assert row.token_hash and token not in row.token_hash and len(row.token_hash) == 64
        row.sent_at = models.utcnow() - auth.timedelta(hours=25)
        s.commit()
    finally:
        s.close()
    assert "verify=expired" in c.get(link).headers["Location"]
    assert "verify=invalid" in c.get("/verify-email?token=nope").headers["Location"]
    assert "verify=invalid" in c.get("/verify-email").headers["Location"]

    # A new link replaces the old one, and works once.
    s = models.get_session()
    try:
        s.query(models.EmailVerification).one().sent_at = models.utcnow() - auth.timedelta(minutes=5)
        s.commit()
    finally:
        s.close()
    assert c.post("/api/account/resend-verification").get_json()["sent"] is True
    assert c.post("/api/account/resend-verification").status_code == 429     # not a mail cannon
    new_link = verify_link(app.outbox, "a@example.com")
    assert new_link != link
    assert "verify=invalid" in c.get(link).headers["Location"]
    assert "verify=done" in c.get(new_link).headers["Location"]
    assert "verify=invalid" in c.get(new_link).headers["Location"]
    assert c.post("/api/account/resend-verification").get_json() == {"verified": True}


def test_a_link_only_confirms_the_account_it_was_sent_for_and_only_when_logged_in(app):
    # Someone registers the admin address with their own password, hoping
    # the real owner will click the link that lands in their inbox.
    squatter = _register(app, "op@example.com")
    link = verify_link(app.outbox, "op@example.com")

    owner_browser = app.test_client()                       # logged out
    r = owner_browser.get(link)
    assert r.headers["Location"].startswith("/login?next=")
    assert squatter.get("/api/account").get_json()["verified"] is False

    other = _user(app, "other@example.com")                  # logged in as someone else
    assert "verify=wrong_account" in other.get(link).headers["Location"]
    assert squatter.get("/api/account").get_json()["is_operator"] is False

    # Logging in through the redirect brings the link back and it works.
    fresh = app.test_client()
    login = fresh.post("/login?next=" + link.replace("?", "%3F").replace("=", "%3D"),
                       data={"email": "op@example.com", "password": "password123"})
    assert login.headers["Location"] == link
    assert "verify=done" in fresh.get(link).headers["Location"]


def test_operator_is_a_verified_admin_address_and_nothing_else(app, monkeypatch):
    first = _user(app, "first@example.com")          # registering first earns nothing
    assert first.get("/api/account").get_json()["is_operator"] is False
    assert first.get("/api/admin/balances").status_code == 403

    squatter = _register(app, "op@example.com")      # the admin address, mailbox not proven
    account = squatter.get("/api/account").get_json()
    assert (account["is_operator"], account["admin_pending"]) == (False, True)
    assert squatter.get("/api/admin/balances").status_code == 403

    squatter.get(verify_link(app.outbox, "op@example.com"))
    assert squatter.get("/api/account").get_json()["is_operator"] is True
    assert squatter.get("/api/admin/balances").status_code == 200


def test_the_default_admin_address(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert auth.admin_emails() == {"davisfox5@gmail.com"}
    monkeypatch.setenv("ADMIN_EMAILS", " A@x.com, b@x.com ")
    assert auth.admin_emails() == {"a@x.com", "b@x.com"}


def test_mailer_prints_to_the_console_without_smtp_and_uses_smtp_with_it(monkeypatch, capsys):
    from lib import mailer

    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert mailer.send("a@x.com", "Hi", "the link") == "console"
    assert "the link" in capsys.readouterr().err

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): sent.update(host=host, port=port)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): sent["tls"] = True
        def login(self, user, password): sent["login"] = (user, password)
        def send_message(self, msg): sent.update(to=msg["To"], frm=msg["From"], body=msg.get_content())

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.x.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                 "SMTP_FROM": "Cards <no-reply@x.com>"}.items():
        monkeypatch.setenv(k, v)
    assert mailer.send("a@x.com", "Hi", "the link") == "smtp"
    assert (sent["host"], sent["port"], sent["tls"], sent["login"]) == ("smtp.x.com", 587, True, ("u", "p"))
    assert sent["to"] == "a@x.com" and "the link" in sent["body"]


# ====================================================================== eBay order parsing

ORDER = {
    "orderId": "12-34567-89012", "creationDate": "2026-11-01T08:25:43.511Z",
    "orderPaymentStatus": "PAID", "cancelStatus": {"cancelState": "NONE_REQUESTED"},
    "totalMarketplaceFee": {"value": "3.00", "currency": "USD"},
    "lineItems": [
        {"lineItemId": "111", "sku": "page01_r0c0", "title": "Dragapult ex", "quantity": 1,
         "legacyItemId": "4001", "lineItemCost": {"value": "20.00", "currency": "USD"}},
        {"lineItemId": "222", "sku": "page01_r0c1", "title": "Pikachu x2", "quantity": 2,
         "lineItemCost": {"value": "10.00", "currency": "USD"}},
    ],
}


def test_ebay_order_becomes_one_sale_line_per_item_with_fees_shared():
    a, b = ebay_orders.order_lines(ORDER)
    assert a["external_id"] == "12-34567-89012:111" and a["sku"] == "page01_r0c0"
    assert (a["quantity"], a["unit_price"], a["fees"]) == (1, 20.0, 2.0)
    assert (b["quantity"], b["unit_price"], b["fees"]) == (2, 5.0, 1.0)      # 10.00 is for both
    assert a["paid"] and not a["void"]
    assert a["sold_at"].isoformat() == "2026-11-01T08:25:43.511000"

    refunded = ebay_orders.order_lines({**ORDER, "orderPaymentStatus": "FULLY_REFUNDED"})[0]
    assert refunded["void"] and not refunded["paid"]
    cancelled = ebay_orders.order_lines({**ORDER, "cancelStatus": {"cancelState": "CANCELED"}})[0]
    assert cancelled["void"]
    for status in ("PENDING", "FAILED"):
        line = ebay_orders.order_lines({**ORDER, "orderPaymentStatus": status})[0]
        assert not line["paid"] and not line["void"]


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._payload


def _ebay_auth(tmp_path, scopes=None, monkeypatch=None):
    path = tmp_path / "tok.json"
    data = {"refresh_token": "rt"}
    if scopes is not None:
        data["scopes"] = scopes
    path.write_text(json.dumps(data))
    a = EbayUserAuth(client_id="cid", client_secret="sec", redirect_uri="Ru", token_path=str(path))
    a._access_token, a._access_expiry = "at", 9e12          # no network for the token
    return a


def test_fetch_sale_lines_pages_and_sends_the_documented_request(tmp_path):
    calls = []

    class Http:
        def get(self, url, headers=None, params=None, timeout=None):
            calls.append((url, headers, params))
            offset = params["offset"]
            orders = [{**ORDER, "orderId": f"o{offset + i}"} for i in range(2 if offset == 0 else 1)]
            return _Resp(200, {"orders": orders, "total": 3})

    lines = ebay_orders.fetch_sale_lines(_ebay_auth(tmp_path, CONSENT_SCOPES),
                                         since=models.datetime(2026, 10, 1), session=Http())
    assert len(lines) == 6 and len(calls) == 2
    url, headers, params = calls[0]
    assert url == "https://api.ebay.com/sell/fulfillment/v1/order"
    assert headers["Authorization"] == "Bearer at"
    assert params == {"filter": "creationdate:[2026-10-01T00:00:00.000Z..]", "limit": 200, "offset": 0}
    assert calls[1][2]["offset"] == 2


def test_a_connection_made_before_sales_read_back_says_to_reconnect(tmp_path):
    old = _ebay_auth(tmp_path)                               # token file with no scopes on it
    assert not old.has_scope(SALES_SCOPE)
    with pytest.raises(ebay_orders.EbaySalesScopeMissing):
        ebay_orders.fetch_sale_lines(old, session=object())


def test_old_tokens_keep_refreshing_with_only_the_scopes_they_were_granted(tmp_path, monkeypatch):
    sent = {}

    def fake_post(self, url, headers=None, data=None, timeout=None):
        sent.update(data)
        return _Resp(200, {"access_token": "new", "expires_in": 7200, "refresh_token": "rt2"})

    monkeypatch.setattr("requests.Session.post", fake_post)
    old = _ebay_auth(tmp_path)
    old._access_token = None
    old.access_token()
    assert sent["scope"] == " ".join(USER_SCOPES) and SALES_SCOPE not in sent["scope"]

    # A fresh consent asks for, and remembers, the sales scope.
    fresh = EbayUserAuth(client_id="cid", client_secret="sec", redirect_uri="Ru",
                         token_path=str(tmp_path / "new.json"))
    assert "sell.fulfillment.readonly" in fresh.build_consent_url()
    fresh.exchange_code("code")
    assert json.loads((tmp_path / "new.json").read_text())["scopes"] == CONSENT_SCOPES
    again = EbayUserAuth(client_id="cid", client_secret="sec", redirect_uri="Ru",
                         token_path=str(tmp_path / "new.json"))
    assert again.has_scope(SALES_SCOPE)


# ====================================================================== recording sales

def _scanned_card(app, client, stem="page01_r0c0", name="Dragapult ex", price=18.0):
    with bench_db.connect(app.bench_path) as conn:
        gid = bench_db.get_or_create_grid(conn, stem, f"input/grids/{stem}.jpg")
        cid = bench_db.insert_card_stub(conn, gid, f"output/crops/{stem}.jpg", 0, 0)
        bench_db.update_card(conn, cid, {"name": name, "final_price": price})
    assert client.post("/api/inventory/promote", json={"card_ids": [cid]}).get_json()["promoted"] == 1
    return cid, client.get("/api/inventory").get_json()["items"][0]


def _line(**kw):
    line = {"external_id": "o1:111", "sku": "page01_r0c0", "title": "Dragapult ex", "quantity": 1,
            "unit_price": 20.0, "fees": 2.6, "sold_at": models.datetime(2026, 11, 1, 8, 0),
            "paid": True, "void": False}
    line.update(kw)
    return line


def test_an_ebay_sale_leaves_the_library_and_lands_in_the_ledger_once(app):
    seller, buyer = _user(app, "op@example.com"), _user(app, "buyer@example.com")
    bench_id, item = _scanned_card(app, seller)
    seller.patch(f"/api/inventory/{item['id']}", json={"acquired_price": 6})
    lid = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 25}).get_json()["listing_id"]
    oid = buyer.post(f"/api/market/listings/{lid}/buy").get_json()["order_id"]   # unpaid order here too

    result = sales_sync.record_sale_lines([_line()], "ebay")
    assert (result["recorded"], result["cards_sold"], result["total"]) == (1, 1, 20.0)
    assert "page01_r0c0" in sales_sync.sold_stems()

    sold = _events(seller, "sold")["events"]
    assert [(e["channel"], e["total"], e["fees"], e["net"], e["gain"]) for e in sold] == [
        ("ebay", 20.0, 2.6, 17.4, 11.4)]
    assert sold[0]["occurred_at"].startswith("2026-11-01")
    assert seller.get("/api/inventory").get_json()["items"] == []
    # It cannot also be sold here: the listing is down and the waiting order cancelled.
    assert buyer.get("/api/market/listings").get_json()["listings"] == []
    assert buyer.get("/api/market/my").get_json()["purchases"][0]["status"] == "cancelled"
    assert seller.post(f"/api/market/orders/{oid}/mark-paid").status_code == 404
    with bench_db.connect(app.bench_path) as conn:
        assert bench_db.get_card(conn, bench_id)["ebay_listing_status"] == "sold"

    # The same order arriving again (every sync re-reads recent orders) changes nothing.
    again = sales_sync.record_sale_lines([_line()], "ebay")
    assert (again["recorded"], again["duplicates"]) == (0, 1)
    assert _events(seller, "sold")["totals"]["sold_cards"] == 1


def test_sales_that_cannot_be_tied_to_a_library_card_are_kept_for_a_person(app):
    op = _user(app, "op@example.com")
    with bench_db.connect(app.bench_path) as conn:      # scanned, never added to a library
        gid = bench_db.get_or_create_grid(conn, "p9", "input/grids/p9.jpg")
        bench_id = bench_db.insert_card_stub(conn, gid, "output/crops/p9_r0c0.jpg", 0, 0)
    result = sales_sync.record_sale_lines([
        _line(external_id="a", sku="p9_r0c0"),
        _line(external_id="b", sku="unknown_sku"),
        _line(external_id="c", sku=None),
        _line(external_id="d", void=True),
        _line(external_id="e", paid=False),
    ], "ebay")
    assert (result["recorded"], result["unmatched"], result["void"], result["not_paid_yet"]) == (0, 3, 1, 1)
    status = op.get("/api/sales/status").get_json()
    reasons = {u["external_id"]: u["detail"] for u in status["unmatched"]}
    assert "never added to a library" in reasons["a"]
    assert "no card with this SKU" in reasons["b"]
    assert "no SKU" in reasons["c"]

    # It did sell, so the Scan page stops offering it. An unknown SKU hides nothing.
    assert sales_sync.sold_stems() == {"p9_r0c0"}
    with bench_db.connect(app.bench_path) as conn:
        assert bench_db.get_card(conn, bench_id)["ebay_listing_status"] == "sold"

    # An unpaid line is not remembered, so it is recorded once it is paid.
    assert sales_sync.record_sale_lines([_line(external_id="e", sku="unknown_sku")], "ebay")["unmatched"] == 1
    sale_id = next(u["id"] for u in status["unmatched"] if u["external_id"] == "b")
    assert op.post(f"/api/sales/external/{sale_id}/dismiss").status_code == 200
    assert op.post(f"/api/sales/external/{sale_id}/dismiss").status_code == 404
    assert len(op.get("/api/sales/status").get_json()["unmatched"]) == 3


def test_reading_sales_back_is_for_the_operator_only(app):
    anon, user = app.test_client(), _user(app, "user@example.com")
    csv_row = b"SKU,Price\ncard-inv1,0.01\n"
    for client, code in ((anon, 401), (user, 403)):
        assert client.get("/api/sales/status").status_code == code
        assert client.post("/api/sales/sync/ebay").status_code == code
        assert client.post("/api/sales/external/1/dismiss").status_code == code
        r = client.post("/api/sales/import", content_type="multipart/form-data",
                        data={"file": (io.BytesIO(csv_row), "s.csv"), "source": "whatnot"})
        assert r.status_code == code


def test_a_sale_never_takes_a_card_from_someone_elses_library(app):
    op, alice = _user(app, "op@example.com"), _user(app, "alice@example.com")
    item = _add(alice)                                    # no photo: its SKU would be card-inv<id>
    lid = alice.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 9}).get_json()["listing_id"]

    # The operator imports a file naming Alice's card by id, and by a stem she holds.
    _scanned_card(app, alice, stem="page07_r0c0")
    csv_text = f"SKU,Price\ncard-inv{item['id']},0.01\npage07_r0c0,0.01\n".encode()
    r = op.post("/api/sales/import", content_type="multipart/form-data",
                data={"file": (io.BytesIO(csv_text), "s.csv"), "source": "whatnot"}).get_json()
    assert (r["recorded"], r["unmatched"]) == (0, 2)
    assert len(alice.get("/api/inventory").get_json()["items"]) == 2
    assert _events(alice, "sold")["events"] == []
    assert alice.get("/api/market/my").get_json()["active_listings"][0]["listing_id"] == lid

    # A user's own eBay account matches that user's cards only, and leaves the bench alone.
    result = sales_sync.record_sale_lines(
        [_line(sku="page07_r0c0")], "ebay", owners={_uid("op@example.com")}, operator_account=False)
    assert result["unmatched"] == 1
    result = sales_sync.record_sale_lines(
        [_line(external_id="hers", sku="page07_r0c0")], "ebay",
        owners={_uid("alice@example.com")}, operator_account=False)
    assert result["recorded"] == 1
    with bench_db.connect(app.bench_path) as conn:
        assert all(r["ebay_listing_status"] is None for r in conn.execute("SELECT * FROM cards"))


def test_a_sku_held_by_two_accounts_is_not_guessed(app):
    a, b = _user(app, "a@example.com"), _user(app, "b@example.com")
    cid, _ = _scanned_card(app, a)
    assert b.post("/api/inventory/promote", json={"card_ids": [cid]}).get_json()["promoted"] == 1
    owners = {_uid("a@example.com"), _uid("b@example.com")}
    assert sales_sync.record_sale_lines([_line()], "ebay", owners=owners)["unmatched"] == 1
    assert len(a.get("/api/inventory").get_json()["items"]) == 1
    assert len(b.get("/api/inventory").get_json()["items"]) == 1


def test_selling_more_than_the_library_holds_records_what_was_there(app):
    seller = _user(app, "op@example.com")
    _scanned_card(app, seller)
    result = sales_sync.record_sale_lines([_line(quantity=3)], "ebay")
    assert result["cards_sold"] == 1
    s = models.get_session()
    try:
        assert "sold 3 but the library held 1" in s.query(models.ExternalSale).one().detail
    finally:
        s.close()


def test_a_sale_refunded_later_puts_the_card_back(app):
    op = _user(app, "op@example.com")
    bench_id, item = _scanned_card(app, op)
    sales_sync.record_sale_lines([_line()], "ebay")
    assert op.get("/api/inventory").get_json()["items"] == []

    result = sales_sync.record_sale_lines([_line(void=True, paid=False)], "ebay")
    assert (result["reversed"], result["duplicates"]) == (1, 0)
    back = op.get("/api/inventory").get_json()["items"]
    assert [(i["id"], i["quantity"]) for i in back] == [(item["id"], 1)]
    kinds = [e["kind"] for e in op.get("/api/library/events").get_json()["events"]]
    assert kinds.count("sold") == 1 and kinds.count("added") == 2      # scanned in, then returned
    assert "page01_r0c0" not in sales_sync.sold_stems()                 # it can be listed again
    with bench_db.connect(app.bench_path) as conn:                      # and the bench flag is cleared
        assert bench_db.get_card(conn, bench_id)["ebay_listing_status"] is None
    assert sales_sync.record_sale_lines([_line(void=True)], "ebay")["duplicates"] == 1


def test_a_failed_payment_that_later_goes_through_is_recorded(app):
    op = _user(app, "op@example.com")
    _scanned_card(app, op)
    failed = ebay_orders.order_lines({**ORDER, "orderPaymentStatus": "FAILED"})[:1]
    assert sales_sync.record_sale_lines(failed, "ebay")["not_paid_yet"] == 1
    paid = ebay_orders.order_lines(ORDER)[:1]
    assert sales_sync.record_sale_lines(paid, "ebay")["recorded"] == 1


def test_sync_ebay_reports_when_ebay_is_not_connected(app, monkeypatch, tmp_path):
    monkeypatch.setenv("EBAY_USER_TOKEN_PATH", str(tmp_path / "none.json"))
    op = _user(app, "op@example.com")
    r = op.post("/api/sales/sync/ebay")
    assert r.status_code == 409 and "not connected" in r.get_json()["error"]
    assert op.post("/api/sales/sync/ebay", json={"days": "x"}).status_code == 400


def test_sync_ebay_end_to_end_with_a_connected_account(app, monkeypatch, tmp_path):
    from webapp import connections

    op = _user(app, "op@example.com")
    _scanned_card(app, op)
    monkeypatch.setattr(connections, "_ebay_auth", lambda: _ebay_auth(tmp_path, CONSENT_SCOPES))
    monkeypatch.setattr("requests.Session.get", lambda self, url, **kw: _Resp(
        200, {"orders": [ORDER], "total": 1}))
    r = op.post("/api/sales/sync/ebay", json={"days": 7}).get_json()
    assert (r["ok"], r["recorded"], r["unmatched"], r["accounts"]) == (True, 1, 1, 1)
    status = op.get("/api/sales/status").get_json()
    assert status["ebay"] == {"connected": True, "can_read_sales": True}
    assert status["last_sync_at"] and status["recent"][0]["sku"] == "page01_r0c0"

    # A sync already in progress is turned away, not queued behind it.
    assert sales_sync._sync_lock.acquire(blocking=False)
    try:
        r = op.post("/api/sales/sync/ebay")
        assert r.status_code == 409 and "already running" in r.get_json()["error"]
    finally:
        sales_sync._sync_lock.release()

    # An older connection: the sync says to reconnect instead of failing silently.
    monkeypatch.setattr(connections, "_ebay_auth", lambda: _ebay_auth(tmp_path))
    r = op.post("/api/sales/sync/ebay")
    assert r.status_code == 409 and "connect eBay again" in r.get_json()["problems"][0]


# ====================================================================== CSV import

WHATNOT_CSV = (
    "Order ID,Product Name,SKU,Quantity,Sold Price,Fees,Sold Date\n"
    'W-1,"Dragapult ex, holo",page01_r0c0,1,$21.50,$2.15,2026-11-02\n'
    "W-2,Mystery,not_ours,1,3.00,0.30,11/03/2026\n"
    "W-3,No sku,,1,4.00,,2026-11-03\n"
)


def test_sales_csv_is_read_by_header_name(app):
    lines, info = sales_sync.parse_sales_csv(WHATNOT_CSV)
    assert info["columns"]["sku"] == "SKU" and info["columns"]["price"] == "Sold Price"
    assert (info["rows"], info["skipped"]) == (3, 1)
    assert lines[0]["external_id"] == "W-1:page01_r0c0"
    assert (lines[0]["unit_price"], lines[0]["fees"], lines[0]["title"]) == (21.5, 2.15, "Dragapult ex, holo")
    assert lines[1]["sold_at"] == models.datetime(2026, 11, 3)

    ebay_style = "Custom label (SKU),Total,Qty\nabc,9.99,2\n"
    lines, info = sales_sync.parse_sales_csv(ebay_style)
    assert (lines[0]["sku"], lines[0]["quantity"], lines[0]["unit_price"]) == ("abc", 2, 9.99)

    with pytest.raises(ValueError, match="no SKU column"):
        sales_sync.parse_sales_csv("Title,Price\nx,1\n")
    with pytest.raises(ValueError, match="no price column"):
        sales_sync.parse_sales_csv("SKU,Title\nx,y\n")


def test_importing_a_sales_csv_previews_then_records_once(app):
    op = _user(app, "op@example.com")
    _scanned_card(app, op)

    def post(**form):
        data = {"file": (io.BytesIO(WHATNOT_CSV.encode()), "sales.csv"), **form}
        return op.post("/api/sales/import", data=data, content_type="multipart/form-data")

    assert post(source="etsy").status_code == 400
    assert post(source="tcgplayer").status_code == 400      # its export has no SKU to match on
    assert op.post("/api/sales/import", data={"source": "whatnot"}).status_code == 400

    preview = post(source="whatnot", dry_run="1").get_json()
    assert (preview["sales"], preview["already_imported"], preview["total"]) == (2, 0, 24.5)
    assert _events(op, "sold")["events"] == []              # a preview records nothing

    done = post(source="whatnot").get_json()
    assert (done["recorded"], done["unmatched"]) == (1, 1)
    sold = _events(op, "sold")["events"][0]
    assert (sold["channel"], sold["total"], sold["fees"]) == ("whatnot", 21.5, 2.15)

    again = post(source="whatnot").get_json()
    assert (again["recorded"], again["duplicates"]) == (0, 2)
    assert post(source="whatnot", dry_run="1").get_json()["already_imported"] == 2


def test_a_sale_keeps_its_identity_when_a_later_export_moves_its_row(app):
    week1 = "SKU,Sold Price,Sold Date\npage01_r0c0,5.00,2026-11-01\n"
    week2 = "SKU,Sold Price,Sold Date\npage01_r0c0,5.00,2026-11-08\npage01_r0c0,5.00,2026-11-01\n"
    first, _ = sales_sync.parse_sales_csv(week1)
    second, _ = sales_sync.parse_sales_csv(week2)
    assert first[0]["external_id"] == second[1]["external_id"]
    assert second[0]["external_id"] != second[1]["external_id"]
    # Two identical rows in one file are two sales, and stay two on re-import.
    twice, _ = sales_sync.parse_sales_csv("SKU,Price\nx,1\nx,1\n")
    assert [l["external_id"] for l in twice] == ["row:x:1.0:", "row:x:1.0:#2"]
    with pytest.raises(ValueError, match="more than"):
        sales_sync.parse_sales_csv("SKU,Price\n" + "x,1\n" * (sales_sync.MAX_CSV_ROWS + 1))


# ====================================================================== automatic payouts

class ConnectStripe(FakeStripe):
    """FakeStripe plus the Connect calls payouts.py makes."""

    def __init__(self):
        super().__init__()
        self.accounts: dict[str, dict] = {}
        self.transfers: list[dict] = []
        self.transfer_keys: dict[str, dict] = {}
        self.fail_transfer = False
        self.lose_transfer_response = False
        self.reversals: list[tuple] = []
        self.reversal_keys: dict[str, dict] = {}
        self.fail_reversal = False
        self.charge_state: dict[str, dict] = {}     # session id -> overrides on its charge
        self.disputes: dict[str, str] = {}          # charge id -> dispute status
        outer = self

        class Charge:
            @staticmethod
            def retrieve(cid):
                sid = cid[len("ch_pi_"):]
                session = outer.sessions[sid]
                data = {"id": cid, "amount": session["amount_total"], "amount_refunded": 0,
                        "refunded": False, "disputed": cid in outer.disputes,
                        "payment_intent": f"pi_{sid}",
                        "transfer_group": f"order-{session['metadata']['order_id']}"}
                data.update(outer.charge_state.get(sid, {}))
                return stripe_sdk.Charge.construct_from(data, "k")

        class Dispute:
            @staticmethod
            def list(charge=None, limit=1):
                status = outer.disputes.get(charge)
                return {"data": [{"id": "dp_1", "charge": charge, "status": status}] if status else []}

        self.Charge, self.Dispute = Charge, Dispute

        class Account:
            @staticmethod
            def create(idempotency_key=None, **kw):
                assert kw["type"] == "express" and kw["capabilities"] == {"transfers": {"requested": True}}
                aid = f"acct_{len(outer.accounts) + 1}"
                outer.accounts[aid] = {"id": aid, "email": kw["email"], "payouts_enabled": False,
                                       "details_submitted": False, "capabilities": {"transfers": "inactive"}}
                return stripe_sdk.Account.construct_from(dict(outer.accounts[aid]), "sk_test_x")

            @staticmethod
            def retrieve(aid):
                return stripe_sdk.Account.construct_from(json.loads(json.dumps(outer.accounts[aid])), "sk_test_x")

        class AccountLink:
            @staticmethod
            def create(**kw):
                assert kw["type"] == "account_onboarding" and kw["refresh_url"] and kw["return_url"]
                outer.last_link = kw
                return stripe_sdk.AccountLink.construct_from(
                    {"object": "account_link", "url": f"https://connect.stripe.test/{kw['account']}"}, "k")

        class PaymentIntent:
            @staticmethod
            def retrieve(pid):
                return stripe_sdk.PaymentIntent.construct_from({"id": pid, "latest_charge": f"ch_{pid}"}, "k")

        class Transfer:
            @staticmethod
            def list(transfer_group=None, limit=10):
                data = [stripe_sdk.Transfer.construct_from(dict(t), "k") for t in outer.transfers
                        if t["transfer_group"] == transfer_group]
                return {"data": data}

            @staticmethod
            def retrieve(tid):
                return stripe_sdk.Transfer.construct_from(
                    dict(next(x for x in outer.transfers if x["id"] == tid)), "k")

            @staticmethod
            def create_reversal(tid, amount=None, idempotency_key=None):
                if outer.fail_reversal:
                    raise RuntimeError("Insufficient funds in the connected account")
                if idempotency_key in outer.reversal_keys:
                    return outer.reversal_keys[idempotency_key]
                t = next(x for x in outer.transfers if x["id"] == tid)
                left = t["amount"] - t["amount_reversed"]
                if t["reversed"] or amount > left:
                    raise RuntimeError("transfer already reversed")
                t["amount_reversed"] += amount
                t["reversed"] = t["amount_reversed"] >= t["amount"]
                outer.reversals.append((tid, amount))
                outer.reversal_keys[idempotency_key] = {"id": f"trr_{len(outer.reversals)}"}
                return outer.reversal_keys[idempotency_key]

            @staticmethod
            def create(idempotency_key=None, **kw):
                if outer.fail_transfer:
                    raise stripe_sdk.InvalidRequestError(
                        "Your destination account needs to have the transfers capability", None)
                if idempotency_key in outer.transfer_keys:
                    return outer.transfer_keys[idempotency_key]
                kw["id"] = f"tr_{len(outer.transfers) + 1}"
                kw.update(reversed=False, amount_reversed=0)
                outer.transfers.append(kw)
                obj = stripe_sdk.Transfer.construct_from(dict(kw), "k")
                outer.transfer_keys[idempotency_key] = obj
                if outer.lose_transfer_response:       # Stripe did it; the answer never arrived
                    outer.lose_transfer_response = False
                    raise stripe_sdk.APIConnectionError("Request timed out")
                return obj

        self.Account, self.AccountLink = Account, AccountLink
        self.PaymentIntent, self.Transfer = PaymentIntent, Transfer

    def finish_onboarding(self, aid):
        self.accounts[aid].update(payouts_enabled=True, details_submitted=True,
                                  capabilities={"transfers": "active"})


@pytest.fixture
def shop(app, monkeypatch):
    fake = ConnectStripe()
    monkeypatch.setattr(marketplace, "_stripe", lambda: fake)
    operator, seller, buyer = (_user(app, f"{n}@example.com") for n in ("op", "seller", "buyer"))

    def sell(price=20):
        item = _add(seller, name=f"Card {price}", card_number=str(price))
        lid = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": price}).get_json()["listing_id"]
        body = buyer.post(f"/api/market/listings/{lid}/buy").get_json()
        sid = body["checkout_url"].rsplit("/", 1)[1]
        fake.pay(sid)
        assert "payment=paid" in buyer.get(f"/market/orders/{body['order_id']}/return").headers["Location"]
        return body["order_id"], sid

    def connect(done=True):
        url = seller.post("/api/payouts/connect").get_json()["url"]
        aid = url.rsplit("/", 1)[1]
        if done:
            fake.finish_onboarding(aid)
        return aid, seller.get("/payouts/connect/return").headers["Location"]

    return type("Shop", (), dict(app=app, fake=fake, operator=operator, seller=seller, buyer=buyer,
                                 sell=staticmethod(sell), connect=staticmethod(connect)))


def test_checkout_is_card_only_and_tagged_for_transfers(shop):
    captured = {}
    create = shop.fake.checkout.Session.create
    shop.fake.checkout.Session.create = staticmethod(lambda **kw: (captured.update(kw), create(**kw))[1])
    oid, _ = shop.sell()
    assert captured["payment_method_types"] == ["card"]
    assert captured["payment_intent_data"] == {"transfer_group": f"order-{oid}"}


def test_a_connected_seller_is_paid_automatically_when_a_sale_settles(shop):
    aid, where = shop.connect()
    assert where.endswith("payouts=active")
    assert shop.seller.get("/api/payouts/account").get_json()["state"] == "active"
    assert shop.fake.last_link["return_url"].endswith("/payouts/connect/return")

    oid, sid = shop.sell(20)
    assert len(shop.fake.transfers) == 1
    t = shop.fake.transfers[0]
    assert (t["amount"], t["currency"], t["destination"]) == (1800, "usd", aid)   # 20.00 less the 10% fee
    assert t["source_transaction"] == f"ch_pi_{sid}" and t["transfer_group"] == f"order-{oid}"

    mine = shop.seller.get("/api/market/my").get_json()
    assert (mine["card_proceeds"], mine["paid_out"], mine["balance"]) == (18.0, 18.0, 0.0)
    rows = shop.operator.get("/api/admin/balances").get_json()
    assert rows["sellers"][0]["auto_payouts"] is True and rows["payouts"][0]["method"] == "stripe"
    assert "tr_1" in rows["payouts"][0]["note"]

    # Settling again, or running payouts again, never pays twice.
    shop.buyer.get(f"/market/orders/{oid}/return")
    assert shop.operator.post("/api/admin/payouts/run").get_json()["orders"] == 0
    assert len(shop.fake.transfers) == 1


def test_sales_made_before_payout_setup_are_paid_when_setup_finishes(shop):
    shop.sell(20)
    shop.sell(30)
    assert shop.fake.transfers == []
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 45.0
    assert shop.seller.get("/api/payouts/account").get_json()["state"] == "none"

    _, where = shop.connect(done=False)                 # left onboarding half way
    assert where.endswith("payouts=incomplete") and shop.fake.transfers == []
    assert shop.seller.get("/api/payouts/account").get_json()["state"] == "incomplete"

    aid, where = shop.connect()                          # same account, now finished
    assert where.endswith("payouts=active") and len(shop.fake.accounts) == 1
    assert [t["amount"] for t in shop.fake.transfers] == [1800, 2700]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0


def test_money_already_paid_by_hand_is_not_sent_again(shop):
    shop.sell(20)
    shop.sell(30)
    sid = shop.operator.get("/api/admin/balances").get_json()["sellers"][0]["seller_id"]
    shop.operator.post("/api/admin/payouts", json={"seller_id": sid, "amount": 30, "method": "Venmo"})
    shop.connect()
    assert sum(t["amount"] for t in shop.fake.transfers) == 1500      # 45.00 owed, 30.00 already paid
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0


def test_a_failed_transfer_leaves_the_sale_intact_and_is_retried(shop):
    shop.connect()
    shop.fake.fail_transfer = True
    oid, _ = shop.sell(20)                               # the sale itself still completes
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 18.0
    assert len(shop.buyer.get("/api/inventory").get_json()["items"]) == 1

    assert shop.seller.post("/api/admin/payouts/run").status_code == 403
    shop.fake.fail_transfer = False
    r = shop.operator.post("/api/admin/payouts/run").get_json()
    assert (r["transferred"], r["orders"], r["errors"]) == (18.0, 1, [])
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0


def test_a_transfer_whose_answer_was_lost_is_found_not_repeated(shop):
    # Stripe makes the transfer but the reply times out. From here the app
    # cannot tell a failure from a success, so it must not pay again.
    shop.connect()
    shop.fake.lose_transfer_response = True
    oid, _ = shop.sell(20)
    assert len(shop.fake.transfers) == 1
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0      # spoken for, not owed
    # Even if looking the transfer up comes back empty, asking again uses the
    # same key and gets the same transfer back.
    real_list = shop.fake.Transfer.list
    shop.fake.Transfer.list = staticmethod(lambda **kw: {"data": []})
    r = shop.operator.post("/api/admin/payouts/run").get_json()
    shop.fake.Transfer.list = real_list
    assert r["errors"] == [] and len(shop.fake.transfers) == 1
    assert list(shop.fake.transfer_keys) == [f"transfer-order-{oid}-n0"]
    mine = shop.seller.get("/api/market/my").get_json()
    assert (mine["paid_out"], mine["balance"]) == (18.0, 0.0)


def test_a_transfer_sent_but_not_recorded_is_never_sent_again(shop, monkeypatch):
    # Stripe makes the transfer, then the app fails to save its id (database
    # locked, process killed). The claim made beforehand must hold.
    shop.connect()
    real_commit = models.Session.commit
    state = {"armed": False}

    def flaky_commit(self):
        if state["armed"] and any(isinstance(o, models.OrderTransfer) and o.transfer_id != "pending"
                                  for o in self.dirty):
            state["armed"] = False
            raise RuntimeError("database is locked")
        return real_commit(self)

    monkeypatch.setattr(models.Session, "commit", flaky_commit)
    state["armed"] = True
    oid, _ = shop.sell(20)
    assert len(shop.fake.transfers) == 1 and state["armed"] is False

    # The app still knows the money is spoken for: nothing shows as owed,
    # so the operator is not led to pay it by hand as well.
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0
    sid = shop.operator.get("/api/admin/balances").get_json()["sellers"][0]["seller_id"]
    assert shop.operator.post("/api/admin/payouts", json={"seller_id": sid, "amount": 16}).status_code == 400

    # The next run asks Stripe, finds the transfer, and records its id. No second transfer.
    r = shop.operator.post("/api/admin/payouts/run").get_json()
    assert (r["orders"], r["errors"]) == (0, [])
    assert len(shop.fake.transfers) == 1
    s = models.get_session()
    try:
        assert s.query(models.OrderTransfer).one().transfer_id == "tr_1"
    finally:
        s.close()


def test_a_claim_with_no_transfer_behind_it_is_released_and_paid(shop):
    shop.connect()
    oid, _ = shop.sell(20)
    s = models.get_session()
    try:  # as if the app died after claiming and before Stripe was called
        s.query(models.OrderTransfer).one().transfer_id = "pending"
        s.commit()
    finally:
        s.close()
    shop.fake.transfers.clear()
    shop.fake.transfer_keys.clear()
    r = shop.operator.post("/api/admin/payouts/run").get_json()
    assert (r["transferred"], r["orders"]) == (18.0, 1)
    assert len(shop.fake.transfers) == 1


def test_one_idempotency_key_per_order_whatever_the_amount(shop):
    keys = []
    create = shop.fake.Transfer.create
    shop.fake.Transfer.create = staticmethod(
        lambda idempotency_key=None, **kw: (keys.append(idempotency_key), create(idempotency_key=idempotency_key, **kw))[1])
    shop.connect()
    oid, _ = shop.sell(20)
    assert keys == [f"transfer-order-{oid}-n0"]


def test_recording_the_same_payout_twice_cannot_overdraw(shop):
    shop.sell(20)
    sid = shop.operator.get("/api/admin/balances").get_json()["sellers"][0]["seller_id"]
    body = {"seller_id": sid, "amount": 18}
    assert shop.operator.post("/api/admin/payouts", json=body).status_code == 200
    assert shop.operator.post("/api/admin/payouts", json=body).status_code == 400
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0


def test_payout_setup_needs_stripe(app):
    seller = _user(app, "seller@example.com")
    assert seller.post("/api/payouts/connect").status_code == 409
    assert seller.get("/api/payouts/account").get_json()["state"] == "off"


# ====================================================================== refunds and disputes after a sale settled

def _status(shop, oid):
    session = models.get_session()
    try:
        return session.get(models.Order, oid).status
    finally:
        session.close()


def test_a_full_refund_in_stripe_takes_the_sellers_share_back(shop):
    aid, _ = shop.connect()
    oid, sid = shop.sell(20)
    assert shop.seller.get("/api/market/my").get_json()["paid_out"] == 18.0

    shop.fake.charge_state[sid] = {"refunded": True, "amount_refunded": 2000}
    assert shop.seller.post("/api/admin/refunds/check").status_code == 403
    r = shop.operator.post("/api/admin/refunds/check").get_json()
    assert r["checked"] == 1 and "refunded in Stripe" in r["changes"][0] and r["errors"] == []
    assert shop.fake.reversals == [("tr_1", 1800)]
    assert _status(shop, oid) == "refunded"

    # Nobody is owed or owes anything, and the sale no longer counts.
    mine = shop.seller.get("/api/market/my").get_json()
    assert (mine["card_proceeds"], mine["paid_out"], mine["balance"]) == (0, 0, 0)
    sold = _events(shop.seller, "sold")
    assert sold["totals"]["sold_cards"] == 0 and sold["events"][0]["refunded"] is True
    assert _events(shop.buyer, "purchased")["totals"]["purchased_spend"] == 0

    # Checking again changes nothing and reverses nothing more.
    assert shop.operator.post("/api/admin/refunds/check").get_json()["checked"] == 0
    assert len(shop.fake.reversals) == 1

    # The card is still with the buyer; the operator hands it back.
    attention = shop.operator.get("/api/admin/balances").get_json()["attention"]
    assert [(a["order_id"], a["status"], a["card_with_buyer"]) for a in attention] == [(oid, "refunded", True)]
    assert shop.seller.post(f"/api/admin/orders/{oid}/return-card").status_code == 403
    assert shop.operator.post(f"/api/admin/orders/{oid}/return-card").status_code == 200
    assert shop.buyer.get("/api/inventory").get_json()["items"] == []
    assert len(shop.seller.get("/api/inventory").get_json()["items"]) == 1
    assert shop.operator.post(f"/api/admin/orders/{oid}/return-card").status_code == 409
    assert shop.operator.get("/api/admin/balances").get_json()["attention"][0]["card_with_buyer"] is False


def test_a_partial_refund_recomputes_the_split_and_reverses_the_difference(shop):
    shop.connect()
    oid, sid = shop.sell(20)                                # seller got 18.00
    shop.fake.charge_state[sid] = {"amount_refunded": 500}  # buyer ends up paying 15.00
    r = shop.operator.post("/api/admin/refunds/check").get_json()
    assert "seller share is now $13.50" in r["changes"][0]
    assert shop.fake.reversals == [("tr_1", 450)]           # 18.00 - 13.50
    mine = shop.seller.get("/api/market/my").get_json()
    assert (mine["card_proceeds"], mine["paid_out"], mine["balance"]) == (13.5, 13.5, 0.0)
    assert _status(shop, oid) == "paid"
    # Same refund seen again: nothing more happens. A further refund is handled on top.
    assert shop.operator.post("/api/admin/refunds/check").get_json()["changes"] == []
    shop.fake.charge_state[sid] = {"amount_refunded": 1000}
    shop.operator.post("/api/admin/refunds/check")
    assert shop.fake.reversals[-1] == ("tr_1", 450) and len(shop.fake.reversals) == 2
    assert shop.seller.get("/api/market/my").get_json()["card_proceeds"] == 9.0


def test_a_dispute_holds_the_money_and_a_win_pays_the_seller_again(shop):
    shop.connect()
    oid, sid = shop.sell(20)
    cid = f"ch_pi_{sid}"
    shop.fake.disputes[cid] = "needs_response"
    r = shop.operator.post("/api/admin/refunds/check").get_json()
    assert "disputed by the buyer" in r["changes"][0]
    assert _status(shop, oid) == "disputed" and shop.fake.reversals == [("tr_1", 1800)]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0
    assert shop.operator.post("/api/admin/payouts/run").get_json()["orders"] == 0   # not while it is open

    shop.fake.disputes[cid] = "won"
    r = shop.operator.post("/api/admin/refunds/check").get_json()
    assert "in your favour" in r["changes"][0] and _status(shop, oid) == "paid"
    assert [t["amount"] for t in shop.fake.transfers] == [1800, 1800]              # paid a second time
    assert shop.fake.transfers[1]["reversed"] is False
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0


def test_a_lost_dispute_ends_as_a_refund(shop):
    shop.connect()
    oid, sid = shop.sell(20)
    shop.fake.disputes[f"ch_pi_{sid}"] = "lost"
    shop.operator.post("/api/admin/refunds/check")
    assert _status(shop, oid) == "refunded" and shop.fake.reversals == [("tr_1", 1800)]


def test_a_reversal_stripe_refuses_shows_as_money_the_seller_owes(shop):
    shop.connect()
    oid, sid = shop.sell(20)
    shop.fake.fail_reversal = True                         # the seller already withdrew it
    shop.fake.charge_state[sid] = {"refunded": True, "amount_refunded": 2000}
    r = shop.operator.post("/api/admin/refunds/check").get_json()
    assert "reversal refused" in r["changes"][0]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == -18.0
    data = shop.operator.get("/api/admin/balances").get_json()
    assert data["sellers"][0]["balance"] == -18.0 and data["attention"][0]["seller_still_holds"] == 18.0


def test_a_refused_reversal_is_tried_again_on_the_next_check(shop):
    shop.connect()
    oid, sid = shop.sell(20)
    shop.fake.fail_reversal = True
    shop.fake.charge_state[sid] = {"refunded": True, "amount_refunded": 2000}
    shop.operator.post("/api/admin/refunds/check")
    assert shop.fake.reversals == [] and _status(shop, oid) == "refunded"

    shop.fake.fail_reversal = False                        # the seller's Stripe balance was topped up
    assert shop.operator.post("/api/admin/refunds/check").get_json()["checked"] == 1
    assert shop.fake.reversals == [("tr_1", 1800)]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0
    assert shop.operator.get("/api/admin/balances").get_json()["attention"][0]["seller_still_holds"] == 0
    assert shop.operator.post("/api/admin/refunds/check").get_json()["checked"] == 0   # finished


def test_a_partial_refund_whose_reversal_failed_is_finished_later(shop):
    shop.connect()
    oid, sid = shop.sell(20)
    shop.fake.fail_reversal = True
    shop.fake.charge_state[sid] = {"amount_refunded": 500}
    assert "reversal refused" in shop.operator.post("/api/admin/refunds/check").get_json()["changes"][0]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == -4.5    # holds 18.00, earned 13.50

    shop.fake.fail_reversal = False
    shop.operator.post("/api/admin/refunds/check")
    assert shop.fake.reversals == [("tr_1", 450)]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0.0
    shop.operator.post("/api/admin/refunds/check")
    assert len(shop.fake.reversals) == 1


def test_a_reversal_stripe_made_but_the_app_did_not_record_is_not_repeated(shop, monkeypatch):
    shop.connect()
    oid, sid = shop.sell(20)
    shop.fake.charge_state[sid] = {"amount_refunded": 500}
    real = shop.fake.Transfer.create_reversal

    def lost(tid, **kw):
        real(tid, **kw)
        raise stripe_sdk.APIConnectionError("Request timed out")

    shop.fake.Transfer.create_reversal = staticmethod(lost)
    shop.operator.post("/api/admin/refunds/check")
    shop.fake.Transfer.create_reversal = real
    shop.operator.post("/api/admin/refunds/check")
    assert shop.fake.reversals == [("tr_1", 450)]                             # once
    mine = shop.seller.get("/api/market/my").get_json()
    assert (mine["paid_out"], mine["balance"]) == (13.5, 0.0)


def test_a_refund_that_lands_while_a_payout_is_unfinished_still_takes_the_money_back(shop):
    shop.connect()
    shop.fake.lose_transfer_response = True                # transfer made, claim left pending
    oid, sid = shop.sell(20)
    shop.fake.charge_state[sid] = {"refunded": True, "amount_refunded": 2000}
    shop.operator.post("/api/admin/refunds/check")
    assert shop.fake.reversals == [("tr_1", 1800)]
    assert shop.seller.get("/api/market/my").get_json()["balance"] == 0


def test_a_refunded_card_goes_back_once_even_when_the_seller_kept_other_copies(shop):
    item = _add(shop.seller, name="Twin", card_number="7", quantity=2)
    assert shop.seller.patch(f"/api/inventory/{item['id']}", json={"acquired_price": 3.0}).status_code == 200
    lid = shop.seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 20}).get_json()["listing_id"]
    body = shop.buyer.post(f"/api/market/listings/{lid}/buy").get_json()
    sid = body["checkout_url"].rsplit("/", 1)[1]
    shop.fake.pay(sid)
    shop.buyer.get(f"/market/orders/{body['order_id']}/return")
    oid = body["order_id"]
    shop.fake.charge_state[sid] = {"refunded": True, "amount_refunded": 2000}
    shop.operator.post("/api/admin/refunds/check")
    assert shop.operator.get("/api/admin/balances").get_json()["attention"][0]["card_with_buyer"] is True

    assert shop.operator.post(f"/api/admin/orders/{oid}/return-card").status_code == 200
    assert shop.buyer.get("/api/inventory").get_json()["items"] == []
    mine = shop.seller.get("/api/inventory").get_json()["items"]
    assert sum(i["quantity"] for i in mine) == 2
    assert {i.get("acquired_price") for i in mine} == {3.0}                   # what the seller paid, not $20
    # A second click must not take another of the seller's own copies' worth from anyone.
    assert shop.operator.post(f"/api/admin/orders/{oid}/return-card").status_code == 409
    assert sum(i["quantity"] for i in shop.seller.get("/api/inventory").get_json()["items"]) == 2


def test_refund_and_dispute_webhooks_do_the_same_as_the_manual_check(shop, monkeypatch):
    import hashlib, hmac, time

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")

    def send(event):
        payload = json.dumps(event).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_x", f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        return shop.app.test_client().post(
            "/api/market/stripe-webhook", data=payload,
            headers={"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"})

    shop.connect()
    oid, sid = shop.sell(20)
    cid = f"ch_pi_{sid}"
    shop.fake.disputes[cid] = "needs_response"
    dispute = {"id": "dp_1", "object": "dispute", "charge": cid, "status": "needs_response"}
    assert send({"id": "e1", "object": "event", "type": "charge.dispute.created",
                 "data": {"object": dispute}}).status_code == 200
    assert _status(shop, oid) == "disputed"

    shop.fake.disputes[cid] = "won"
    send({"id": "e2", "object": "event", "type": "charge.dispute.closed",
          "data": {"object": {**dispute, "status": "won"}}})
    assert _status(shop, oid) == "paid" and len(shop.fake.transfers) == 2

    # The first event delivered again, late: the dispute is over, so nothing moves.
    send({"id": "e1", "object": "event", "type": "charge.dispute.created", "data": {"object": dispute}})
    assert _status(shop, oid) == "paid" and len(shop.fake.reversals) == 1

    # Another charge on the same Stripe account that happens to carry this
    # order's transfer_group cannot refund it.
    foreign = {"id": "ch_someone_else", "object": "charge", "amount": 2000, "amount_refunded": 2000,
               "refunded": True, "disputed": False, "transfer_group": f"order-{oid}", "payment_intent": "pi_x"}
    assert send({"id": "e9", "object": "event", "type": "charge.refunded",
                 "data": {"object": foreign}}).status_code == 200
    assert _status(shop, oid) == "paid"

    # A stale event claiming a refund Stripe does not show is not believed.
    stale = {**foreign, "id": cid, "payment_intent": f"pi_{sid}"}
    send({"id": "e10", "object": "event", "type": "charge.refunded", "data": {"object": stale}})
    assert _status(shop, oid) == "paid"

    # If handling fails, Stripe is told so and will deliver the event again.
    from webapp import refunds
    with monkeypatch.context() as m:
        m.setattr(refunds, "handle_event", lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked")))
        assert send({"id": "e11", "object": "event", "type": "charge.refunded",
                     "data": {"object": stale}}).status_code == 500

    shop.fake.charge_state[sid] = {"refunded": True, "amount_refunded": 2000}

    charge = {"id": cid, "object": "charge", "amount": 2000, "amount_refunded": 2000, "refunded": True,
              "disputed": False, "transfer_group": f"order-{oid}", "payment_intent": f"pi_{sid}"}
    send({"id": "e3", "object": "event", "type": "charge.refunded", "data": {"object": charge}})
    send({"id": "e3", "object": "event", "type": "charge.refunded", "data": {"object": charge}})   # redelivered
    assert _status(shop, oid) == "refunded" and len(shop.fake.reversals) == 2

    # A charge this app did not make is ignored.
    stranger = {**charge, "transfer_group": None}
    assert send({"id": "e4", "object": "event", "type": "charge.refunded",
                 "data": {"object": stranger}}).status_code == 200


# ====================================================================== forgotten password

def test_a_cookie_without_a_password_stamp_is_not_a_login(app):
    c = _user(app, "a@example.com")
    with c.session_transaction() as sess:
        sess.pop("pw")
    assert c.get("/api/account").status_code == 401


def test_login_only_redirects_to_a_path_on_this_site(app):
    _user(app, "a@example.com")
    creds = {"email": "a@example.com", "password": "password123"}
    for bad in ("//evil.example", "/\\evil.example", "https://evil.example"):
        r = app.test_client().post("/login", query_string={"next": bad}, data=creds)
        assert r.headers["Location"].endswith("/portfolio"), bad
    r = app.test_client().post("/login", query_string={"next": "/decks"}, data=creds)
    assert r.headers["Location"].endswith("/decks")


def _reset_link(outbox, email):
    import re
    body = next(m["body"] for m in reversed(outbox) if m["to"] == email and "reset-password" in m["body"])
    return re.search(r"(/reset-password\?token=\S+)", body).group(1)


def test_forgot_password_resets_it_signs_other_browsers_out_and_confirms_the_address(app):
    old_browser = _register(app, "a@example.com")            # never confirmed their email
    assert old_browser.get("/api/account").status_code == 200

    anon = app.test_client()
    assert anon.get("/forgot-password").status_code == 200
    r = anon.post("/forgot-password", data={"email": "a@example.com"})
    assert r.status_code == 200 and b"on its way" in r.data
    link = _reset_link(app.outbox, "a@example.com")
    token = link.split("token=")[1]
    assert anon.get(link).status_code == 200

    assert anon.post("/reset-password", data={"token": token, "password": "short"}).status_code == 400
    r = anon.post("/reset-password", data={"token": token, "password": "a-new-password"})
    assert r.headers["Location"] == "/portfolio?reset=done"
    account = anon.get("/api/account").get_json()
    assert account["email"] == "a@example.com" and account["verified"] is True   # the link proved the mailbox

    # The browser that was logged in with the old password is out.
    assert old_browser.get("/api/account").status_code == 401
    # The link cannot be used again; the old password no longer works; the new one does.
    assert anon.get(link).status_code == 400
    assert app.test_client().post("/reset-password", data={"token": token, "password": "another-one"}).status_code == 400
    fresh = app.test_client()
    assert fresh.post("/login", json={"email": "a@example.com", "password": "password123"}).status_code == 400
    assert fresh.post("/login", json={"email": "a@example.com", "password": "a-new-password"}).status_code == 200


def test_forgot_password_does_not_reveal_who_is_registered_and_links_expire(app):
    _register(app, "a@example.com")
    anon = app.test_client()
    known = anon.post("/forgot-password", json={"email": "a@example.com"}).get_json()
    unknown = anon.post("/forgot-password", json={"email": "nobody@example.com"}).get_json()
    assert known == unknown
    sent = [m for m in app.outbox if "reset-password" in m["body"]]
    assert [m["to"] for m in sent] == ["a@example.com"]

    # Asking again straight away does not send a second mail.
    anon.post("/forgot-password", json={"email": "a@example.com"})
    assert len([m for m in app.outbox if "reset-password" in m["body"]]) == 1

    link = _reset_link(app.outbox, "a@example.com")
    s = models.get_session()
    try:
        row = s.query(models.PasswordReset).one()
        assert link.split("token=")[1] not in row.token_hash        # only a hash is stored
        row.sent_at = models.utcnow() - auth.timedelta(minutes=61)
        s.commit()
    finally:
        s.close()
    assert anon.get(link).status_code == 400
    assert anon.post("/reset-password", data={"token": link.split("token=")[1],
                                               "password": "a-new-password"}).status_code == 400


def test_the_owner_of_an_address_can_take_back_an_account_someone_else_registered(app):
    squatter = _register(app, "op@example.com")              # their password, the operator's address
    owner = app.test_client()
    owner.post("/forgot-password", data={"email": "op@example.com"})
    link = _reset_link(app.outbox, "op@example.com")
    owner.post("/reset-password", data={"token": link.split("token=")[1], "password": "owners-password"})
    assert owner.get("/api/account").get_json()["is_operator"] is True
    assert squatter.get("/api/account").status_code == 401
    assert squatter.post("/login", json={"email": "op@example.com", "password": "password123"}).status_code == 400


# ====================================================================== eBay partial refunds

def test_a_partial_refund_at_ebay_lowers_the_recorded_price(app):
    op = _user(app, "op@example.com")
    _scanned_card(app, op)
    sales_sync.record_sale_lines([_line()], "ebay")                       # 20.00, fees 2.60
    partly = _line(refunded=5.0)
    result = sales_sync.record_sale_lines([partly], "ebay")
    assert (result["adjusted"], result["duplicates"], result["recorded"]) == (1, 0, 0)
    sold = _events(op, "sold")["events"][0]
    assert (sold["total"], sold["net"]) == (15.0, 12.4) and "partly refunded" in sold["note"]
    assert sales_sync.record_sale_lines([partly], "ebay")["duplicates"] == 1   # nothing more to do
    assert sales_sync.record_sale_lines([_line(refunded=8.0)], "ebay")["adjusted"] == 1
    assert _events(op, "sold")["events"][0]["total"] == 12.0
    assert op.get("/api/inventory").get_json()["items"] == []            # still sold


def test_a_sale_first_seen_already_partly_refunded_is_recorded_net(app):
    op = _user(app, "op@example.com")
    _scanned_card(app, op)
    order = {**ORDER, "orderPaymentStatus": "PARTIALLY_REFUNDED"}
    order["lineItems"] = [{**ORDER["lineItems"][0],
                           "refunds": [{"amount": {"value": "4.00"}}, {"amount": {"value": "1.00"}}]}]
    line = ebay_orders.order_lines(order)[0]
    assert (line["refunded"], line["paid"], line["void"]) == (5.0, True, False)
    sales_sync.record_sale_lines([line], "ebay")
    assert _events(op, "sold")["events"][0]["total"] == 15.0


def test_a_line_refunded_in_full_inside_a_partly_refunded_order_did_not_sell(app):
    op = _user(app, "op@example.com")
    _scanned_card(app, op)
    sales_sync.record_sale_lines(ebay_orders.order_lines(ORDER)[:1], "ebay")
    assert op.get("/api/inventory").get_json()["items"] == []

    order = {**ORDER, "orderPaymentStatus": "PARTIALLY_REFUNDED"}
    order["lineItems"] = [
        {**ORDER["lineItems"][0], "refunds": [{"amount": {"value": "23.50", "currency": "USD"}}]},   # price + shipping
        {**ORDER["lineItems"][1], "refunds": [{"amount": {"value": "9.00", "currency": "EUR"}}]}]    # not netted
    first, second = ebay_orders.order_lines(order)
    assert (first["void"], first["refunded"]) == (True, 20.0)
    assert (second["void"], second["refunded"]) == (False, 0.0)
    assert sales_sync.record_sale_lines([first], "ebay")["reversed"] == 1
    assert len(op.get("/api/inventory").get_json()["items"]) == 1          # the card is back


def test_a_different_price_on_a_re_read_is_not_a_refund(app):
    op = _user(app, "op@example.com")
    _scanned_card(app, op)
    sales_sync.record_sale_lines([_line()], "ebay")
    again = sales_sync.record_sale_lines([_line(unit_price=17.0)], "ebay")
    assert (again["adjusted"], again["duplicates"]) == (0, 1)
    assert _events(op, "sold")["events"][0]["total"] == 20.0
