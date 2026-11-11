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

Calls, per Stripe's API reference: Transfer.retrieve,
Transfer.create_reversal(id, amount=), Charge.retrieve, Dispute.list(charge=). Charges made through this app carry
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
    LibraryEvent,
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

def _settle_transfer(s, stripe, order: Order, keep: float) -> Optional[str]:
    """Make the seller's transfer for this order worth at most `keep`
    (0 = take it all back). Returns an error string if Stripe refused, else
    None. Commits.

    Works from what Stripe says the transfer is worth now, not from local
    arithmetic, so it can be run again after any failure: a reversal Stripe
    refused is retried, and one Stripe made but this app failed to record is
    noticed and recorded instead of being repeated.
    """
    from webapp import payouts

    with payouts.payout_lock:
        row = s.query(OrderTransfer).filter_by(order_id=order.id).one_or_none()
        if row is not None and row.transfer_id == payouts.PENDING:
            # A payout for this order is mid-flight or was interrupted. Find
            # out whether the transfer exists before deciding anything.
            try:
                payouts._reconcile_pending(s, stripe, order.seller_id)   # noqa: SLF001
            except Exception as exc:
                s.rollback()
                return f"could not check an unfinished payout with Stripe: {exc}"
            row = s.query(OrderTransfer).filter_by(order_id=order.id).one_or_none()
        if row is None:
            return None
        try:
            transfer = stripe.Transfer.retrieve(row.transfer_id)
            left = int(transfer["amount"]) - int(transfer["amount_reversed"])
            take = left - _cents(max(keep, 0.0))
            if take > 0:
                stripe.Transfer.create_reversal(
                    row.transfer_id, amount=take,
                    # Keyed on the transfer's state at Stripe: the same step
                    # repeated is the same request, the next step is a new one.
                    idempotency_key=f"reversal-{row.transfer_id}-{transfer['amount_reversed']}-{take}",
                )
                left -= take
        except Exception as exc:
            s.rollback()
            print(f"[refunds] could not reverse {row.transfer_id} for order {order.id}: {exc}",
                  file=sys.stderr)
            return str(exc)
        if left <= 0:
            s.delete(row)
        else:
            row.amount = round(left / 100.0, 2)
        s.commit()
        return None


# ----------------------------------------------------------------------
# Bringing an order in line with its charge
# ----------------------------------------------------------------------

WON = ("won", "warning_closed", "prevented")


def apply_charge_state(s, stripe, order: Order, charge, dispute_status: Optional[str] = None) -> Optional[str]:
    """Move `order` to match what Stripe says about its charge. Returns a
    short description of what changed, or None. Commits.

    `charge` and `dispute_status` must be what Stripe says right now (see
    check_order), never the body of a webhook event: events arrive late,
    twice and out of order. Given current state this only moves an order
    toward it, and is safe to run any number of times.
    """
    from webapp.marketplace import PLATFORM_FEE_PCT

    if order.payment_provider != "stripe" or order.status not in SETTLED + ("disputed", "refunded"):
        return None
    charged = int(charge["amount"])
    refunded = int(charge["amount_refunded"])
    fully_refunded = bool(charge["refunded"]) or refunded >= charged or dispute_status == "lost"

    if order.status == "refunded" or fully_refunded:
        # Also reached for an order already marked refunded whose reversal
        # was refused or interrupted earlier: try again.
        was = order.status
        problem = _settle_transfer(s, stripe, order, keep=0.0)
        order.status = "refunded"
        s.commit()
        if was == "refunded":
            return None
        why = "the dispute was lost" if dispute_status == "lost" else "refunded in Stripe"
        return f"order {order.id}: {why}" + (f"; reversal refused ({problem})" if problem else "")

    open_dispute = bool(charge["disputed"] or dispute_status) and dispute_status not in WON
    if open_dispute:
        problem = _settle_transfer(s, stripe, order, keep=0.0)
        if order.status == "disputed":
            return None
        order.status = "disputed"
        s.commit()
        return f"order {order.id}: disputed by the buyer" + (
            f"; reversal refused ({problem})" if problem else "")

    changed = None
    if order.status == "disputed":           # and the dispute is no longer open
        order.status = "paid"
        s.commit()
        changed = f"order {order.id}: dispute closed in your favour; the seller will be paid again"

    if refunded > 0:
        # Partial refund. The fee and the seller's share are recomputed on
        # what the buyer ended up paying, and the transfer is brought down to
        # the new share. Both steps are idempotent, so a reversal that failed
        # last time is simply attempted again.
        net = round((charged - refunded) / 100.0, 2)
        new_fee = round(net * PLATFORM_FEE_PCT / 100.0, 2)
        new_share = round(net - new_fee, 2)
        moved = abs(new_share - order.seller_proceeds) >= 0.005
        if moved:
            order.platform_fee, order.seller_proceeds = new_fee, new_share
            s.commit()
        problem = _settle_transfer(s, stripe, order, keep=new_share) if order.status in SETTLED else None
        if moved:
            changed = (f"order {order.id}: ${refunded / 100:.2f} refunded in Stripe; "
                       f"seller share is now ${new_share:.2f}"
                       + (f"; reversal refused ({problem})" if problem else ""))
    return changed


def _dispute_status(stripe, charge) -> Optional[str]:
    if not charge["disputed"]:
        return None
    found = stripe.Dispute.list(charge=charge["id"], limit=1)
    return found["data"][0]["status"] if found["data"] else None


def _own_charge(stripe, order: Order):
    """The charge that paid for this order, fetched through the order's own
    Checkout session. This is the only charge an order is ever judged by."""
    session_obj = stripe.checkout.Session.retrieve(order.payment_ref)
    intent = stripe.PaymentIntent.retrieve(session_obj["payment_intent"])
    return stripe.Charge.retrieve(intent["latest_charge"])


def check_order(s, stripe, order: Order) -> Optional[str]:
    """Ask Stripe about one card order and apply what it says now."""
    if not order.payment_ref:
        return None
    charge = _own_charge(stripe, order)
    return apply_charge_state(s, stripe, order, charge, _dispute_status(stripe, charge))


def handle_event(s, stripe, event) -> Optional[str]:
    """A verified webhook event about a charge or a dispute.

    The event is only a nudge to go and look. It says which charge something
    happened to; what happened is read fresh from Stripe, through the order's
    own session. So a redelivered or out-of-order event cannot move an order
    backwards, and a charge that merely carries this app's transfer_group
    (another integration on the same Stripe account) cannot touch an order
    it did not pay for.
    """
    kind, obj = event["type"], event["data"]["object"]
    if kind == "charge.refunded":
        charge_id, group = obj["id"], obj["transfer_group"]
    elif kind in ("charge.dispute.created", "charge.dispute.closed"):
        charge_id = obj["charge"]
        group = stripe.Charge.retrieve(charge_id)["transfer_group"]
    else:
        return None
    oid = order_id_from_group(group)
    order = s.get(Order, oid) if oid else None
    if order is None or order.payment_provider != "stripe" or not order.payment_ref:
        return None
    charge = _own_charge(stripe, order)
    if charge["id"] != charge_id:
        return None                                   # not this order's charge
    changed = apply_charge_state(s, stripe, order, charge, _dispute_status(stripe, charge))
    if order.status == "paid":
        from webapp import payouts
        payouts.pay_seller_quietly(order.seller_id)     # a won dispute: pay again
    return changed


# ----------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------

def _operator(s):
    return is_operator(s, current_user_id())


RETURN_NOTE = "was refunded; card returned"


def _card_was_returned(s, order: Order) -> bool:
    return s.query(LibraryEvent).filter(
        LibraryEvent.order_id == order.id, LibraryEvent.user_id == order.seller_id,
        LibraryEvent.kind == "added").count() > 0


def _buyers_copy(s, order: Order) -> Optional[InventoryItem]:
    """The row in the buyer's library that this order put there. When the
    seller had one copy the listing's own row moved to the buyer; when they
    had several, the buyer got a new row stamped with the sale's price and
    time."""
    listing = s.get(MarketListing, order.listing_id)
    if listing is None:
        return None
    moved = s.get(InventoryItem, listing.inventory_item_id)
    if moved is not None and moved.user_id == order.buyer_id and moved.quantity > 0:
        return moved
    bought = (s.query(LibraryEvent)
              .filter_by(order_id=order.id, user_id=order.buyer_id, kind="purchased").first())
    if bought is None:
        return None
    rows = (s.query(InventoryItem)
            .filter(InventoryItem.user_id == order.buyer_id, InventoryItem.quantity > 0,
                    InventoryItem.catalog_card_id == listing.catalog_card_id,
                    InventoryItem.acquired_at == bought.occurred_at).all())
    return rows[0] if len(rows) == 1 else None


def needs_attention(s) -> list[dict]:
    """Refunded and disputed orders, for the operator panel. A refunded order
    whose card is still with the buyer can have it handed back."""
    out = []
    for o in (s.query(Order).filter(Order.status.in_(("refunded", "disputed")))
              .order_by(Order.id.desc()).limit(100).all()):
        listing = s.get(MarketListing, o.listing_id)
        returned = _card_was_returned(s, o)
        buyer, seller = s.get(User, o.buyer_id), s.get(User, o.seller_id)
        still_out = s.query(OrderTransfer).filter_by(order_id=o.id).one_or_none()
        out.append({
            "order_id": o.id, "status": o.status, "amount": o.amount,
            "card": listing.catalog_card.name if listing else None,
            "buyer": buyer.email if buyer else "?", "seller": seller.email if seller else "?",
            "card_returned": returned,
            "card_with_buyer": (o.status == "refunded" and not returned
                                and _buyers_copy(s, o) is not None),
            # The reversal was refused: the seller still holds this much.
            "seller_still_holds": still_out.amount if still_out else 0,
        })
    return out


@refunds_bp.route("/api/admin/refunds/check", methods=["POST"])
@verified_required
def check_refunds():
    """Operator only: ask Stripe about every recent card order and bring the
    books in line. For installs Stripe's webhook cannot reach. Also retries
    any reversal that was refused or interrupted before."""
    from webapp import marketplace, payouts

    stripe = marketplace._stripe()   # noqa: SLF001
    if stripe is None:
        return jsonify({"error": "card payments are not set up on this install"}), 409
    s = get_session()
    try:
        if not _operator(s):
            return jsonify({"error": "operator only"}), 403
        since = utcnow() - timedelta(days=LOOKBACK_DAYS)
        unfinished = {row[0] for row in s.query(OrderTransfer.order_id).all()}
        orders = [o for o in (s.query(Order)
                              .filter(Order.payment_provider == "stripe", Order.created_at >= since,
                                      Order.status.in_(SETTLED + ("disputed", "refunded"))).all())
                  # A refunded order is finished unless a transfer is still out.
                  if o.status != "refunded" or o.id in unfinished]
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
    library, once. Both ledgers get a row saying so. Refused if the buyer no
    longer holds the card (they sold or removed it), which a person must
    sort out."""
    s = get_session()
    try:
        if not _operator(s):
            return jsonify({"error": "operator only"}), 403
        order = s.query(Order).filter_by(id=order_id, status="refunded").one_or_none()
        if order is None:
            return jsonify({"error": "no such refunded order"}), 404
        if _card_was_returned(s, order):
            return jsonify({"error": "this order's card was already returned"}), 409
        item = _buyers_copy(s, order)
        if item is None:
            return jsonify({"error": "the buyer no longer holds this card, so it cannot be moved back"}), 409
        if s.query(MarketListing).filter_by(inventory_item_id=item.id, status="active").count():
            return jsonify({"error": "the buyer has this card listed for sale; it must be unlisted first"}), 409
        # What the seller had paid for it, from their own sale record.
        sale = (s.query(LibraryEvent)
                .filter_by(order_id=order.id, user_id=order.seller_id, kind="sold").first())
        cost = sale.unit_cost if sale else None
        common = dict(channel="market", quantity=1, condition=item.condition,
                      image_url=item.image_url, crop_path=item.source_crop_path, order_id=order.id)
        log_event(s, order.buyer_id, item.catalog_card_id, "removed",
                  note=f"order {order.id} was refunded; card returned to the seller", **common)
        log_event(s, order.seller_id, item.catalog_card_id, "added",
                  note=f"order {order.id} {RETURN_NOTE}", **common)
        if item.quantity > 1:
            item.quantity -= 1
            s.add(InventoryItem(
                user_id=order.seller_id, catalog_card_id=item.catalog_card_id, quantity=1,
                condition=item.condition, image_url=item.image_url, acquired_price=cost,
                source_crop_path=item.source_crop_path))
        else:
            item.user_id = order.seller_id
            item.acquired_price = cost
            item.acquired_at = None
        s.commit()
    finally:
        s.close()
    return jsonify({"order_id": order_id, "returned": True})


def refunded_order_ids(s) -> set[int]:
    """Orders whose money went back. Their sold/purchased ledger rows stay
    (the ledger is a record of what happened) but stop counting in totals."""
    return {row[0] for row in s.query(Order.id).filter(Order.status == "refunded").all()}

