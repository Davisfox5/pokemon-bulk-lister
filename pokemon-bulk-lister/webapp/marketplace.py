"""On-platform marketplace: list a card for sale in one click, browse, buy.

The platform records a fee on every sale (PLATFORM_FEE_PCT of the sale price,
default 10%). Two payment paths, same ledger:

  stripe    If STRIPE_SECRET_KEY is set (and the `stripe` package installed),
            "buy" returns a Stripe Checkout URL; the webhook marks the order
            paid. Money lands in the platform's Stripe account; seller_proceeds
            accrue to the seller's balance (paid out off-platform for now).
  offline   No Stripe configured: the order is created pending_payment, buyer
            and seller settle directly (Venmo/cash/trade night), and the seller
            clicks "mark paid" — the fee is recorded as owed to the platform.

On payment the card actually changes hands in the database: the inventory item
moves from seller to buyer (with the sale price as the buyer's cost basis), so
both portfolios stay truthful.
"""
from __future__ import annotations

import os
import sys
from flask import Blueprint, abort, jsonify, render_template, request

from webapp.auth import current_user_id, login_required
from webapp.catalog import price_provenance
from webapp.models import (
    CatalogCard,
    InventoryItem,
    MarketListing,
    Order,
    User,
    get_session,
    log_event,
    utcnow,
)

market_bp = Blueprint("market", __name__)

PLATFORM_FEE_PCT = float(os.getenv("PLATFORM_FEE_PCT", "10"))


def _fee(amount: float) -> float:
    return round(amount * PLATFORM_FEE_PCT / 100.0, 2)


def _mask_email(email: str) -> str:
    local = email.split("@")[0]
    return (local[:1] + "***") if local else "***"


def _stripe():
    """Return a configured stripe module, or None when not set up.

    TODO(launch): wire up Stripe before launch — decision 2026-07-22 is to ship
    offline settlement until then. Launch checklist: create the platform Stripe
    account, set STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET, `pip install
    stripe`, register the /api/market/stripe-webhook endpoint in the Stripe
    dashboard, sandbox-test the checkout path (it is code-complete but has
    never run against a real key), and decide on Stripe Connect for automated
    seller payouts vs. manual payouts from the balance ledger.
    """
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

def _parse_price(value) -> float | None:
    """A positive dollar amount, or None when the value is not one."""
    try:
        price = round(float(value), 2)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


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
        s.commit()
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
                out["to_list"] += 1
                out["total"] += price
                if not dry_run:
                    _create_listing(s, item, price)
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
        if s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").count():
            return jsonify({"error": "another buyer already has a pending order"}), 409

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
            session_obj = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "quantity": 1,
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(round(amount * 100)),
                        "product_data": {"name": listing.catalog_card.name},
                    },
                }],
                metadata={"order_id": str(order.id)},
                success_url=f"{base}/portfolio?order={order.id}&paid=1",
                cancel_url=f"{base}/market?cancelled={order.id}",
            )
            order.payment_ref = session_obj.id
            s.commit()
            return jsonify({"order_id": order.id, "checkout_url": session_obj.url})

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


def _apply_payment(s, order: Order) -> None:
    """Mark paid + move the card from seller to buyer. Caller commits."""
    listing = s.get(MarketListing, order.listing_id)
    item = s.get(InventoryItem, listing.inventory_item_id)
    now = utcnow()

    # Both sides of the trade go in the library ledger. Cost basis is read
    # before the row changes hands below.
    common = dict(
        channel="market", unit_price=order.amount, order_id=order.id,
        condition=item.condition if item else None,
        image_url=item.image_url if item else None,
        crop_path=item.source_crop_path if item else None,
        occurred_at=now,
    )
    log_event(s, order.seller_id, listing.catalog_card_id, "sold",
              unit_cost=item.acquired_price if item else None,
              fees=order.platform_fee, **common)
    log_event(s, order.buyer_id, listing.catalog_card_id, "purchased", **common)

    if item is not None and item.user_id == order.seller_id:
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
    order.status = "paid"
    order.paid_at = now
    # Any other pending orders on this listing lose the race.
    s.query(Order).filter(
        Order.listing_id == listing.id,
        Order.id != order.id,
        Order.status == "pending_payment",
    ).update({"status": "cancelled"})


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
        _apply_payment(s, order)
        s.commit()
    finally:
        s.close()
    return jsonify({"order_id": order_id, "status": "paid"})


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
    stripe = _stripe()
    if stripe is None:
        abort(404)
    payload = request.get_data()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    try:
        if secret:
            event = stripe.Webhook.construct_event(
                payload, request.headers.get("Stripe-Signature", ""), secret
            )
        else:
            event = stripe.Event.construct_from(request.get_json(force=True), stripe.api_key)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    if event["type"] == "checkout.session.completed":
        order_id = int(event["data"]["object"]["metadata"]["order_id"])
        s = get_session()
        try:
            order = s.query(Order).filter_by(id=order_id, status="pending_payment").one_or_none()
            if order is not None:
                _apply_payment(s, order)
                s.commit()
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

        balance = round(sum(
            o.seller_proceeds for o in selling if o.status in ("paid", "completed")
        ), 2)
        fees_recorded = round(sum(
            o.platform_fee for o in selling if o.status in ("paid", "completed")
        ), 2)

        out = {
            "active_listings": active_out,
            "sales": [_order_dict(s, o) for o in selling],
            "purchases": [_order_dict(s, o) for o in buying],
            "balance": balance,
            "fees_recorded": fees_recorded,
            "fee_pct": PLATFORM_FEE_PCT,
            "payments": "stripe" if _stripe() else "offline",
        }
    finally:
        s.close()
    return jsonify(out)
