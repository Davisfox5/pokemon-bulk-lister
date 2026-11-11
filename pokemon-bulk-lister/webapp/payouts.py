"""Automatic seller payouts through Stripe Connect.

Card payments land in the operator's Stripe account (webapp/marketplace.py).
This module moves each seller's share on to the seller, without anyone
copying numbers into Venmo:

  1. A seller clicks "Set up payouts". They get a Stripe Express account and
     are sent to Stripe's own onboarding to enter their bank details. This
     app never sees them.
  2. When one of their card sales settles, their proceeds are sent to that
     account with a Stripe Transfer. Stripe then pays out to their bank on
     its normal schedule.
  3. Sales that settled before they finished onboarding are paid the moment
     they do. So is anything a transfer failed on earlier ("Pay out now"
     retries, for the operator).

Calls, as documented by Stripe (connect/separate-charges-and-transfers,
api/transfers/create, api/account_links/create, connect/express-accounts):

  Account.create(type="express", capabilities={"transfers": {"requested": True}})
  AccountLink.create(account=, refresh_url=, return_url=, type="account_onboarding")
  Transfer.create(amount=, currency="usd", destination=<acct>,
                  source_transaction=<charge id>, transfer_group=)

source_transaction ties the transfer to the buyer's charge, so it succeeds
even while that money is still pending in the platform balance, and can never
move more than the charge brought in. The charge id comes from the Checkout
session's PaymentIntent (`latest_charge`). Checkout is limited to cards
(marketplace.buy) because Stripe advises against source_transaction for
payment methods that can fail days later.

Stripe now calls Express a legacy account type and steers new platforms to
its newer account configuration. Express is used here because it is the
documented, supported call that needs the least setup; revisit if Stripe
retires it.

Every transfer is an OrderTransfer row. order_id is unique and the row is
committed before Stripe is called, so one order cannot be paid twice even if
the app dies mid-payout (see pay_seller). Manual payouts stay in the Payout
table; a seller's balance subtracts both. Nothing here was run against Stripe itself: test it
in Stripe test mode before relying on it.
"""
from __future__ import annotations

import sys
import threading

from flask import Blueprint, jsonify, redirect, request
from sqlalchemy.exc import IntegrityError

from webapp.auth import current_user_id, is_operator, login_required, verified_required
from webapp.models import (
    Order,
    OrderTransfer,
    SellerPayoutAccount,
    User,
    get_session,
)

payouts_bp = Blueprint("payouts", __name__)

# One payout decision at a time, manual or automatic. The balance check and
# the row that spends it must not interleave with another request's. (A
# process lock is enough: the app is one process.)
payout_lock = threading.Lock()


def _stripe():
    from webapp import marketplace
    return marketplace._stripe()   # noqa: SLF001  one place decides if Stripe is on


def _cents(amount: float) -> int:
    return int(round(amount * 100))


# ----------------------------------------------------------------------
# Connected account status
# ----------------------------------------------------------------------

def refresh_account(s, stripe, account: SellerPayoutAccount) -> SellerPayoutAccount:
    """Re-read the connected account from Stripe and cache what matters."""
    remote = stripe.Account.retrieve(account.stripe_account_id)
    account.transfers_active = remote["capabilities"]["transfers"] == "active"
    account.payouts_enabled = bool(remote["payouts_enabled"])
    account.details_submitted = bool(remote["details_submitted"])
    s.commit()
    return account


def _account_dict(account: SellerPayoutAccount | None, stripe_on: bool) -> dict:
    if not stripe_on:
        return {"available": False, "state": "off",
                "next": "Card payments are off on this install, so there is nothing to pay out."}
    if account is None:
        return {"available": True, "state": "none",
                "next": "Set up payouts to have your share of card sales sent to your bank automatically."}
    if account.transfers_active:
        return {"available": True, "state": "active",
                "next": ("Your share of each card sale is sent to your Stripe account automatically. "
                         + ("Stripe pays it out to your bank on its schedule."
                            if account.payouts_enabled else
                            "Stripe still needs a bank account from you before it can pay out to your bank."))}
    return {"available": True, "state": "incomplete",
            "next": "Stripe needs more details before it can pay you. Continue setup to finish."}


# ----------------------------------------------------------------------
# Paying sellers
# ----------------------------------------------------------------------

PENDING = "pending"   # OrderTransfer.transfer_id before Stripe has answered


def _nothing_moved(exc: Exception) -> bool:
    """True only when Stripe itself answered "no" to the request (a 4xx:
    bad parameters, missing capability, wrong key). Then no transfer exists.
    A timeout, a dropped connection or a 5xx proves nothing either way."""
    try:
        import stripe as sdk
    except ImportError:
        return False
    return isinstance(exc, (sdk.InvalidRequestError, sdk.AuthenticationError, sdk.PermissionError))


def _reconcile_pending(s, stripe, seller_id: int) -> None:
    """Settle claims left behind by a crash or a failed write.

    A pending row means "a transfer for this order may or may not exist at
    Stripe". Ask Stripe, which is the only side that knows: if the transfer
    is there, record its id; if not, drop the claim so the order can be paid.
    Until that is settled the claim counts as paid, so nothing is sent twice.
    """
    rows = (s.query(OrderTransfer).join(Order, OrderTransfer.order_id == Order.id)
            .filter(Order.seller_id == seller_id, OrderTransfer.transfer_id == PENDING).all())
    for row in rows:
        found = stripe.Transfer.list(transfer_group=f"order-{row.order_id}", limit=10)
        # A transfer that was fully reversed (refund, dispute) is history,
        # not the payment this claim stands for.
        match = [t for t in found["data"]
                 if str(t["metadata"]["order_id"]) == str(row.order_id) and not t["reversed"]]
        if match:
            row.transfer_id = match[0]["id"]
            row.amount = round(int(match[0]["amount"]) / 100.0, 2)
        else:
            s.delete(row)
        s.commit()


def pay_seller(seller_id: int) -> dict:
    """Transfer everything owed to a seller for settled card orders.

    Safe to call at any time and from anywhere: it does nothing unless the
    seller has an active connected account and a positive balance, pays each
    order at most once, and never more than the balance (so money the
    operator already paid by hand is not sent again).

    Order of operations per order, chosen so no failure can pay twice:
      1. Commit a pending OrderTransfer row. order_id is unique, so this is
         the claim; from here the amount counts as paid in seller_balance.
      2. Ask Stripe for the transfer.
      3. Stripe said yes: store the transfer id on the claim.
         Stripe said no (a 4xx): delete the claim; the order is still owed.
         Anything else (a timeout, a 5xx, a crash): the claim stays pending,
         and _reconcile_pending asks Stripe what really happened next time:
         transfer found, record it; not found, release the claim.
    """
    from webapp.marketplace import seller_balance

    stripe = _stripe()
    out = {"transferred": 0.0, "orders": 0, "errors": []}
    if stripe is None:
        return out
    with payout_lock:
        s = get_session()
        try:
            account = s.query(SellerPayoutAccount).filter_by(user_id=seller_id).one_or_none()
            if account is None or not account.transfers_active:
                return out
            try:
                _reconcile_pending(s, stripe, seller_id)
            except Exception as exc:
                s.rollback()
                out["errors"].append(f"could not check earlier transfers with Stripe: {exc}")
                return out      # do not send anything while the past is unclear
            remaining = seller_balance(s, seller_id)["balance"]
            claimed = {row[0] for row in s.query(OrderTransfer.order_id).all()}
            orders = (s.query(Order)
                      .filter(Order.seller_id == seller_id, Order.payment_provider == "stripe",
                              Order.status.in_(("paid", "completed")))
                      .order_by(Order.id.asc()).all())
            for order in orders:
                if remaining < 0.005:
                    break
                if order.id in claimed or not order.payment_ref:
                    continue
                amount = round(min(order.seller_proceeds, remaining), 2)
                if amount <= 0:
                    continue
                claim = OrderTransfer(order_id=order.id, transfer_id=PENDING, amount=amount)
                s.add(claim)
                try:
                    s.commit()
                except IntegrityError:
                    s.rollback()          # claimed by another worker
                    continue
                except Exception as exc:
                    s.rollback()          # nothing was sent; try again later
                    out["errors"].append(f"order {order.id}: could not record the claim: {exc}")
                    continue
                try:
                    session_obj = stripe.checkout.Session.retrieve(order.payment_ref)
                    intent = stripe.PaymentIntent.retrieve(session_obj["payment_intent"])
                    # An order can be paid again after a dispute is won, so
                    # the key carries how many transfers were made and fully
                    # taken back before. A transfer whose answer never reached
                    # us is not reversed, so it does not change the key: asking
                    # again returns that same transfer, never a second one.
                    earlier = sum(1 for t in stripe.Transfer.list(
                        transfer_group=f"order-{order.id}", limit=20)["data"] if t["reversed"])
                    transfer = stripe.Transfer.create(
                        amount=_cents(amount), currency="usd",
                        destination=account.stripe_account_id,
                        source_transaction=intent["latest_charge"],
                        transfer_group=f"order-{order.id}",
                        metadata={"order_id": str(order.id), "seller_id": str(seller_id)},
                        # One key per payment of an order, whatever the
                        # amount: a repeat of this request can only ever
                        # return the same transfer.
                        idempotency_key=f"transfer-order-{order.id}-n{earlier}",
                    )
                except Exception as exc:
                    if _nothing_moved(exc):
                        s.delete(claim)      # Stripe refused: the order is still owed
                        s.commit()
                    # Any other error does not prove nothing moved: a timeout
                    # can hide a transfer Stripe did make. The claim stays
                    # pending, and the next run asks Stripe before retrying
                    # (found: record it; not found: release the claim).
                    out["errors"].append(f"order {order.id}: {exc}")
                    print(f"[payouts] transfer for order {order.id} failed: {exc}", file=sys.stderr)
                    continue
                remaining = round(remaining - amount, 2)
                out["transferred"] = round(out["transferred"] + amount, 2)
                out["orders"] += 1
                try:
                    claim.transfer_id = transfer["id"]
                    s.commit()
                except Exception as exc:
                    # The money moved; only the id failed to save. The claim
                    # stays pending (and counted) and is reconciled later.
                    s.rollback()
                    print(f"[payouts] order {order.id}: transfer {transfer['id']} sent but "
                          f"not recorded ({exc}); will reconcile", file=sys.stderr)
        finally:
            s.close()
    return out


def pay_seller_quietly(seller_id: int) -> None:
    """For use right after a sale settles: a payout problem must never undo
    or block the sale itself. Failures are logged and retried later."""
    try:
        pay_seller(seller_id)
    except Exception as exc:
        print(f"[payouts] seller {seller_id}: {exc}", file=sys.stderr)


# ----------------------------------------------------------------------
# Seller routes
# ----------------------------------------------------------------------

@payouts_bp.route("/api/payouts/account")
@login_required
def payout_account():
    """The signed-in seller's payout setup, refreshed from Stripe."""
    stripe = _stripe()
    uid = current_user_id()
    s = get_session()
    try:
        account = s.query(SellerPayoutAccount).filter_by(user_id=uid).one_or_none()
        if stripe is not None and account is not None:
            try:
                refresh_account(s, stripe, account)
            except Exception as exc:
                s.rollback()
                print(f"[payouts] refresh account for user {uid}: {exc}", file=sys.stderr)
        out = _account_dict(account, stripe is not None)
    finally:
        s.close()
    return jsonify(out)


@payouts_bp.route("/api/payouts/connect", methods=["POST"])
@verified_required
def payout_connect():
    """Create the seller's connected account if needed and return a Stripe
    onboarding link to send them to."""
    stripe = _stripe()
    if stripe is None:
        return jsonify({"error": "card payments are not set up on this install"}), 409
    uid = current_user_id()
    s = get_session()
    try:
        account = s.query(SellerPayoutAccount).filter_by(user_id=uid).one_or_none()
        if account is None:
            user = s.get(User, uid)
            try:
                remote = stripe.Account.create(
                    type="express", email=user.email,
                    capabilities={"transfers": {"requested": True}},
                    metadata={"user_id": str(uid)},
                )
            except Exception as exc:
                return jsonify({"error": f"Stripe could not create a payout account: {exc}"}), 502
            account = SellerPayoutAccount(user_id=uid, stripe_account_id=remote["id"])
            s.add(account)
            try:
                s.commit()
            except IntegrityError:    # a second click got there first
                s.rollback()
                account = s.query(SellerPayoutAccount).filter_by(user_id=uid).one()
        base = request.host_url.rstrip("/")
        try:
            link = stripe.AccountLink.create(
                account=account.stripe_account_id, type="account_onboarding",
                refresh_url=f"{base}/payouts/connect/refresh",
                return_url=f"{base}/payouts/connect/return",
            )
        except Exception as exc:
            return jsonify({"error": f"Stripe could not start onboarding: {exc}"}), 502
        out = {"url": link["url"]}
    finally:
        s.close()
    return jsonify(out)


@payouts_bp.route("/payouts/connect/return")
@login_required
def payout_connect_return():
    """Stripe sends the seller here when they leave onboarding, finished or
    not. Read the real state from Stripe, and pay anything already owed."""
    stripe = _stripe()
    uid = current_user_id()
    state = "incomplete"
    s = get_session()
    try:
        account = s.query(SellerPayoutAccount).filter_by(user_id=uid).one_or_none()
        if stripe is not None and account is not None:
            try:
                refresh_account(s, stripe, account)
                state = "active" if account.transfers_active else "incomplete"
            except Exception as exc:
                s.rollback()
                print(f"[payouts] connect return for user {uid}: {exc}", file=sys.stderr)
                state = "unknown"
    finally:
        s.close()
    if state == "active":
        pay_seller_quietly(uid)
    return redirect(f"/portfolio?payouts={state}")


@payouts_bp.route("/payouts/connect/refresh")
@login_required
def payout_connect_refresh():
    """Stripe sends the seller here when an onboarding link has expired. The
    page offers a fresh one; nothing is changed by this GET."""
    return redirect("/portfolio?payouts=retry")


# ----------------------------------------------------------------------
# Operator
# ----------------------------------------------------------------------

@payouts_bp.route("/api/admin/payouts/run", methods=["POST"])
@verified_required
def run_payouts():
    """Operator only: try to pay every seller who is owed money and has
    finished payout setup. Used to retry after a failure."""
    stripe = _stripe()
    if stripe is None:
        return jsonify({"error": "card payments are not set up on this install"}), 409
    s = get_session()
    try:
        if not is_operator(s, current_user_id()):
            return jsonify({"error": "operator only"}), 403
        accounts = s.query(SellerPayoutAccount).all()
        for account in accounts:
            try:
                refresh_account(s, stripe, account)
            except Exception as exc:
                s.rollback()
                print(f"[payouts] refresh {account.stripe_account_id}: {exc}", file=sys.stderr)
        seller_ids = [a.user_id for a in accounts if a.transfers_active]
    finally:
        s.close()
    total, orders, errors = 0.0, 0, []
    for sid in seller_ids:
        result = pay_seller(sid)
        total += result["transferred"]
        orders += result["orders"]
        errors += result["errors"]
    return jsonify({"transferred": round(total, 2), "orders": orders,
                    "sellers": len(seller_ids), "errors": errors[:10]})
