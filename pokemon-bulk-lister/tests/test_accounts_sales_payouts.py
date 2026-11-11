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
from webapp import auth, catalog, marketplace, models, payouts, sales_sync
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
               sales_sync.sales_bp, payouts.payouts_bp):
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
    anon = app.test_client()
    assert "verify=expired" in anon.get(link).headers["Location"]
    assert "verify=invalid" in anon.get("/verify-email?token=nope").headers["Location"]
    assert "verify=invalid" in anon.get("/verify-email").headers["Location"]

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
    assert "verify=invalid" in anon.get(link).headers["Location"]
    assert "verify=done" in anon.get(new_link).headers["Location"]
    assert "verify=invalid" in anon.get(new_link).headers["Location"]
    assert c.post("/api/account/resend-verification").get_json() == {"verified": True}


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
    pending = ebay_orders.order_lines({**ORDER, "orderPaymentStatus": "PENDING"})[0]
    assert not pending["paid"] and not pending["void"]


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
    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    bench_id, item = _scanned_card(app, seller)
    seller.patch(f"/api/inventory/{item['id']}", json={"acquired_price": 6})
    lid = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 25}).get_json()["listing_id"]
    oid = buyer.post(f"/api/market/listings/{lid}/buy").get_json()["order_id"]   # unpaid order here too

    result = sales_sync.record_sale_lines([_line()], "ebay")
    assert (result["recorded"], result["cards_sold"], result["total"]) == (1, 1, 20.0)

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
    c = app.test_client()
    with bench_db.connect(app.bench_path) as conn:      # scanned, never added to a library
        gid = bench_db.get_or_create_grid(conn, "p9", "input/grids/p9.jpg")
        bench_db.insert_card_stub(conn, gid, "output/crops/p9_r0c0.jpg", 0, 0)
    result = sales_sync.record_sale_lines([
        _line(external_id="a", sku="p9_r0c0"),
        _line(external_id="b", sku="unknown_sku"),
        _line(external_id="c", sku=None),
        _line(external_id="d", void=True),
        _line(external_id="e", paid=False),
    ], "ebay")
    assert (result["recorded"], result["unmatched"], result["void"], result["not_paid_yet"]) == (0, 3, 1, 1)
    status = c.get("/api/sales/status").get_json()
    reasons = {u["external_id"]: u["detail"] for u in status["unmatched"]}
    assert "never added to a library" in reasons["a"]
    assert "no card in any library" in reasons["b"]
    assert "no SKU" in reasons["c"]

    # An unpaid line is not remembered, so it is recorded once it is paid.
    assert sales_sync.record_sale_lines([_line(external_id="e", sku="unknown_sku")], "ebay")["unmatched"] == 1
    sale_id = status["unmatched"][0]["id"]
    assert c.post(f"/api/sales/external/{sale_id}/dismiss").status_code == 200
    assert c.post(f"/api/sales/external/{sale_id}/dismiss").status_code == 404
    assert len(c.get("/api/sales/status").get_json()["unmatched"]) == 3


def test_a_users_own_ebay_account_only_matches_that_users_cards(app):
    mine, theirs = _user(app, "me@example.com"), _user(app, "them@example.com")
    _scanned_card(app, theirs)
    s = models.get_session()
    try:
        my_id = s.query(models.User).filter_by(email="me@example.com").one().id
    finally:
        s.close()
    assert sales_sync.record_sale_lines([_line()], "ebay", owner_uid=my_id)["unmatched"] == 1
    assert len(theirs.get("/api/inventory").get_json()["items"]) == 1


def test_selling_more_than_the_library_holds_records_what_was_there(app):
    seller = _user(app, "seller@example.com")
    _scanned_card(app, seller)
    result = sales_sync.record_sale_lines([_line(quantity=3)], "ebay")
    assert result["cards_sold"] == 1
    s = models.get_session()
    try:
        assert "sold 3 but the library held 1" in s.query(models.ExternalSale).one().detail
    finally:
        s.close()


def test_sync_ebay_reports_when_ebay_is_not_connected(app, monkeypatch, tmp_path):
    monkeypatch.setenv("EBAY_USER_TOKEN_PATH", str(tmp_path / "none.json"))
    c = app.test_client()
    r = c.post("/api/sales/sync/ebay")
    assert r.status_code == 409 and "not connected" in r.get_json()["error"]
    assert c.post("/api/sales/sync/ebay", json={"days": "x"}).status_code == 400


def test_sync_ebay_end_to_end_with_a_connected_account(app, monkeypatch, tmp_path):
    from webapp import connections

    seller = _user(app, "seller@example.com")
    _scanned_card(app, seller)
    monkeypatch.setattr(connections, "_ebay_auth", lambda: _ebay_auth(tmp_path, CONSENT_SCOPES))
    monkeypatch.setattr("requests.Session.get", lambda self, url, **kw: _Resp(
        200, {"orders": [ORDER], "total": 1}))
    c = app.test_client()
    r = c.post("/api/sales/sync/ebay", json={"days": 7}).get_json()
    assert (r["ok"], r["recorded"], r["unmatched"], r["accounts"]) == (True, 1, 1, 1)
    status = c.get("/api/sales/status").get_json()
    assert status["ebay"] == {"connected": True, "can_read_sales": True}
    assert status["last_sync_at"] and status["recent"][0]["sku"] == "page01_r0c0"

    # An older connection: the sync says to reconnect instead of failing silently.
    monkeypatch.setattr(connections, "_ebay_auth", lambda: _ebay_auth(tmp_path))
    r = c.post("/api/sales/sync/ebay")
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
    seller = _user(app, "seller@example.com")
    _scanned_card(app, seller)
    c = app.test_client()

    def post(**form):
        data = {"file": (io.BytesIO(WHATNOT_CSV.encode()), "sales.csv"), **form}
        return c.post("/api/sales/import", data=data, content_type="multipart/form-data")

    assert post(source="etsy").status_code == 400
    assert c.post("/api/sales/import", data={"source": "whatnot"}).status_code == 400

    preview = post(source="whatnot", dry_run="1").get_json()
    assert (preview["sales"], preview["already_imported"], preview["total"]) == (2, 0, 24.5)
    assert _events(seller, "sold")["events"] == []          # a preview records nothing

    done = post(source="whatnot").get_json()
    assert (done["recorded"], done["unmatched"]) == (1, 1)
    sold = _events(seller, "sold")["events"][0]
    assert (sold["channel"], sold["total"], sold["fees"]) == ("whatnot", 21.5, 2.15)

    again = post(source="whatnot").get_json()
    assert (again["recorded"], again["duplicates"]) == (0, 2)
    assert post(source="whatnot", dry_run="1").get_json()["already_imported"] == 2


# ====================================================================== automatic payouts

class ConnectStripe(FakeStripe):
    """FakeStripe plus the Connect calls payouts.py makes."""

    def __init__(self):
        super().__init__()
        self.accounts: dict[str, dict] = {}
        self.transfers: list[dict] = []
        self.transfer_keys: dict[str, dict] = {}
        self.fail_transfer = False
        outer = self

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
            def create(idempotency_key=None, **kw):
                if outer.fail_transfer:
                    raise RuntimeError("Your destination account needs to have the transfers capability")
                if idempotency_key in outer.transfer_keys:
                    return outer.transfer_keys[idempotency_key]
                kw["id"] = f"tr_{len(outer.transfers) + 1}"
                outer.transfers.append(kw)
                obj = stripe_sdk.Transfer.construct_from(dict(kw), "k")
                outer.transfer_keys[idempotency_key] = obj
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
