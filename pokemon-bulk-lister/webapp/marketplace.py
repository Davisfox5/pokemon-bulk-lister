"""On-platform marketplace: list a card for sale in one click, browse, buy.

The platform records a fee on every sale (PLATFORM_FEE_PCT of the sale price,
default 10%). Two payment paths, same ledger:

  stripe    STRIPE_SECRET_KEY set: "buy" returns a Stripe Checkout URL. The
            buyer pays the platform's Stripe account. The order is settled
            three ways, whichever happens first, and settling is idempotent:
              1. the buyer's browser returns from Checkout and the server
                 asks Stripe whether the session was paid (works on a local
                 install with no public URL)
              2. the signed webhook, if STRIPE_WEBHOOK_SECRET is set
              3. a sweep of stale orders the next time the listing or the
                 seller's orders are looked at
            An unpaid Checkout session expires after STRIPE_CHECKOUT_MINUTES
            and frees the card. The seller's proceeds accrue as a balance the
            operator pays out (see Payout).
  offline   No Stripe key: the order is created pending_payment, buyer and
            seller settle directly (Venmo/cash/trade night), and the seller
            clicks "mark paid". The fee is recorded as owed to the platform.

On payment the card actually changes hands in the database: the inventory item
moves from seller to buyer (with the sale price as the buyer's cost basis), so
both portfolios stay truthful.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import timedelta

from flask import Blueprint, abort, jsonify, redirect, render_template, request
from sqlalchemy.exc import IntegrityError

from webapp.auth import current_user_id, login_required
from webapp.catalog import price_provenance
from webapp.models import (
    CatalogCard,
    InventoryItem,
    MarketListing,
    Order,
    Payout,
    User,
    get_session,
    log_event,
    utcnow,
)

market_bp = Blueprint("market", __name__)

PLATFORM_FEE_PCT = float(os.getenv("PLATFORM_FEE_PCT", "10"))
# How long a buyer has to finish paying before the card is freed. Stripe's
# own minimum for a Checkout session is 30 minutes.
STRIPE_CHECKOUT_MINUTES = max(31, int(os.getenv("STRIPE_CHECKOUT_MINUTES", "31")))


def _fee(amount: float) -> float:
    return round(amount * PLATFORM_FEE_PCT / 100.0, 2)


def _mask_email(email: str) -> str:
    local = email.split("@")[0]
    return (local[:1] + "***") if local else "***"


def _stripe():
    """Return a configured stripe module, or None when not set up."""
    if not os.getenv("STRIPE_SECRET_KEY"):
        return None
    try:
        import stripe
    except ImportError:
        print("[market] STRIPE_SECRET_KEY set but `pip install stripe` missing", file=sys.stderr)
        return None
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe


# ----------------------------------------------------------------------
# Selling
# ----------------------------------------------------------------------

MAX_PRICE = 1_000_000.0


def _parse_price(value) -> float | None:
    """A positive, finite dollar amount, or None when the value is not one.
    ("inf" and "nan" parse as floats; stored, they turn every listing
    response into invalid JSON.)"""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price):
        return None
    price = round(price, 2)
    return price if 0 < price <= MAX_PRICE else None


def _create_listing(s, item: InventoryItem, price: float) -> MarketListing:
    listing = MarketListing(
        seller_id=item.user_id,
        inventory_item_id=item.id,
        catalog_card_id=item.catalog_card_id,
        price=price,
    )
    s.add(listing)
    return listing


@market_bp.route("/api/inventory/<int:item_id>/list-for-sale", methods=["POST"])
@login_required
def list_for_sale(item_id: int):
    """One-click sell. Body: {"price": float?}, defaults to current market price."""
    body = request.get_json(silent=True) or {}
    uid = current_user_id()
    s = get_session()
    try:
        item = s.query(InventoryItem).filter_by(id=item_id, user_id=uid).one_or_none()
        if item is None or item.quantity <= 0:
            abort(404)
        existing = s.query(MarketListing).filter_by(
            inventory_item_id=item.id, status="active"
        ).one_or_none()
        if existing:
            return jsonify({"error": "already listed", "listing_id": existing.id}), 409

        if body.get("price") in (None, ""):
            price = _parse_price(item.catalog_card.final_price)
            if price is None:
                return jsonify({"error": "no market price known for this card, enter a price"}), 400
        else:
            price = _parse_price(body.get("price"))
            if price is None:
                return jsonify({"error": "price must be a number greater than 0"}), 400

        listing = _create_listing(s, item, price)
        try:
            s.commit()
        except IntegrityError:
            # Lost a race with another request listing the same card; the
            # unique index on active listings refused the second one.
            s.rollback()
            return jsonify({"error": "already listed"}), 409
        out = {"listing_id": listing.id, "price": listing.price,
               "fee_on_sale": _fee(listing.price), "fee_pct": PLATFORM_FEE_PCT}
    finally:
        s.close()
    return jsonify(out)


@market_bp.route("/api/inventory/list-for-sale", methods=["POST"])
@login_required
def bulk_list_for_sale():
    """List many cards at their current market price in one go.

    Body: {"item_ids": [...], "include_flagged": bool?, "dry_run": bool?}

    Cards with no price, cards already listed, and (unless include_flagged)
    cards whose price is flagged for review are skipped and counted. dry_run
    returns the same counts without listing anything, so the UI can say
    exactly what is about to happen before the user confirms.
    """
    body = request.get_json(silent=True) or {}
    ids = body.get("item_ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "item_ids (list) required"}), 400
    include_flagged = bool(body.get("include_flagged"))
    dry_run = bool(body.get("dry_run"))
    uid = current_user_id()
    s = get_session()
    try:
        items = (s.query(InventoryItem)
                 .filter(InventoryItem.user_id == uid, InventoryItem.quantity > 0,
                         InventoryItem.id.in_([int(i) for i in ids]))
                 .all())
        listed_ids = {row[0] for row in s.query(MarketListing.inventory_item_id)
                      .filter_by(seller_id=uid, status="active").all()}
        out = {"to_list": 0, "total": 0.0, "skipped_no_price": 0,
               "skipped_already_listed": 0, "skipped_flagged": 0}
        for item in items:
            cc = item.catalog_card
            price = _parse_price(cc.final_price)
            if item.id in listed_ids:
                out["skipped_already_listed"] += 1
            elif price is None:
                out["skipped_no_price"] += 1
            elif price_provenance(cc)["flagged"] and not include_flagged:
                out["skipped_flagged"] += 1
            else:
                if not dry_run:
                    try:
                        with s.begin_nested():
                            _create_listing(s, item, price)
                    except IntegrityError:
                        out["skipped_already_listed"] += 1
                        continue
                out["to_list"] += 1
                out["total"] += price
        if not dry_run:
            s.commit()
        out["total"] = round(out["total"], 2)
        out["fee_on_sale"] = _fee(out["total"])
        out["fee_pct"] = PLATFORM_FEE_PCT
        out["dry_run"] = dry_run
        out["listed"] = 0 if dry_run else out["to_list"]
    finally:
        s.close()
    return jsonify(out)


@market_bp.route("/api/market/listings/<int:listing_id>", methods=["PATCH"])
@login_required
def reprice_listing(listing_id: int):
    """Body: {"price": float}. Change the asking price of your active listing."""
    price = _parse_price((request.get_json(silent=True) or {}).get("price"))
    if price is None:
        return jsonify({"error": "price must be a number greater than 0"}), 400
    s = get_session()
    try:
        listing = s.query(MarketListing).filter_by(
            id=listing_id, seller_id=current_user_id(), status="active"
        ).one_or_none()
        if listing is None:
            abort(404)
        if s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").count():
            return jsonify({"error": "a buyer has a pending order at the current price"}), 409
        listing.price = price
        s.commit()
    finally:
        s.close()
    return jsonify({"listing_id": listing_id, "price": price, "fee_on_sale": _fee(price)})


@market_bp.route("/api/market/listings/<int:listing_id>/cancel", methods=["POST"])
@login_required
def cancel_listing(listing_id: int):
    s = get_session()
    try:
        listing = s.query(MarketListing).filter_by(
            id=listing_id, seller_id=current_user_id(), status="active"
        ).one_or_none()
        if listing is None:
            abort(404)
        pending = s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").count()
        if pending:
            return jsonify({"error": "a buyer has a pending order on this listing"}), 409
        listing.status = "cancelled"
        s.commit()
    finally:
        s.close()
    return jsonify({"cancelled": listing_id})


# ----------------------------------------------------------------------
# Browsing
# ----------------------------------------------------------------------

@market_bp.route("/market")
@login_required
def market_page():
    return render_template("market.html")


@market_bp.route("/api/market/listings")
@login_required
def browse():
    uid = current_user_id()
    s = get_session()
    try:
        rows = (
            s.query(MarketListing, CatalogCard, InventoryItem, User)
            .join(CatalogCard, MarketListing.catalog_card_id == CatalogCard.id)
            .join(InventoryItem, MarketListing.inventory_item_id == InventoryItem.id)
            .join(User, MarketListing.seller_id == User.id)
            .filter(MarketListing.status == "active")
            .order_by(MarketListing.created_at.desc())
            .all()
        )
        pending_ids = {row[0] for row in s.query(Order.listing_id)
                       .filter_by(status="pending_payment").all()}
        payload = []
        for listing, cc, item, seller in rows:
            market = cc.final_price
            vs_market = (
                round((listing.price - market) / market * 100, 1)
                if market else None
            )
            payload.append({
                "listing_id": listing.id,
                "catalog_card_id": cc.id,
                "name": cc.name,
                "set_name": cc.set_name,
                "set_code": cc.set_code,
                "card_number": cc.card_number,
                "rarity": cc.rarity,
                "is_holo": cc.is_holo,
                "condition": item.condition,
                "image_url": item.image_url or cc.image_url,
                "crop_path": item.source_crop_path,
                "price": listing.price,
                "market_price": market,
                "vs_market_pct": vs_market,
                "provenance": price_provenance(cc),
                "pending": listing.id in pending_ids,
                "seller": _mask_email(seller.email),
                "mine": listing.seller_id == uid,
                "listed_at": listing.created_at.isoformat() + "Z",
            })
    finally:
        s.close()
    return jsonify({
        "listings": payload,
        "fee_pct": PLATFORM_FEE_PCT,
        "payments": "stripe" if _stripe() else "offline",
    })


# ----------------------------------------------------------------------
# Buying
# ----------------------------------------------------------------------

@market_bp.route("/api/market/listings/<int:listing_id>/buy", methods=["POST"])
@login_required
def buy(listing_id: int):
    uid = current_user_id()
    s = get_session()
    try:
        listing = s.query(MarketListing).filter_by(id=listing_id, status="active").one_or_none()
        if listing is None:
            abort(404)
        if listing.seller_id == uid:
            return jsonify({"error": "that's your own listing"}), 400
        # An abandoned card checkout must not block the next buyer forever.
        _sweep_stripe_orders(
            s, s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").all())
        s.refresh(listing)
        if listing.status != "active":
            return jsonify({"error": "this card just sold"}), 409
        if s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").count():
            return jsonify({"error": "another buyer already has a pending order"}), 409
        item = s.get(InventoryItem, listing.inventory_item_id)
        if item is None or item.user_id != listing.seller_id or item.quantity < 1:
            # The seller no longer holds the card; the listing is stale.
            listing.status = "cancelled"
            s.commit()
            return jsonify({"error": "this card is no longer available"}), 409

        amount = listing.price
        order = Order(
            listing_id=listing.id,
            buyer_id=uid,
            seller_id=listing.seller_id,
            amount=amount,
            platform_fee=_fee(amount),
            seller_proceeds=round(amount - _fee(amount), 2),
        )

        stripe = _stripe()
        if stripe is not None:
            order.payment_provider = "stripe"
            s.add(order)
            s.commit()
            base = request.host_url.rstrip("/")
            try:
                session_obj = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=[{
                        "quantity": 1,
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": _cents(amount),
                            "product_data": {"name": listing.catalog_card.name},
                        },
                    }],
                    metadata={"order_id": str(order.id)},
                    client_reference_id=str(order.id),
                    # Stripe substitutes the session id into the template.
                    success_url=(f"{base}/market/orders/{order.id}/return"
                                 "?session_id={CHECKOUT_SESSION_ID}"),
                    cancel_url=f"{base}/market/orders/{order.id}/abandon",
                    expires_at=int(time.time()) + STRIPE_CHECKOUT_MINUTES * 60,
                )
            except Exception as exc:
                # No session, so nothing can ever settle this order: do not
                # leave it holding the card.
                order.status = "cancelled"
                s.commit()
                print(f"[market] stripe checkout failed: {exc}", file=sys.stderr)
                return jsonify({"error": f"could not start card payment: {exc}"}), 502
            order.payment_ref = session_obj["id"]
            s.commit()
            return jsonify({"order_id": order.id, "checkout_url": session_obj["url"]})

        # Offline settlement: order awaits the seller confirming payment.
        s.add(order)
        s.commit()
        return jsonify({
            "order_id": order.id,
            "status": "pending_payment",
            "note": ("No payment processor configured — settle payment with the "
                     "seller directly; they confirm with “mark paid”, which "
                     "transfers the card to your library."),
        })
    finally:
        s.close()


class PaymentError(Exception):
    """The order cannot be settled; the message is safe to show the user."""


def _apply_payment(s, order: Order) -> None:
    """Mark paid + move the card from seller to buyer. Caller commits.

    Safe to call twice for one order: the order is claimed with a conditional
    UPDATE first, so a double click or a webhook retry settles it once and
    the ledger gets one sold/purchased pair, not two.
    """
    claimed = s.query(Order).filter(
        Order.id == order.id, Order.status == "pending_payment"
    ).update({"status": "paid"}, synchronize_session=False)
    if claimed != 1:
        raise PaymentError("this order was already settled or cancelled")

    listing = s.get(MarketListing, order.listing_id)
    item = s.get(InventoryItem, listing.inventory_item_id)
    now = utcnow()
    if item is None or item.user_id != order.seller_id or item.quantity < 1:
        # Nothing to hand over. Cancel rather than record a sale of a card
        # the seller does not hold.
        s.query(Order).filter(Order.id == order.id).update(
            {"status": "cancelled"}, synchronize_session=False)
        listing.status = "cancelled"
        s.commit()
        raise PaymentError("the seller no longer has this card; the order was cancelled")

    # Both sides of the trade go in the library ledger. Cost basis is read
    # before the row changes hands below.
    common = dict(
        channel="market", unit_price=order.amount, order_id=order.id,
        condition=item.condition, image_url=item.image_url,
        crop_path=item.source_crop_path, occurred_at=now,
    )
    log_event(s, order.seller_id, listing.catalog_card_id, "sold",
              unit_cost=item.acquired_price, fees=order.platform_fee, **common)
    log_event(s, order.buyer_id, listing.catalog_card_id, "purchased", **common)

    if item.quantity > 1:
        item.quantity -= 1
        s.add(InventoryItem(
            user_id=order.buyer_id,
            catalog_card_id=item.catalog_card_id,
            quantity=1,
            condition=item.condition,
            acquired_price=order.amount,
            acquired_at=now,
            image_url=item.image_url,
            source_crop_path=item.source_crop_path,
        ))
    else:
        item.user_id = order.buyer_id
        item.acquired_price = order.amount
        item.acquired_at = now
        item.notes = None

    listing.status = "sold"
    listing.sold_at = now
    s.query(Order).filter(Order.id == order.id).update(
        {"paid_at": now}, synchronize_session=False)
    # Any other pending orders on this listing lose the race.
    s.query(Order).filter(
        Order.listing_id == listing.id,
        Order.id != order.id,
        Order.status == "pending_payment",
    ).update({"status": "cancelled"}, synchronize_session=False)


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def _settle_stripe_order(s, stripe, order: Order) -> str:
    """Ask Stripe what happened to an order's Checkout session and act on it.

    Returns the order's resulting status: 'paid', 'cancelled' or
    'pending_payment'. Commits. Trusts only what Stripe's API says about the
    session this server created, never a query string.
    """
    if order.status != "pending_payment":
        return order.status
    if not order.payment_ref:
        order.status = "cancelled"   # checkout never started
        s.commit()
        return "cancelled"
    session_obj = stripe.checkout.Session.retrieve(order.payment_ref)
    return _apply_stripe_session(s, order, session_obj)


def _apply_stripe_session(s, order: Order, session_obj) -> str:
    """Settle `order` from a Checkout session object (fetched or delivered
    by a verified webhook). Commits."""
    if order.status != "pending_payment":
        return order.status
    if session_obj["id"] != order.payment_ref:
        raise PaymentError("this payment belongs to a different order")
    if session_obj["payment_status"] == "paid":
        if int(session_obj["amount_total"]) != _cents(order.amount):
            raise PaymentError(
                f"Stripe charged {session_obj['amount_total']} cents but the order is "
                f"for {_cents(order.amount)}; not settling it automatically")
        try:
            _apply_payment(s, order)
        except PaymentError:
            # The buyer was charged but there is no card to hand over (the
            # order was cancelled by _apply_payment). Send the money back.
            _refund(session_obj, order)
            raise
        s.commit()
        return "paid"
    if session_obj["status"] == "expired":
        order.status = "cancelled"
        s.commit()
        return "cancelled"
    return "pending_payment"


def _refund(session_obj, order: Order) -> None:
    stripe = _stripe()
    try:
        stripe.Refund.create(payment_intent=session_obj["payment_intent"])
        print(f"[market] refunded order {order.id}: card no longer available", file=sys.stderr)
    except Exception as exc:
        print(f"[market] REFUND NEEDED for order {order.id} "
              f"(session {session_obj['id']}): {exc}", file=sys.stderr)


def _void_stripe_order(s, stripe, order: Order) -> str:
    """Cancel an unpaid card order. The Checkout session is expired first so
    the buyer cannot pay for a card that has been released; if the payment
    already went through, the order is settled instead. Commits."""
    status = _settle_stripe_order(s, stripe, order)
    if status != "pending_payment":
        return status
    try:
        stripe.checkout.Session.expire(order.payment_ref)
    except Exception as exc:
        # Expiring fails when the session completed a moment ago: look again.
        status = _settle_stripe_order(s, stripe, order)
        if status == "pending_payment":
            raise PaymentError(f"could not cancel the card payment: {exc}")
        return status
    order.status = "cancelled"
    s.commit()
    return "cancelled"


def _sweep_stripe_orders(s, orders) -> None:
    """Settle or release card orders whose checkout window has passed. Called
    where a stale order would otherwise be in the way."""
    cutoff = utcnow() - timedelta(minutes=STRIPE_CHECKOUT_MINUTES + 1)
    stale = [o for o in orders
             if o.status == "pending_payment" and o.payment_provider == "stripe"
             and o.created_at < cutoff]
    if not stale:
        return
    stripe = _stripe()
    if stripe is None:
        return
    for order in stale:
        try:
            if _settle_stripe_order(s, stripe, order) == "pending_payment":
                _void_stripe_order(s, stripe, order)
        except Exception as exc:
            s.rollback()
            print(f"[market] sweep order {order.id}: {exc}", file=sys.stderr)


@market_bp.route("/market/orders/<int:order_id>/return")
@login_required
def stripe_return(order_id: int):
    """Where Stripe sends the buyer after paying. Settles the order by asking
    Stripe directly, so card payments work without a webhook."""
    stripe = _stripe()
    s = get_session()
    try:
        order = s.query(Order).filter_by(id=order_id, buyer_id=current_user_id()).one_or_none()
        if order is None or order.payment_provider != "stripe":
            abort(404)
        if stripe is None:
            return redirect(f"/portfolio?payment=unknown&order={order_id}")
        try:
            status = _settle_stripe_order(s, stripe, order)
        except PaymentError as exc:
            print(f"[market] stripe return order {order_id}: {exc}", file=sys.stderr)
            return redirect(f"/portfolio?payment=problem&order={order_id}")
        except Exception as exc:
            print(f"[market] stripe return order {order_id}: {exc}", file=sys.stderr)
            return redirect(f"/portfolio?payment=unknown&order={order_id}")
    finally:
        s.close()
    outcome = {"paid": "paid", "completed": "paid", "cancelled": "cancelled"}.get(status, "pending")
    return redirect(f"/portfolio?payment={outcome}&order={order_id}")


@market_bp.route("/market/orders/<int:order_id>/abandon")
@login_required
def stripe_abandon(order_id: int):
    """Where Stripe sends a buyer who backs out of Checkout. Releases the
    card straight away instead of waiting for the session to expire."""
    stripe = _stripe()
    s = get_session()
    try:
        order = s.query(Order).filter_by(id=order_id, buyer_id=current_user_id()).one_or_none()
        if order is None or order.payment_provider != "stripe":
            abort(404)
        status = order.status
        if stripe is not None and status == "pending_payment":
            try:
                status = _void_stripe_order(s, stripe, order)
            except Exception as exc:
                s.rollback()
                print(f"[market] stripe abandon order {order_id}: {exc}", file=sys.stderr)
    finally:
        s.close()
    if status in ("paid", "completed"):
        return redirect(f"/portfolio?payment=paid&order={order_id}")
    return redirect(f"/market?payment=cancelled&order={order_id}")


@market_bp.route("/api/market/orders/<int:order_id>/mark-paid", methods=["POST"])
@login_required
def mark_paid(order_id: int):
    """Offline path: the SELLER confirms they've been paid."""
    s = get_session()
    try:
        order = s.query(Order).filter_by(
            id=order_id, seller_id=current_user_id(),
            status="pending_payment", payment_provider="offline",
        ).one_or_none()
        if order is None:
            abort(404)
        try:
            _apply_payment(s, order)
        except PaymentError as exc:
            return jsonify({"error": str(exc)}), 409
        s.commit()
    finally:
        s.close()
    return jsonify({"order_id": order_id, "status": "paid"})


@market_bp.route("/api/market/orders/<int:order_id>/cancel", methods=["POST"])
@login_required
def cancel_order(order_id: int):
    """Either side can back out of an unpaid order. Without this, a buyer who
    clicks Buy and never pays would hold the seller's card indefinitely.

    For a card order the Checkout session is expired first; if the payment
    turns out to have gone through already, the order is settled instead and
    this returns 409."""
    uid = current_user_id()
    s = get_session()
    try:
        order = s.query(Order).filter(
            Order.id == order_id, Order.status == "pending_payment",
            (Order.buyer_id == uid) | (Order.seller_id == uid),
        ).one_or_none()
        if order is None:
            abort(404)
        if order.payment_provider == "stripe":
            stripe = _stripe()
            if stripe is None:
                return jsonify({"error": "card payments are not configured on this server, "
                                         "so this order cannot be checked with Stripe"}), 409
            try:
                status = _void_stripe_order(s, stripe, order)
            except PaymentError as exc:
                return jsonify({"error": str(exc)}), 409
            except Exception as exc:
                s.rollback()
                return jsonify({"error": f"could not reach Stripe: {exc}"}), 502
            if status != "cancelled":
                return jsonify({"error": "this order has already been paid", "status": status}), 409
        else:
            order.status = "cancelled"
            s.commit()
    finally:
        s.close()
    return jsonify({"order_id": order_id, "status": "cancelled"})


@market_bp.route("/api/market/orders/<int:order_id>/complete", methods=["POST"])
@login_required
def complete_order(order_id: int):
    """The BUYER confirms the card arrived."""
    s = get_session()
    try:
        order = s.query(Order).filter_by(
            id=order_id, buyer_id=current_user_id(), status="paid"
        ).one_or_none()
        if order is None:
            abort(404)
        order.status = "completed"
        order.completed_at = utcnow()
        s.commit()
    finally:
        s.close()
    return jsonify({"order_id": order_id, "status": "completed"})


@market_bp.route("/api/market/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """Optional second path to settlement, for installs Stripe can reach.

    Only signed events are accepted. Without STRIPE_WEBHOOK_SECRET this
    endpoint is off: an unsigned "payment completed" from anyone on the
    network would otherwise hand over cards for free.
    """
    stripe = _stripe()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if stripe is None or not secret:
        abort(404)
    try:
        event = stripe.Webhook.construct_event(
            request.get_data(), request.headers.get("Stripe-Signature", ""), secret
        )
    except Exception as exc:
        return jsonify({"error": f"signature check failed: {exc}"}), 400

    kind = event["type"]
    if not kind.startswith("checkout.session."):
        return jsonify({"received": True})
    session_obj = event["data"]["object"]
    s = get_session()
    try:
        order = s.query(Order).filter_by(
            payment_ref=session_obj["id"], payment_provider="stripe"
        ).one_or_none()
        if order is not None and order.status == "pending_payment":
            try:
                if kind in ("checkout.session.completed",
                            "checkout.session.async_payment_succeeded",
                            "checkout.session.expired"):
                    _apply_stripe_session(s, order, session_obj)
                elif kind == "checkout.session.async_payment_failed":
                    order.status = "cancelled"
                    s.commit()
            except PaymentError as exc:
                s.rollback()
                print(f"[market] stripe webhook order {order.id}: {exc}", file=sys.stderr)
    finally:
        s.close()
    return jsonify({"received": True})


# ----------------------------------------------------------------------
# My sales / purchases / balance
# ----------------------------------------------------------------------

def _order_dict(s, o: Order) -> dict:
    cc = s.get(CatalogCard, o.listing.catalog_card_id) if o.listing else None
    return {
        "order_id": o.id,
        "card": cc.name if cc else None,
        "amount": o.amount,
        "platform_fee": o.platform_fee,
        "seller_proceeds": o.seller_proceeds,
        "status": o.status,
        "payment_provider": o.payment_provider,
        "created_at": o.created_at.isoformat() + "Z",
    }


@market_bp.route("/api/market/my")
@login_required
def my_market():
    uid = current_user_id()
    s = get_session()
    try:
        active = (
            s.query(MarketListing)
            .filter_by(seller_id=uid, status="active")
            .order_by(MarketListing.created_at.desc())
            .all()
        )
        active_out = [{
            "listing_id": l.id,
            "inventory_item_id": l.inventory_item_id,
            "card": l.catalog_card.name,
            "price": l.price,
            "market_price": l.catalog_card.final_price,
            "fee_on_sale": _fee(l.price),
            "listed_at": l.created_at.isoformat() + "Z",
        } for l in active]

        selling = (s.query(Order).filter_by(seller_id=uid)
                   .order_by(Order.created_at.desc()).limit(50).all())
        buying = (s.query(Order).filter_by(buyer_id=uid)
                  .order_by(Order.created_at.desc()).limit(50).all())

        _sweep_stripe_orders(s, [o for o in selling + buying if o.status == "pending_payment"])
        money = seller_balance(s, uid)

        out = {
            "active_listings": active_out,
            "sales": [_order_dict(s, o) for o in selling],
            "purchases": [_order_dict(s, o) for o in buying],
            **money,
            "fee_pct": PLATFORM_FEE_PCT,
            "payments": "stripe" if _stripe() else "offline",
            "is_operator": is_operator(s, uid),
        }
    finally:
        s.close()
    return jsonify(out)


# ----------------------------------------------------------------------
# Balances and payouts
# ----------------------------------------------------------------------

def seller_balance(s, seller_id: int) -> dict:
    """Where a seller stands with the platform.

    card_proceeds  their share of sales paid by card (the money is in the
                   operator's Stripe account)
    paid_out       what the operator has already sent them
    balance        card_proceeds - paid_out: what the operator still owes
    fees_owed      platform fees on sales the seller was paid for directly
    """
    settled = (s.query(Order)
               .filter(Order.seller_id == seller_id, Order.status.in_(("paid", "completed")))
               .all())
    card = sum(o.seller_proceeds for o in settled if o.payment_provider == "stripe")
    fees_owed = sum(o.platform_fee for o in settled if o.payment_provider != "stripe")
    paid_out = sum(p.amount for p in s.query(Payout).filter_by(seller_id=seller_id).all())
    return {
        "card_proceeds": round(card, 2),
        "paid_out": round(paid_out, 2),
        "balance": round(card - paid_out, 2),
        "fees_owed": round(fees_owed, 2),
        "fees_recorded": round(sum(o.platform_fee for o in settled), 2),
    }


def is_operator(s, uid) -> bool:
    """The operator runs the install and holds the Stripe account. Set
    ADMIN_EMAILS (comma separated) to name them; otherwise it is the first
    account registered."""
    if not uid:
        return False
    user = s.get(User, uid)
    if user is None:
        return False
    admins = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
    if admins:
        return user.email.lower() in admins
    first = s.query(User).order_by(User.id.asc()).first()
    return first is not None and first.id == user.id


@market_bp.route("/api/admin/balances")
@login_required
def admin_balances():
    """Operator only: every seller with money moving, and what is owed."""
    s = get_session()
    try:
        if not is_operator(s, current_user_id()):
            abort(403)
        seller_ids = {row[0] for row in s.query(Order.seller_id)
                      .filter(Order.status.in_(("paid", "completed"))).distinct().all()}
        seller_ids |= {row[0] for row in s.query(Payout.seller_id).distinct().all()}
        rows = []
        for sid in sorted(seller_ids):
            user = s.get(User, sid)
            rows.append({"seller_id": sid, "email": user.email if user else "?",
                         **seller_balance(s, sid)})
        payouts = [{
            "id": p.id, "seller_id": p.seller_id, "amount": p.amount, "method": p.method,
            "note": p.note, "created_at": p.created_at.isoformat() + "Z",
        } for p in s.query(Payout).order_by(Payout.created_at.desc()).limit(100).all()]
    finally:
        s.close()
    rows.sort(key=lambda r: -r["balance"])
    return jsonify({"sellers": rows, "payouts": payouts})


@market_bp.route("/api/admin/payouts", methods=["POST"])
@login_required
def record_payout():
    """Operator only. Body: {seller_id, amount, method?, note?}. Records that
    the operator sent a seller money; it does not move any money itself."""
    body = request.get_json(silent=True) or {}
    amount = _parse_price(body.get("amount"))
    if amount is None:
        return jsonify({"error": "amount must be a number greater than 0"}), 400
    s = get_session()
    try:
        uid = current_user_id()
        if not is_operator(s, uid):
            abort(403)
        try:
            seller = s.get(User, int(body.get("seller_id")))
        except (TypeError, ValueError):
            seller = None
        if seller is None:
            return jsonify({"error": "unknown seller"}), 400
        owed = seller_balance(s, seller.id)["balance"]
        if amount > owed + 0.005:
            return jsonify({"error": f"that is more than the ${owed:.2f} owed to this seller"}), 400
        s.add(Payout(
            seller_id=seller.id, amount=amount, recorded_by=uid,
            method=str(body.get("method") or "")[:40] or None,
            note=str(body.get("note") or "")[:500] or None,
        ))
        s.commit()
        out = {"seller_id": seller.id, **seller_balance(s, seller.id)}
    finally:
        s.close()
    return jsonify(out)
