"""Portfolio layer: dashboard page, valuation APIs, and alerts.

Selling happens on-platform (webapp/marketplace.py). eBay is a pricing data
source only, via the site operator's credentials — users never link accounts.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request

from webapp import scheduler
from webapp.auth import current_user_id, login_required
from webapp.catalog import _item_dict
from webapp.models import (
    Alert,
    AlertRule,
    CatalogCard,
    InventoryItem,
    PriceSnapshot,
    get_session,
    utcnow,
)

ROOT = Path(__file__).resolve().parent.parent

portfolio_bp = Blueprint("portfolio", __name__)

RULE_KINDS = ("price_above", "price_below", "pct_change", "sell_signal")


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------

@portfolio_bp.route("/portfolio")
@login_required
def portfolio_page():
    return render_template("portfolio.html")


# ----------------------------------------------------------------------
# Valuation
# ----------------------------------------------------------------------

@portfolio_bp.route("/api/portfolio/summary")
@login_required
def summary():
    uid = current_user_id()
    s = get_session()
    try:
        items = (s.query(InventoryItem)
                 .filter(InventoryItem.user_id == uid, InventoryItem.quantity > 0)
                 .join(CatalogCard).all())
        rows = [_item_dict(s, i) for i in items]
        from webapp.listing_import import listed_on
        elsewhere = listed_on(s, [i.id for i in items])
        for row in rows:
            row["listed_on"] = elsewhere.get(row["id"], [])
        total_value = round(sum(r["value"] or 0 for r in rows), 2)
        total_cost = round(
            sum((i.acquired_price or 0) * i.quantity for i in items if i.acquired_price), 2
        )
        unread = s.query(Alert).filter_by(user_id=uid, is_read=False).count()
        priced = sum(1 for r in rows if r["price"] is not None)
    finally:
        s.close()
    rows.sort(key=lambda d: (d["value"] is None, -(d["value"] or 0)))
    return jsonify({
        "items": rows,
        "totals": {
            "value": total_value,
            "cost_basis": total_cost or None,
            "item_count": len(rows),
            "card_count": sum(r["quantity"] for r in rows),
            "priced_count": priced,
            "unread_alerts": unread,
        },
        "scheduler": scheduler.status(),
    })


@portfolio_bp.route("/api/portfolio/history")
@login_required
def history():
    """Daily portfolio value series (last-known price carried forward)."""
    days = min(365, max(7, int(request.args.get("days", 90))))
    uid = current_user_id()
    s = get_session()
    try:
        items = s.query(InventoryItem).filter_by(user_id=uid).all()
        qty_by_cc: dict[int, int] = defaultdict(int)
        for i in items:
            qty_by_cc[i.catalog_card_id] += i.quantity
        ccids = list(qty_by_cc)
        snaps = (
            s.query(PriceSnapshot)
            .filter(PriceSnapshot.catalog_card_id.in_(ccids),
                    PriceSnapshot.final_price.isnot(None))
            .order_by(PriceSnapshot.priced_at.asc())
            .all()
        ) if ccids else []
    finally:
        s.close()

    by_cc: dict[int, list] = defaultdict(list)
    for snap in snaps:
        by_cc[snap.catalog_card_id].append((snap.priced_at, snap.final_price))

    now = utcnow()
    series = []
    for d in range(days, -1, -1):
        day_end = now - timedelta(days=d)
        total = 0.0
        any_price = False
        for ccid, qty in qty_by_cc.items():
            last = None
            for ts, price in by_cc.get(ccid, ()):
                if ts <= day_end:
                    last = price
                else:
                    break
            if last is not None:
                total += last * qty
                any_price = True
        series.append({
            "date": day_end.date().isoformat(),
            "value": round(total, 2) if any_price else None,
        })
    return jsonify({"days": days, "series": series})


@portfolio_bp.route("/api/portfolio/refresh-prices", methods=["POST"])
@login_required
def refresh_prices():
    """Force-refresh the current user's catalog cards in the background."""
    uid = current_user_id()
    s = get_session()
    try:
        ccids = [row[0] for row in s.query(InventoryItem.catalog_card_id)
                 .filter(InventoryItem.user_id == uid, InventoryItem.quantity > 0)
                 .distinct().all()]
    finally:
        s.close()
    if not ccids:
        return jsonify({"error": "no inventory to refresh"}), 400
    threading.Thread(
        target=scheduler.refresh_cards, args=(ccids,), kwargs={"batch_size": len(ccids)},
        daemon=True,
    ).start()
    return jsonify({"started": True, "cards": len(ccids)})


# ----------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------

@portfolio_bp.route("/api/alerts")
@login_required
def list_alerts():
    s = get_session()
    try:
        rows = (
            s.query(Alert)
            .filter_by(user_id=current_user_id())
            .order_by(Alert.created_at.desc())
            .limit(100)
            .all()
        )
        payload = [{
            "id": a.id,
            "kind": a.kind,
            "message": a.message,
            "is_read": a.is_read,
            "created_at": a.created_at.isoformat() + "Z",
        } for a in rows]
    finally:
        s.close()
    return jsonify({"alerts": payload})


@portfolio_bp.route("/api/alerts/read-all", methods=["POST"])
@login_required
def read_all_alerts():
    s = get_session()
    try:
        n = (s.query(Alert)
             .filter_by(user_id=current_user_id(), is_read=False)
             .update({"is_read": True}))
        s.commit()
    finally:
        s.close()
    return jsonify({"marked": n})


@portfolio_bp.route("/api/alert-rules", methods=["GET"])
@login_required
def list_rules():
    s = get_session()
    try:
        rules = s.query(AlertRule).filter_by(user_id=current_user_id()).all()
        payload = []
        for r in rules:
            card = s.get(CatalogCard, r.catalog_card_id) if r.catalog_card_id else None
            payload.append({
                "id": r.id,
                "kind": r.kind,
                "threshold": r.threshold,
                "window_days": r.window_days,
                "active": r.active,
                "catalog_card_id": r.catalog_card_id,
                "card_name": card.name if card else None,
            })
    finally:
        s.close()
    return jsonify({"rules": payload})


@portfolio_bp.route("/api/alert-rules", methods=["POST"])
@login_required
def create_rule():
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if kind not in RULE_KINDS:
        return jsonify({"error": f"kind must be one of {RULE_KINDS}"}), 400
    try:
        threshold = float(body["threshold"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "numeric threshold required"}), 400

    s = get_session()
    try:
        ccid = body.get("catalog_card_id")
        if ccid is not None and s.get(CatalogCard, int(ccid)) is None:
            return jsonify({"error": "unknown catalog_card_id"}), 400
        rule = AlertRule(
            user_id=current_user_id(),
            catalog_card_id=int(ccid) if ccid is not None else None,
            kind=kind,
            threshold=threshold,
            window_days=int(body.get("window_days") or 30),
        )
        s.add(rule)
        s.commit()
        rid = rule.id
    finally:
        s.close()
    return jsonify({"id": rid})


@portfolio_bp.route("/api/alert-rules/<int:rule_id>", methods=["DELETE"])
@login_required
def delete_rule(rule_id: int):
    s = get_session()
    try:
        rule = s.query(AlertRule).filter_by(id=rule_id, user_id=current_user_id()).one_or_none()
        if rule is None:
            abort(404)
        s.delete(rule)
        s.commit()
    finally:
        s.close()
    return jsonify({"deleted": rule_id})
