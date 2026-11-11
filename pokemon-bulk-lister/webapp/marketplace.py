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

from webapp.auth import current_user_id, is_operator, login_required, verified_required
from webapp.catalog import price_provenance
from webapp.models import (
    CatalogCard,
    InventoryItem,
    MarketListing,
    Order,
    OrderTransfer,
    Payout,
    SellerPayoutAccount,
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
@verified_required
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
@verified_required
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
@verified_required
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
@verified_required
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
            try:
                s.commit()
            except IntegrityError:
                s.rollback()   # uq_pending_order: another buyer got there first
                return jsonify({"error": "another buyer already has a pending order"}), 409
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
                    # Cards only: they confirm at once. Payment methods that
                    # can fail days later would let a seller be paid for a
                    # charge that then bounces.
                    payment_method_types=["card"],
                    payment_intent_data={"transfer_group": f"order-{order.id}"},
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
            # Attach the session only if the order is still open. If the
            # seller cancelled it while Stripe was answering, the buyer must
            # not be handed a live checkout for a dead order.
            attached = s.query(Order).filter(
                Order.id == order.id, Order.status == "pending_payment"
            ).update({"payment_ref": session_obj["id"]}, synchronize_session=False)
            s.commit()
            if attached != 1:
                try:
                    stripe.checkout.Session.expire(session_obj["id"])
                except Exception as exc:
                    print(f"[market] could not expire {session_obj['id']}: {exc}", file=sys.stderr)
                return jsonify({"error": "the seller cancelled this order"}), 409
            return jsonify({"order_id": order.id, "checkout_url": session_obj["url"]})

        # Offline settlement: order awaits the seller confirming payment.
        s.add(order)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return jsonify({"error": "another buyer already has a pending order"}), 409
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


class AlreadySettled(PaymentError):
    """Another request claimed the order first. Not a failure of the payment:
    the caller must look at what the order became before doing anything."""


class CardUnavailable(PaymentError):
    """The seller no longer holds the card. The order has been cancelled."""


def _apply_payment(s, order: Order) -> None:
    """Mark paid + move the card from seller to buyer. Caller commits.

    Safe to call twice for one order: the order is claimed with a conditional
    UPDATE first, so a double click or a webhook retry settles it once and
    the ledger gets one sold/purchased pair, not two. The loser of that race
    gets AlreadySettled.
    """
    claimed = s.query(Order).filter(
        Order.id == order.id, Order.status == "pending_payment"
    ).update({"status": "paid"}, synchronize_session=False)
    if claimed != 1:
        raise AlreadySettled("this order was already settled or cancelled")

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
        raise CardUnavailable("the seller no longer has this card; the order was cancelled")

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
    # Any other pending orders on this listing lose the race. Their card
    # checkouts are closed too, so nobody pays for a card that just sold (if
    # one slips through, _apply_stripe_session refunds it).
    losers = s.query(Order).filter(
        Order.listing_id == listing.id,
        Order.id != order.id,
        Order.status == "pending_payment",
    ).all()
    for other in losers:
        other.status = "cancelled"
        _expire_session(other)


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def _expire_session(order: Order) -> None:
    """Best effort: close an order's Checkout session so it cannot be paid."""
    if order.payment_provider != "stripe" or not order.payment_ref:
        return
    stripe = _stripe()
    if stripe is None:
        return
    try:
        stripe.checkout.Session.expire(order.payment_ref)
    except Exception as exc:   # already complete or already expired
        print(f"[market] expire {order.payment_ref}: {exc}", file=sys.stderr)


def _refund(s, session_obj, order: Order, why: str) -> None:
    """Send a payment back and make sure a failure to do so is not lost.

    The idempotency key means two requests refunding the same order cannot
    refund it twice. If Stripe will not take the refund, the order is marked
    'refund_due' so the operator sees it; it is never just a log line.
    Commits.
    """
    stripe = _stripe()
    try:
        stripe.Refund.create(
            payment_intent=session_obj["payment_intent"],
            idempotency_key=f"refund-order-{order.id}",
        )
        print(f"[market] refunded order {order.id}: {why}", file=sys.stderr)
    except Exception as exc:
        print(f"[market] REFUND NEEDED for order {order.id} "
              f"(session {session_obj['id']}): {exc}", file=sys.stderr)
        s.query(Order).filter(Order.id == order.id).update(
            {"status": "refund_due"}, synchronize_session=False)
        s.commit()


def _settle_stripe_order(s, stripe, order: Order) -> str:
    """Ask Stripe what happened to an order's Checkout session and act on it.

    Returns the order's resulting status. Commits. Trusts only what Stripe's
    API says about the session this server created, never a query string.
    """
    s.refresh(order)
    if order.status in ("paid", "completed", "refund_due"):
        return order.status
    if not order.payment_ref:
        if order.status == "pending_payment" and (
                utcnow() - order.created_at > timedelta(minutes=2)):
            order.status = "cancelled"   # checkout never started
            s.commit()
        # A younger order is still inside buy(): Stripe is creating its
        # session right now. Leave it alone.
        return order.status
    session_obj = stripe.checkout.Session.retrieve(order.payment_ref)
    return _apply_stripe_session(s, order, session_obj)


def _apply_stripe_session(s, order: Order, session_obj) -> str:
    """Settle `order` from a Checkout session object (fetched or delivered
    by a verified webhook). Commits.

    Every way this can go is handled here, so that no outcome leaves a buyer
    charged with neither a card nor a refund:
      paid, order open           -> hand the card over
      paid, order already paid   -> nothing to do (another request won)
      paid, order cancelled      -> refund
      paid, card gone            -> refund
      paid, wrong amount         -> cancel and refund
      expired, order open        -> cancel
    """
    if session_obj["id"] != order.payment_ref:
        raise PaymentError("this payment belongs to a different order")
    s.refresh(order)   # the row may have changed while Stripe was answering
    if order.status in ("paid", "completed", "refund_due"):
        return order.status
    paid = session_obj["payment_status"] == "paid"

    if order.status == "cancelled":
        if paid:
            _refund(s, session_obj, order, "paid after the order was cancelled")
        s.refresh(order)
        return order.status

    if paid:
        expected = _cents(order.amount)
        if int(session_obj["amount_total"]) != expected or session_obj["currency"] != "usd":
            # Not what was asked for (a tax, a discount, another currency).
            # Do not guess: release the card and give the money back.
            s.query(Order).filter(
                Order.id == order.id, Order.status == "pending_payment"
            ).update({"status": "cancelled"}, synchronize_session=False)
            s.commit()
            _refund(s, session_obj, order, "amount charged did not match the order")
            raise PaymentError(
                f"Stripe charged {session_obj['amount_total']} {session_obj['currency']} but "
                f"the order is for {expected} usd; it was cancelled and refunded")
        try:
            _apply_payment(s, order)
        except AlreadySettled:
            # Lost a race (webhook vs. browser return). Whatever the winner
            # decided stands; go round again to act on it.
            s.rollback()
            return _apply_stripe_session(s, order, session_obj)
        except CardUnavailable:
            _refund(s, session_obj, order, "card no longer available")
            raise
        s.commit()
        # The sale is done. Now send the seller their share, if they have
        # payouts set up. A failure here leaves a balance to retry, nothing more.
        from webapp import payouts
        payouts.pay_seller_quietly(order.seller_id)
        return "paid"

    if session_obj["status"] == "expired":
        s.query(Order).filter(
            Order.id == order.id, Order.status == "pending_payment"
        ).update({"status": "cancelled"}, synchronize_session=False)
        s.commit()
        s.refresh(order)
        return order.status
    return "pending_payment"


def _void_stripe_order(s, stripe, order: Order) -> str:
    """Cancel an unpaid card order. The Checkout session is expired first so
    the buyer cannot pay for a card that has been released; if the payment
    already went through, the order is settled instead. Commits."""
    status = _settle_stripe_order(s, stripe, order)
    if status != "pending_payment":
        return status
    if not order.payment_ref:
        raise PaymentError("checkout is still starting; try again in a moment")
    try:
        stripe.checkout.Session.expire(order.payment_ref)
    except Exception as exc:
        # Expiring fails when the session completed a moment ago: look again.
        status = _settle_stripe_order(s, stripe, order)
        if status == "pending_payment":
            raise PaymentError(f"could not cancel the card payment: {exc}")
        return status
    s.query(Order).filter(
        Order.id == order.id, Order.status == "pending_payment"
    ).update({"status": "cancelled"}, synchronize_session=False)
    s.commit()
    s.refresh(order)
    return order.status


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
    for order in stale:
        try:
            if stripe is None:
                # The key was removed. The session has expired by now, so the
                # card can be released. Should it turn out to have been paid,
                # the cancelled-but-paid rule refunds it once Stripe is back.
                order.status = "cancelled"
                s.commit()
            elif _settle_stripe_order(s, stripe, order) == "pending_payment":
                _void_stripe_order(s, stripe, order)
        except Exception as exc:
            s.rollback()
            print(f"[market] sweep order {order.id}: {exc}", file=sys.stderr)


@market_bp.route("/market/orders/<int:order_id>/return")
@login_required
def stripe_return(order_id: int):
    """Where Stripe sends the buyer after paying. Settles the order by asking
    Stripe directly, so card payments work without a webhook. Safe as a GET:
    it only ever acts on what Stripe says about this order's own session."""
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
            s.rollback()
            print(f"[market] stripe return order {order_id}: {exc}", file=sys.stderr)
            return redirect(f"/portfolio?payment=unknown&order={order_id}")
    finally:
        s.close()
    outcome = {"paid": "paid", "completed": "paid", "cancelled": "cancelled",
               "refund_due": "problem"}.get(status, "pending")
    return redirect(f"/portfolio?payment={outcome}&order={order_id}")


@market_bp.route("/market/orders/<int:order_id>/abandon")
@login_required
def stripe_abandon(order_id: int):
    """Where Stripe sends a buyer who backs out of Checkout. Changes nothing
    itself (a link must not be able to cancel someone's checkout): the
    marketplace page it lands on cancels the order with a same-origin POST."""
    return redirect(f"/market?payment=cancelled&order={order_id}")


@market_bp.route("/api/market/orders/<int:order_id>/mark-paid", methods=["POST"])
@verified_required
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
                _sweep_stripe_orders(s, [order])   # releases it once its checkout has expired
                s.refresh(order)
                if order.status == "cancelled":
                    return jsonify({"order_id": order_id, "status": "cancelled"})
                return jsonify({"error": "card payments are switched off on this server, so the "
                                         "payment cannot be checked. The order is released "
                                         f"{STRIPE_CHECKOUT_MINUTES} minutes after it was placed."}), 409
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
    if kind.startswith("charge."):
        # Money going back after a sale settled: a refund from the Stripe
        # dashboard, or a dispute. See webapp/refunds.py.
        from webapp import refunds
        s = get_session()
        try:
            changed = refunds.handle_event(s, stripe, event)
            if changed:
                print(f"[market] {changed}", file=sys.stderr)
        except Exception as exc:
            s.rollback()
            print(f"[market] stripe webhook {kind}: {exc}", file=sys.stderr)
        finally:
            s.close()
        return jsonify({"received": True})
    if not kind.startswith("checkout.session."):
        return jsonify({"received": True})
    session_obj = event["data"]["object"]
    s = get_session()
    try:
        order = s.query(Order).filter_by(
            payment_ref=session_obj["id"], payment_provider="stripe"
        ).one_or_none()
        if order is not None:
            try:
                if kind in ("checkout.session.completed",
                            "checkout.session.async_payment_succeeded",
                            "checkout.session.expired"):
                    # Also for an order that is no longer open: a payment
                    # that lands on a cancelled order has to be refunded.
                    _apply_stripe_session(s, order, session_obj)
                elif kind == "checkout.session.async_payment_failed":
                    s.query(Order).filter(
                        Order.id == order.id, Order.status == "pending_payment"
                    ).update({"status": "cancelled"}, synchronize_session=False)
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
    by_hand = sum(p.amount for p in s.query(Payout).filter_by(seller_id=seller_id).all())
    # Automatic transfers, including ones still being confirmed with Stripe:
    # a claim counts from the moment it is made, so it cannot be paid again.
    transferred = sum(
        t.amount for t in s.query(OrderTransfer).join(Order, OrderTransfer.order_id == Order.id)
        .filter(Order.seller_id == seller_id).all())
    paid_out = by_hand + transferred
    return {
        "card_proceeds": round(card, 2),
        "paid_out": round(paid_out, 2),
        "balance": round(card - paid_out, 2),
        "fees_owed": round(fees_owed, 2),
        "fees_recorded": round(sum(o.platform_fee for o in settled), 2),
    }


@market_bp.route("/api/admin/balances")
@verified_required
def admin_balances():
    """Operator only: every seller with money moving, and what is owed."""
    s = get_session()
    try:
        if not is_operator(s, current_user_id()):
            abort(403)
        seller_ids = {row[0] for row in s.query(Order.seller_id)
                      .filter(Order.status.in_(("paid", "completed"))).distinct().all()}
        seller_ids |= {row[0] for row in s.query(Payout.seller_id).distinct().all()}
        # A seller who kept a transfer for a refunded sale owes money back.
        seller_ids |= {row[0] for row in s.query(Order.seller_id)
                       .join(OrderTransfer, OrderTransfer.order_id == Order.id).distinct().all()}
        rows = []
        for sid in sorted(seller_ids):
            user = s.get(User, sid)
            acct = s.query(SellerPayoutAccount).filter_by(user_id=sid).one_or_none()
            rows.append({"seller_id": sid, "email": user.email if user else "?",
                         "auto_payouts": bool(acct and acct.transfers_active),
                         **seller_balance(s, sid)})
        refunds_due = [{
            "order_id": o.id, "amount": o.amount, "payment_ref": o.payment_ref,
            "buyer": (s.get(User, o.buyer_id).email if s.get(User, o.buyer_id) else "?"),
            "created_at": o.created_at.isoformat() + "Z",
        } for o in s.query(Order).filter_by(status="refund_due").all()]
        payouts = [{
            "id": p.id, "seller_id": p.seller_id, "amount": p.amount, "method": p.method,
            "note": p.note, "created_at": p.created_at.isoformat() + "Z",
        } for p in s.query(Payout).order_by(Payout.created_at.desc()).limit(100).all()]
        payouts += [{
            "id": f"t{t.id}", "seller_id": o.seller_id, "amount": t.amount, "method": "stripe",
            "note": (f"automatic transfer {t.transfer_id} for order {t.order_id}"
                     if t.transfer_id != "pending" else
                     f"transfer for order {t.order_id} is being confirmed with Stripe"),
            "created_at": t.created_at.isoformat() + "Z",
        } for t, o in s.query(OrderTransfer, Order).join(Order, OrderTransfer.order_id == Order.id)
            .order_by(OrderTransfer.created_at.desc()).limit(100).all()]
        payouts.sort(key=lambda r: r["created_at"], reverse=True)
        from webapp import refunds
        attention = refunds.needs_attention(s)
    finally:
        s.close()
    rows.sort(key=lambda r: -r["balance"])
    return jsonify({"sellers": rows, "payouts": payouts, "refunds_due": refunds_due,
                    "attention": attention})


@market_bp.route("/api/admin/payouts", methods=["POST"])
@verified_required
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
        from webapp.payouts import payout_lock
        # Check and record as one step, so a double submit (or an automatic
        # transfer running at the same moment) cannot overdraw the balance.
        with payout_lock:
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
