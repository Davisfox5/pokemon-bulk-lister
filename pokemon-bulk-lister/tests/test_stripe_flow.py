"""Card payments: Checkout, settlement on return, webhook, expiry, payouts.

Stripe's HTTP API is replaced by an in-memory fake, but the objects it hands
back are built by the real SDK (`construct_from`) and webhooks go through the
SDK's real signature check, so field access and verification are exercised
as they run in production.
"""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path

import pytest
import stripe as stripe_sdk
from flask import Flask

from webapp import marketplace, models

SECRET = "whsec_test_secret"


class FakeStripe:
    """Stands in for the `stripe` module as marketplace.py uses it."""

    Webhook = stripe_sdk.Webhook

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.refunds: list[str] = []
        self.fail_create = False
        outer = self

        class Session:
            @staticmethod
            def create(**kw):
                if outer.fail_create:
                    raise RuntimeError("Invalid API Key provided")
                sid = f"cs_test_{len(outer.sessions) + 1}"
                outer.sessions[sid] = {
                    "id": sid, "object": "checkout.session", "status": "open",
                    "payment_status": "unpaid", "payment_intent": f"pi_{sid}",
                    "amount_total": kw["line_items"][0]["price_data"]["unit_amount"],
                    "metadata": kw["metadata"], "url": f"https://checkout.stripe.test/{sid}",
                    "success_url": kw["success_url"], "cancel_url": kw["cancel_url"],
                    "expires_at": kw["expires_at"],
                }
                return outer._obj(sid)

            @staticmethod
            def retrieve(sid):
                return outer._obj(sid)

            @staticmethod
            def expire(sid):
                if outer.sessions[sid]["status"] != "open":
                    raise RuntimeError("only open sessions can be expired")
                outer.sessions[sid]["status"] = "expired"
                return outer._obj(sid)

        class Refund:
            @staticmethod
            def create(payment_intent):
                outer.refunds.append(payment_intent)

        self.checkout = type("checkout", (), {"Session": Session})
        self.Refund = Refund

    def _obj(self, sid):
        return stripe_sdk.checkout.Session.construct_from(dict(self.sessions[sid]), "sk_test_x")

    def pay(self, sid):
        self.sessions[sid].update(status="complete", payment_status="paid")


@pytest.fixture
def env(tmp_path, monkeypatch):
    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    from webapp import catalog
    from webapp.auth import auth_bp
    from webapp.library import library_bp

    fake = FakeStripe()
    monkeypatch.setattr(marketplace, "_stripe", lambda: fake)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    templates = Path(__file__).resolve().parent.parent / "webapp" / "templates"
    app = Flask(__name__, template_folder=str(templates))
    app.secret_key = "test"
    for bp in (auth_bp, catalog.catalog_bp, marketplace.market_bp, library_bp):
        app.register_blueprint(bp)

    def user(email):
        c = app.test_client()
        assert c.post("/register", json={"email": email, "password": "password123"}).status_code == 200
        return c

    operator, seller, buyer = user("op@example.com"), user("seller@example.com"), user("buyer@example.com")
    item = seller.post("/api/inventory/add", json={"name": "Dragapult ex", "price": 4}).get_json()
    lid = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 20}).get_json()["listing_id"]
    return type("Env", (), dict(app=app, fake=fake, operator=operator, seller=seller, buyer=buyer,
                                 item=item, lid=lid, user=staticmethod(user)))


def _buy(env, client=None):
    r = (client or env.buyer).post(f"/api/market/listings/{env.lid}/buy")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    return body["order_id"], body["checkout_url"].rsplit("/", 1)[1]


def _sold(client):
    return client.get("/api/library/events?kind=sold").get_json()


def test_buy_starts_a_checkout_session_for_the_listing_price(env):
    oid, sid = _buy(env)
    session = env.fake.sessions[sid]
    assert session["amount_total"] == 2000
    assert session["metadata"] == {"order_id": str(oid)}
    assert session["success_url"].endswith(f"/market/orders/{oid}/return?session_id={{CHECKOUT_SESSION_ID}}")
    assert session["cancel_url"].endswith(f"/market/orders/{oid}/abandon")
    assert session["expires_at"] > time.time() + 30 * 60
    assert env.seller.get("/api/market/my").get_json()["payments"] == "stripe"


def test_returning_from_checkout_settles_the_order_once(env):
    oid, sid = _buy(env)
    # Coming back without having paid changes nothing.
    r = env.buyer.get(f"/market/orders/{oid}/return?session_id={sid}")
    assert r.status_code == 302 and "payment=pending" in r.headers["Location"]
    assert _sold(env.seller)["events"] == []

    env.fake.pay(sid)
    r = env.buyer.get(f"/market/orders/{oid}/return?session_id={sid}")
    assert "payment=paid" in r.headers["Location"]
    env.buyer.get(f"/market/orders/{oid}/return?session_id={sid}")   # refresh / back button

    sold = _sold(env.seller)
    assert sold["totals"]["sold_cards"] == 1
    assert (sold["events"][0]["total"], sold["events"][0]["fees"], sold["events"][0]["gain"]) == (20.0, 2.0, 14.0)
    assert len(env.buyer.get("/api/inventory").get_json()["items"]) == 1
    assert env.seller.get("/api/inventory").get_json()["items"] == []


def test_only_the_buyer_can_use_the_return_link(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    assert env.seller.get(f"/market/orders/{oid}/return").status_code == 404
    assert env.app.test_client().get(f"/market/orders/{oid}/return").status_code == 302  # to login
    assert _sold(env.seller)["events"] == []


def test_a_wrong_amount_is_not_settled(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    env.fake.sessions[sid]["amount_total"] = 100
    r = env.buyer.get(f"/market/orders/{oid}/return")
    assert "payment=problem" in r.headers["Location"]
    assert _sold(env.seller)["events"] == []


def test_backing_out_of_checkout_frees_the_card(env):
    oid, sid = _buy(env)
    r = env.buyer.get(f"/market/orders/{oid}/abandon")
    assert "payment=cancelled" in r.headers["Location"]
    assert env.fake.sessions[sid]["status"] == "expired"
    other = env.user("other@example.com")
    _buy(env, other)   # the listing is available again


def test_backing_out_after_paying_settles_instead(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    r = env.buyer.get(f"/market/orders/{oid}/abandon")
    assert "payment=paid" in r.headers["Location"]
    assert _sold(env.seller)["totals"]["sold_cards"] == 1


def test_seller_can_cancel_an_unpaid_card_order_but_not_a_paid_one(env):
    oid, sid = _buy(env)
    assert env.seller.post(f"/api/market/orders/{oid}/cancel").status_code == 200
    assert env.fake.sessions[sid]["status"] == "expired"

    oid2, sid2 = _buy(env)
    env.fake.pay(sid2)
    r = env.seller.post(f"/api/market/orders/{oid2}/cancel")
    assert r.status_code == 409 and r.get_json()["status"] == "paid"
    assert _sold(env.seller)["totals"]["sold_cards"] == 1


def test_an_abandoned_checkout_is_swept_when_the_next_buyer_arrives(env):
    oid, sid = _buy(env)
    s = models.get_session()
    try:
        s.get(models.Order, oid).created_at = models.utcnow() - timedelta(minutes=45)
        s.commit()
    finally:
        s.close()
    other = env.user("other@example.com")
    oid2, _ = _buy(env, other)
    assert oid2 != oid and env.fake.sessions[sid]["status"] == "expired"


def test_a_stale_order_that_was_actually_paid_is_settled_by_the_sweep(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)   # paid, but the buyer closed the tab before returning
    s = models.get_session()
    try:
        s.get(models.Order, oid).created_at = models.utcnow() - timedelta(minutes=45)
        s.commit()
    finally:
        s.close()
    env.seller.get("/api/market/my")
    assert _sold(env.seller)["totals"]["sold_cards"] == 1


def test_a_failed_checkout_start_does_not_leave_the_card_locked(env):
    env.fake.fail_create = True
    r = env.buyer.post(f"/api/market/listings/{env.lid}/buy")
    assert r.status_code == 502 and "could not start card payment" in r.get_json()["error"]
    env.fake.fail_create = False
    _buy(env)


def _signed(event: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": f"t={ts},v1={sig}", "Content-Type": "application/json"}


def _event(env, sid, kind="checkout.session.completed"):
    return {"id": "evt_1", "object": "event", "type": kind,
            "data": {"object": dict(env.fake.sessions[sid])}}


def test_signed_webhook_settles_and_is_safe_to_redeliver(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    payload, headers = _signed(_event(env, sid))
    hook = env.app.test_client()
    assert hook.post("/api/market/stripe-webhook", data=payload, headers=headers).status_code == 200
    assert hook.post("/api/market/stripe-webhook", data=payload, headers=headers).status_code == 200
    assert _sold(env.seller)["totals"]["sold_cards"] == 1


def test_webhook_rejects_unsigned_and_wrongly_signed_events(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    hook = env.app.test_client()
    body = json.dumps(_event(env, sid))
    assert hook.post("/api/market/stripe-webhook", data=body,
                     headers={"Content-Type": "application/json"}).status_code == 400
    payload, headers = _signed(_event(env, sid), secret="whsec_wrong")
    assert hook.post("/api/market/stripe-webhook", data=payload, headers=headers).status_code == 400
    assert _sold(env.seller)["events"] == []


def test_webhook_is_off_without_a_signing_secret(env, monkeypatch):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    payload, headers = _signed(_event(env, sid))
    assert env.app.test_client().post("/api/market/stripe-webhook", data=payload,
                                      headers=headers).status_code == 404
    assert _sold(env.seller)["events"] == []


def test_expired_webhook_releases_the_order(env):
    oid, sid = _buy(env)
    env.fake.sessions[sid]["status"] = "expired"
    payload, headers = _signed(_event(env, sid, "checkout.session.expired"))
    env.app.test_client().post("/api/market/stripe-webhook", data=payload, headers=headers)
    assert env.buyer.get("/api/market/my").get_json()["purchases"][0]["status"] == "cancelled"


def test_payment_for_a_card_that_is_gone_is_refunded(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    s = models.get_session()
    try:  # the state the guards prevent, forced directly
        s.get(models.InventoryItem, env.item["id"]).quantity = 0
        s.commit()
    finally:
        s.close()
    r = env.buyer.get(f"/market/orders/{oid}/return")
    assert "payment=problem" in r.headers["Location"]
    assert env.fake.refunds == [f"pi_{sid}"]
    assert _sold(env.seller)["events"] == []


def test_balances_and_payouts(env):
    oid, sid = _buy(env)
    env.fake.pay(sid)
    env.buyer.get(f"/market/orders/{oid}/return")

    mine = env.seller.get("/api/market/my").get_json()
    assert (mine["card_proceeds"], mine["paid_out"], mine["balance"], mine["fees_owed"]) == (18.0, 0, 18.0, 0)
    assert mine["is_operator"] is False
    assert env.operator.get("/api/market/my").get_json()["is_operator"] is True

    # Only the operator sees balances or records payouts.
    assert env.seller.get("/api/admin/balances").status_code == 403
    assert env.seller.post("/api/admin/payouts", json={"seller_id": 2, "amount": 5}).status_code == 403
    rows = env.operator.get("/api/admin/balances").get_json()["sellers"]
    assert [(r["email"], r["balance"]) for r in rows] == [("seller@example.com", 18.0)]
    sid_ = rows[0]["seller_id"]

    assert env.operator.post("/api/admin/payouts", json={"seller_id": sid_, "amount": 50}).status_code == 400
    assert env.operator.post("/api/admin/payouts", json={"seller_id": sid_, "amount": "inf"}).status_code == 400
    assert env.operator.post("/api/admin/payouts", json={"seller_id": 999, "amount": 1}).status_code == 400
    paid = env.operator.post("/api/admin/payouts",
                             json={"seller_id": sid_, "amount": 10, "method": "Venmo"}).get_json()
    assert (paid["paid_out"], paid["balance"]) == (10.0, 8.0)
    assert env.seller.get("/api/market/my").get_json()["balance"] == 8.0


def test_admin_emails_names_the_operator(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "seller@example.com")
    assert env.seller.get("/api/admin/balances").status_code == 200
    assert env.operator.get("/api/admin/balances").status_code == 403


def test_the_database_refuses_a_second_active_listing_for_one_card(env):
    from sqlalchemy.exc import IntegrityError

    s = models.get_session()
    try:
        s.add(models.MarketListing(seller_id=2, inventory_item_id=env.item["id"],
                                   catalog_card_id=env.item["catalog_card_id"], price=9))
        with pytest.raises(IntegrityError):
            s.commit()
    finally:
        s.rollback()
        s.close()


def test_duplicate_active_listings_from_an_older_database_are_cleaned_up(tmp_path):
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'old.sqlite'}"
    models.init_models(url)
    s = models.get_session()
    try:
        s.execute(text("DROP INDEX uq_active_listing"))
        user = models.User(email="a@example.com", password_hash="x")
        s.add(user)
        s.flush()
        cc = models.get_or_create_catalog_card(s, {"name": "Pikachu"})
        item = models.InventoryItem(user_id=user.id, catalog_card_id=cc.id)
        s.add(item)
        s.flush()
        for price in (5, 6, 7):
            s.add(models.MarketListing(seller_id=user.id, inventory_item_id=item.id,
                                       catalog_card_id=cc.id, price=price))
        s.commit()
    finally:
        s.close()
    models.init_models(url)   # relaunch
    s = models.get_session()
    try:
        active = s.query(models.MarketListing).filter_by(status="active").all()
        assert [l.price for l in active] == [5]
    finally:
        s.close()
