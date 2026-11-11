"""Read sold orders back from eBay (Sell Fulfillment API, getOrders).

Contract taken from eBay's published OpenAPI document for the Fulfillment API
(sell_fulfillment_v1_oas3.json):

  GET {base}/sell/fulfillment/v1/order
      ?filter=creationdate:[<ISO8601 UTC>..]&limit=<=200&offset=N
  scope: https://api.ebay.com/oauth/api_scope/sell.fulfillment (or .readonly)
  response: {orders: [...], total, next, ...}
  order:    orderId, creationDate, orderPaymentStatus, cancelStatus.cancelState,
            totalMarketplaceFee{value}, lineItems[]
  line:     lineItemId, sku, title, quantity, legacyItemId,
            lineItemCost{value}  (unit price x quantity, before discounts)
            refunds[]{amount{value}}  (empty unless the seller refunded)

Each line item becomes one flat "sale line" dict, which is what the rest of
the app works with. Nothing here writes to eBay.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

from lib.ebay_oauth import SALES_SCOPE, EbayUserAuth

PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"
PAGE_SIZE = 200     # the API's maximum
MAX_PAGES = 50      # 10,000 orders per sync is a bug, not a backlog


class EbayOrdersError(RuntimeError):
    pass


class EbaySalesScopeMissing(EbayOrdersError):
    """The saved authorization predates sales read-back. Reconnect eBay."""


def _money(amount: Optional[dict]) -> Optional[float]:
    try:
        return float((amount or {}).get("value"))
    except (TypeError, ValueError):
        return None


def _when(value: Optional[str]) -> Optional[datetime]:
    """eBay timestamps are ISO 8601 UTC ('2026-11-01T08:25:43.511Z'). Stored
    naive-UTC like every other datetime in the app."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def order_lines(order: dict) -> list[dict]:
    """Flatten one eBay order into sale lines.

    The marketplace fee is reported per order, so it is shared across the
    order's lines in proportion to each line's cost.
    """
    lines = order.get("lineItems") or []
    costs = [(_money(li.get("lineItemCost")) or 0.0) for li in lines]
    order_cost = sum(costs)
    order_fee = _money(order.get("totalMarketplaceFee"))
    payment = order.get("orderPaymentStatus")
    cancelled = (order.get("cancelStatus") or {}).get("cancelState") == "CANCELED"
    out = []
    for li, cost in zip(lines, costs):
        qty = max(1, int(li.get("quantity") or 1))
        fee = None
        if order_fee is not None:
            fee = round(order_fee * (cost / order_cost), 2) if order_cost else 0.0
        # Money given back on this line (a partial refund leaves the order
        # PARTIALLY_REFUNDED and lists each refund here).
        currency = (li.get("lineItemCost") or {}).get("currency")
        refunded = round(sum(
            (_money(r.get("amount")) or 0.0) for r in (li.get("refunds") or [])
            # Only money in the same currency as the price can be netted off it.
            if (r.get("amount") or {}).get("currency") in (None, currency)), 2)
        # Refunds can include shipping and tax the price never did, so cap at
        # the price. A line refunded for at least its price did not sell.
        line_void = cost > 0 and refunded >= cost
        refunded = min(refunded, cost)
        out.append({
            "refunded": refunded,
            "external_id": f"{order.get('orderId')}:{li.get('lineItemId')}",
            "order_id": order.get("orderId"),
            "sku": (li.get("sku") or "").strip() or None,
            "title": li.get("title"),
            "listing_id": li.get("legacyItemId"),
            "quantity": qty,
            "unit_price": round(cost / qty, 2),
            "fees": fee,
            "sold_at": _when(order.get("creationDate")),
            # Status values (PAID, PARTIALLY_REFUNDED, FULLY_REFUNDED, FAILED,
            # PENDING; cancelState CANCELED) are eBay's documented enums but
            # are not spelled out in the OpenAPI file, so they have not been
            # checked against a live response. An unknown value is treated as
            # "not paid yet" and simply waits for the next sync.
            "paid": payment in ("PAID", "PARTIALLY_REFUNDED"),
            # Money did not stay with the seller: nothing to record as a sale,
            # and a sale recorded earlier is reversed. A FAILED payment is
            # not final (the buyer can pay again), so it only counts as
            # "not paid yet".
            "void": cancelled or payment == "FULLY_REFUNDED" or line_void,
            "payment_status": payment,
        })
    return out


def fetch_sale_lines(auth: EbayUserAuth, since: Optional[datetime] = None,
                     session: Optional[Any] = None, timeout: int = 30) -> list[dict]:
    """Every order line created since `since` (default: 30 days ago; eBay
    serves at most two years)."""
    if not auth.has_scope(SALES_SCOPE):
        raise EbaySalesScopeMissing(
            "this eBay connection was made before sales could be read back; "
            "disconnect and connect eBay again to allow it")
    since = since or (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None)
    stamp = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    base = SANDBOX_BASE if auth.is_sandbox else PRODUCTION_BASE
    http = session or requests.Session()

    lines: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        resp = http.get(
            f"{base}/sell/fulfillment/v1/order",
            headers={"Authorization": f"Bearer {auth.access_token()}", "Accept": "application/json"},
            params={"filter": f"creationdate:[{stamp}..]", "limit": PAGE_SIZE, "offset": offset},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise EbayOrdersError(f"eBay getOrders failed {resp.status_code}: {resp.text[:300]}")
        page = resp.json()
        orders = page.get("orders") or []
        for order in orders:
            lines.extend(order_lines(order))
        offset += len(orders)
        if not orders or offset >= int(page.get("total") or 0):
            break
    return lines
