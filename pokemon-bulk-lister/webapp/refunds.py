"""Refunds and chargebacks that happen after a card sale has settled.

A sale settles, the seller is paid, and later the money goes back to the
buyer: the operator refunds the charge in the Stripe dashboard, or the buyer
disputes it with their bank. Stripe takes that money from the platform's
balance, so the seller's share has to come back too, and the books have to
say what happened.

What Stripe reports, and what this module does about it:

  charge fully refunded     order -> 'refunded'; the seller's transfer is
                            reversed; the sale no longer counts in anyone's
                            totals; the operator is shown the card so it can
                            be handed back to the seller in one click
  charge partly refunded    the order's fee and seller share are recomputed
                            on what the buyer ended up paying, and the
                            difference is reversed from the seller's transfer
  charge disputed           order -> 'disputed'; the transfer is reversed
                            while the bank decides
  dispute won               order -> 'paid' again; the seller is paid again
  dispute lost              order -> 'refunded'

How it hears about it: the signed webhook (charge.refunded,
charge.dispute.created, charge.dispute.closed) when the install is reachable
from the internet, and "Check Stripe for refunds" on the operator panel
otherwise, which asks Stripe about each recent card order. Both paths end in
apply_charge_state, which only moves an order toward what Stripe says and is
safe to run any number of times.

A reversal Stripe refuses (the seller already withdrew the money) leaves the
transfer on the books, so the seller's balance goes negative: they owe the
platform, and the operator sees by how much.

Calls, per Stripe's API reference: Transfer.create_reversal(id, amount=),
Charge.retrieve, Dispute.list(charge=). Charges made through this app carry
transfer_group "order-<id>", which is how a charge is tied back to its order.
Not run against Stripe itself.
"""
from __future__ import annotations

import re
import sys
from datetime import timedelta
from typing import Optional

from flask import Blueprint, jsonify

from webapp.auth import current_user_id, is_operator, verified_required
from webapp.models import (
    InventoryItem,
    MarketListing,
    Order,
    OrderTransfer,
    User,
    get_session,
    log_event,
    utcnow,
)

refunds_bp = Blueprint("refunds", __name__)

SETTLED = ("paid", "completed")
# How far back "Check Stripe for refunds" looks. Card disputes can be opened
# up to about 120 days after the charge.
LOOKBACK_DAYS = 130


def _cents(amount: float) -> int:
    return int(round(amount * 100))


def order_id_from_group(transfer_group: Optional[str]) -> Optional[int]:
    m = re.fullmatch(r"order-(\d+)", transfer_group or "")
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------
# Taking the seller's share back
# ----------------------------------------------------------------------

def _reverse_transfer(s, stripe, order: Order, amount: Optional[float] = None) -> Optional[str]:
    """Reverse all (or `amount`) of the transfer that paid the seller for
    this order. Returns an error string if Stripe refused, else None.
    Commits. Does nothing if the seller was never paid by transfer."""
    row = s.query(OrderTransfer).filter_by(order_id=order.id).one_or_none()
    if row is None or row.transfer_id == "pending":
        return None
    take = round(min(amount if amount is not None else row.amount, row.amount), 2)
    if take <= 0:
        return None
    try:
        stripe.Transfer.create_reversal(
            row.transfer_id, amount=_cents(take),
            # Keyed on what is left, so a repeat of the same step is the same
            # request, and a later, further reversal is a new one.
            idempotency_key=f"reversal-{row.transfer_id}-{_cents(row.amount)}-{_cents(take)}",
        )
    except Exception as exc:
        print(f"[refunds] could not reverse {row.transfer_id} for order {order.id}: {exc}",
              file=sys.stderr)
        return str(exc)
    if take >= row.amount - 0.005:
        s.delete(row)
    else:
        row.amount = round(row.amount - take, 2)
    s.commit()
    return None


# ----------------------------------------------------------------------
# Bringing an order in line with its charge
# ----------------------------------------------------------------------

def apply_charge_state(s, stripe, order: Order, charge, dispute_status: Optional[str] = None) -> Optional[str]:
    """Move `order` to match what Stripe says about its charge. Returns a
    short description of what changed, or None. Commits.

    dispute_status: the dispute's status when known (from a dispute event or
    Dispute.list); None means "look at charge['disputed'] only".
    """
    from webapp.marketplace import PLATFORM_FEE_PCT

    if order.payment_provider != "stripe" or order.status not in SETTLED + ("disputed",):
        return None
    charged = int(charge["amount"])
    refunded = int(charge["amount_refunded"])

    if charge["refunded"] or refunded >= charged or dispute_status == "lost":
        problem = _reverse_transfer(s, stripe, order)
        order.status = "refunded"
        s.commit()
        why = "the dispute was lost" if dispute_status == "lost" else "refunded in Stripe"
        return f"order {order.id}: {why}" + (f"; reversal refused ({problem})" if problem else "")

    if dispute_status in ("won", "warning_closed", "prevented") and order.status == "disputed":
        order.status = "paid"
        s.commit()
        return f"order {order.id}: dispute closed in your favour; the seller will be paid again"

    if (charge["disputed"] or dispute_status) and dispute_status not in ("won", "warning_closed", "prevented"):
        if order.status != "disputed":
            problem = _reverse_transfer(s, stripe, order)
            order.status = "disputed"
            s.commit()
            return f"order {order.id}: disputed by the buyer" + (
                f"; reversal refused ({problem})" if problem else "")
        return None

    if refunded > 0:
        # Partial refund. The order keeps its original amount; what the buyer
        # ended up paying is fee + seller share, so "already handled" can be
        # read off the order itself and this stays idempotent.
        net = round((charged - refunded) / 100.0, 2)
        if abs(net - (order.platform_fee + order.seller_proceeds)) < 0.005:
            return None
        new_fee = round(net * PLATFORM_FEE_PCT / 100.0, 2)
        new_share = round(net - new_fee, 2)
        give_back = round(order.seller_proceeds - new_share, 2)
        order.platform_fee, order.seller_proceeds = new_fee, new_share
        s.commit()
        problem = _reverse_transfer(s, stripe, order, amount=give_back) if give_back > 0 else None
        return (f"order {order.id}: ${refunded / 100:.2f} refunded in Stripe; "
                f"seller share is now ${new_share:.2f}"
                + (f"; reversal refused ({problem})" if problem else ""))
    return None


def _dispute_status(stripe, charge) -> Optional[str]:
    if not charge["disputed"]:
        return None
    found = stripe.Dispute.list(charge=charge["id"], limit=1)
    return found["data"][0]["status"] if found["data"] else None


def check_order(s, stripe, order: Order) -> Optional[str]:
    """Ask Stripe about one settled card order and apply what it says."""
    if not order.payment_ref:
        return None
    session_obj = stripe.checkout.Session.retrieve(order.payment_ref)
    intent = stripe.PaymentIntent.retrieve(session_obj["payment_intent"])
    charge = stripe.Charge.retrieve(intent["latest_charge"])
    return apply_charge_state(s, stripe, order, charge, _dispute_status(stripe, charge))


def handle_event(s, stripe, event) -> Optional[str]:
    """A verified webhook event about a charge or a dispute."""
    kind, obj = event["type"], event["data"]["object"]
    if kind == "charge.refunded":
        charge, status = obj, None
    elif kind in ("charge.dispute.created", "charge.dispute.closed"):
        charge, status = stripe.Charge.retrieve(obj["charge"]), obj["status"]
    else:
        return None
    oid = order_id_from_group(charge["transfer_group"])
    order = s.get(Order, oid) if oid else None
    if order is None:
        return None
    changed = apply_charge_state(s, stripe, order, charge, status)
    if changed and order.status == "paid":
        from webapp import payouts
        payouts.pay_seller_quietly(order.seller_id)     # a won dispute: pay again
    return changed


# ----------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------

def _operator(s):
    return is_operator(s, current_user_id())


def needs_attention(s) -> list[dict]:
    """Refunded and disputed orders, for the operator panel. A refunded order
    whose card is still with the buyer can have it handed back."""
    out = []
    for o in (s.query(Order).filter(Order.status.in_(("refunded", "disputed")))
              .order_by(Order.id.desc()).limit(100).all()):
        listing = s.get(MarketListing, o.listing_id)
        item = s.get(InventoryItem, listing.inventory_item_id) if listing else None
        with_buyer = bool(item and item.user_id == o.buyer_id and item.quantity > 0)
        buyer, seller = s.get(User, o.buyer_id), s.get(User, o.seller_id)
        still_out = s.query(OrderTransfer).filter_by(order_id=o.id).one_or_none()
        out.append({
            "order_id": o.id, "status": o.status, "amount": o.amount,
            "card": listing.catalog_card.name if listing else None,
            "buyer": buyer.email if buyer else "?", "seller": seller.email if seller else "?",
            "card_with_buyer": with_buyer and o.status == "refunded",
            # The reversal was refused: the seller still holds this much.
            "seller_still_holds": still_out.amount if still_out else 0,
        })
    return out


@refunds_bp.route("/api/admin/refunds/check", methods=["POST"])
@verified_required
def check_refunds():
    """Operator only: ask Stripe about every recent settled card order and
    bring the books in line. For installs Stripe's webhook cannot reach."""
    from webapp import marketplace, payouts

    stripe = marketplace._stripe()   # noqa: SLF001
    if stripe is None:
        return jsonify({"error": "card payments are not set up on this install"}), 409
    s = get_session()
    try:
        if not _operator(s):
            return jsonify({"error": "operator only"}), 403
        since = utcnow() - timedelta(days=LOOKBACK_DAYS)
        orders = (s.query(Order)
                  .filter(Order.payment_provider == "stripe", Order.created_at >= since,
                          Order.status.in_(SETTLED + ("disputed",))).all())
        changes, errors, repay = [], [], set()
        for order in orders:
            try:
                changed = check_order(s, stripe, order)
            except Exception as exc:
                s.rollback()
                errors.append(f"order {order.id}: {exc}")
                continue
            if changed:
                changes.append(changed)
                if order.status == "paid":
                    repay.add(order.seller_id)
        checked = len(orders)
    finally:
        s.close()
    for seller_id in repay:
        payouts.pay_seller_quietly(seller_id)
    return jsonify({"checked": checked, "changes": changes, "errors": errors[:10]})


@refunds_bp.route("/api/admin/orders/<int:order_id>/return-card", methods=["POST"])
@verified_required
def return_card(order_id: int):
    """Operator only: a refunded order's card goes back to the seller's
    library. Both ledgers get a row saying so. Refused if the buyer no longer
    holds the card (they sold or removed it), which a person must sort out."""
    s = get_session()
    try:
        if not _operator(s):
            return jsonify({"error": "operator only"}), 403
        order = s.query(Order).filter_by(id=order_id, status="refunded").one_or_none()
        if order is None:
            return jsonify({"error": "no such refunded order"}), 404
        listing = s.get(MarketListing, order.listing_id)
        item = s.get(InventoryItem, listing.inventory_item_id) if listing else None
        if item is None or item.user_id != order.buyer_id or item.quantity < 1:
            return jsonify({"error": "the buyer no longer holds this card, so it cannot be moved back"}), 409
        if s.query(MarketListing).filter_by(inventory_item_id=item.id, status="active").count():
            return jsonify({"error": "the buyer has this card listed for sale; it must be unlisted first"}), 409
        common = dict(channel="market", quantity=1, condition=item.condition,
                      image_url=item.image_url, crop_path=item.source_crop_path, order_id=order.id)
        log_event(s, order.buyer_id, item.catalog_card_id, "removed",
                  note=f"order {order.id} was refunded; card returned to the seller", **common)
        log_event(s, order.seller_id, item.catalog_card_id, "added",
                  note=f"order {order.id} was refunded; card returned", **common)
        if item.quantity > 1:
            item.quantity -= 1
            s.add(InventoryItem(
                user_id=order.seller_id, catalog_card_id=item.catalog_card_id, quantity=1,
                condition=item.condition, image_url=item.image_url,
                source_crop_path=item.source_crop_path))
        else:
            item.user_id = order.seller_id
            item.acquired_price = None
            item.acquired_at = None
        s.commit()
    finally:
        s.close()
    return jsonify({"order_id": order_id, "returned": True})


def refunded_order_ids(s) -> set[int]:
    """Orders whose money went back. Their sold/purchased ledger rows stay
    (the ledger is a record of what happened) but stop counting in totals."""
    return {row[0] for row in s.query(Order.id).filter(Order.status == "refunded").all()}

