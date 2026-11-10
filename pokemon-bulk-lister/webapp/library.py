"""Library ledger: everything a user has added, bought and sold.

Inventory (webapp/catalog.py) answers "what do I hold now". This module
answers "what have I sold" and "what have I bought", from the library_events
table, and adds the two ways cards move without the scan bench or the
on-platform marketplace: entering a card by hand, and recording a sale that
happened somewhere else (eBay, a card show, a trade night).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import Blueprint, abort, jsonify, request

from webapp.auth import current_user_id, login_required
from webapp.catalog import _item_dict, release_listings
from webapp.models import (
    EVENT_CHANNELS,
    CatalogCard,
    InventoryItem,
    LibraryEvent,
    MarketListing,
    Order,
    get_or_create_catalog_card,
    get_session,
    log_event,
)

library_bp = Blueprint("library", __name__)

CONDITIONS = ("NM", "LP", "MP", "HP", "DMG")


def _parse_date(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _money(value, field: str) -> Optional[float]:
    """Parse an optional non-negative dollar amount; raises ValueError with a
    message fit to show the user."""
    if value in (None, ""):
        return None
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if amount < 0:
        raise ValueError(f"{field} cannot be negative")
    return amount


def _quantity(value) -> int:
    """Missing means 1. Anything else must be a whole number (range is the
    caller's check, so 0 is rejected rather than quietly turned into 1)."""
    if value in (None, ""):
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("quantity must be a whole number")


def _event_dict(ev: LibraryEvent) -> dict:
    cc = ev.catalog_card
    gross = round((ev.unit_price or 0) * ev.quantity, 2) if ev.unit_price is not None else None
    net = round(gross - (ev.fees or 0), 2) if gross is not None else None
    gain = None
    if ev.kind == "sold" and net is not None and ev.unit_cost is not None:
        gain = round(net - ev.unit_cost * ev.quantity, 2)
    return {
        "id": ev.id,
        "kind": ev.kind,
        "channel": ev.channel,
        "catalog_card_id": cc.id,
        "name": cc.name,
        "set_name": cc.set_name,
        "set_code": cc.set_code,
        "card_number": cc.card_number,
        "is_holo": cc.is_holo,
        "condition": ev.condition,
        "quantity": ev.quantity,
        "unit_price": ev.unit_price,
        "unit_cost": ev.unit_cost,
        "fees": ev.fees,
        "total": gross,
        "net": net,
        "gain": gain,
        "image_url": ev.image_url or cc.image_url,
        "crop_path": ev.crop_path,
        "note": ev.note,
        "order_id": ev.order_id,
        "occurred_at": ev.occurred_at.isoformat() + "Z",
        "market_price_now": cc.final_price,
    }


def backfill_order_events() -> int:
    """Write ledger rows for marketplace orders paid before the ledger
    existed. Idempotent: an order that already has events is skipped."""
    s = get_session()
    created = 0
    try:
        logged = {row[0] for row in s.query(LibraryEvent.order_id)
                  .filter(LibraryEvent.order_id.isnot(None)).distinct().all()}
        orders = s.query(Order).filter(Order.status.in_(("paid", "completed"))).all()
        for o in orders:
            if o.id in logged:
                continue
            listing = s.get(MarketListing, o.listing_id)
            if listing is None:
                continue
            item = s.get(InventoryItem, listing.inventory_item_id)
            common = dict(
                channel="market", unit_price=o.amount, order_id=o.id,
                condition=item.condition if item else None,
                image_url=item.image_url if item else None,
                crop_path=item.source_crop_path if item else None,
                occurred_at=o.paid_at or o.created_at,
            )
            log_event(s, o.seller_id, listing.catalog_card_id, "sold",
                      fees=o.platform_fee, **common)
            log_event(s, o.buyer_id, listing.catalog_card_id, "purchased", **common)
            created += 2
        s.commit()
    finally:
        s.close()
    return created


# ----------------------------------------------------------------------
# History
# ----------------------------------------------------------------------

@library_bp.route("/api/library/events")
@login_required
def list_events():
    """?kind=sold|purchased|added (omit for everything)."""
    kind = request.args.get("kind")
    uid = current_user_id()
    s = get_session()
    try:
        q = s.query(LibraryEvent).filter_by(user_id=uid)
        if kind:
            q = q.filter_by(kind=kind)
        events = q.order_by(LibraryEvent.occurred_at.desc(), LibraryEvent.id.desc()).limit(2000).all()
        rows = [_event_dict(e) for e in events]

        all_events = s.query(LibraryEvent).filter_by(user_id=uid).all()
        totals = {
            "sold_cards": 0, "sold_gross": 0.0, "sold_fees": 0.0, "sold_net": 0.0,
            "realized_gain": 0.0, "sold_with_cost": 0,
            "purchased_cards": 0, "purchased_spend": 0.0, "purchased_unpriced": 0,
            "added_cards": 0,
        }
        for e in all_events:
            gross = (e.unit_price or 0) * e.quantity
            if e.kind == "sold":
                totals["sold_cards"] += e.quantity
                totals["sold_gross"] += gross
                totals["sold_fees"] += e.fees or 0
                if e.unit_cost is not None:
                    totals["sold_with_cost"] += e.quantity
                    totals["realized_gain"] += gross - (e.fees or 0) - e.unit_cost * e.quantity
            elif e.kind == "purchased":
                totals["purchased_cards"] += e.quantity
                totals["purchased_spend"] += gross
                if e.unit_price is None:
                    totals["purchased_unpriced"] += e.quantity
            elif e.kind == "added":
                totals["added_cards"] += e.quantity
        totals["sold_net"] = totals["sold_gross"] - totals["sold_fees"]
        totals = {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}
    finally:
        s.close()
    return jsonify({"events": rows, "totals": totals})


# ----------------------------------------------------------------------
# Record a sale made somewhere else
# ----------------------------------------------------------------------

@library_bp.route("/api/inventory/<int:item_id>/record-sale", methods=["POST"])
@login_required
def record_sale(item_id: int):
    """Body: {price, quantity?, channel?, fees?, sold_on?, note?}. `price` is
    per card. Takes the copies out of the library and writes a 'sold' event."""
    body = request.get_json(silent=True) or {}
    try:
        price = _money(body.get("price"), "price")
        fees = _money(body.get("fees"), "fees")
        quantity = _quantity(body.get("quantity"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if price is None:
        return jsonify({"error": "enter the price each card sold for (0 for a giveaway)"}), 400
    channel = body.get("channel") or "other"
    if channel not in EVENT_CHANNELS:
        return jsonify({"error": f"channel must be one of {EVENT_CHANNELS}"}), 400

    uid = current_user_id()
    s = get_session()
    try:
        item = s.query(InventoryItem).filter_by(id=item_id, user_id=uid).one_or_none()
        if item is None or item.quantity <= 0:
            abort(404)
        if quantity < 1 or quantity > item.quantity:
            return jsonify({"error": f"quantity must be between 1 and {item.quantity}"}), 400
        if quantity == item.quantity:
            blocked = release_listings(s, item)
            if blocked:
                return jsonify({"error": blocked}), 409
        ev = log_event(
            s, uid, item.catalog_card_id, "sold",
            channel=channel, quantity=quantity, unit_price=price,
            unit_cost=item.acquired_price, fees=fees, condition=item.condition,
            image_url=item.image_url, crop_path=item.source_crop_path,
            note=body.get("note"), occurred_at=_parse_date(body.get("sold_on")),
        )
        # The row stays at quantity 0 rather than being deleted: it keeps a
        # re-import of the same scan from putting a sold card back.
        item.quantity -= quantity
        s.commit()
        out = {"event": _event_dict(ev), "remaining": item.quantity}
    finally:
        s.close()
    return jsonify(out)


# ----------------------------------------------------------------------
# Add a card by hand (bought elsewhere, or already owned)
# ----------------------------------------------------------------------

@library_bp.route("/api/inventory/add", methods=["POST"])
@login_required
def add_card():
    """Body: {name, set_name?, set_code?, card_number?, rarity?, is_holo?,
    quantity?, condition?, price?, purchased?, channel?, acquired_on?, note?}.

    A card with a price, or with purchased:true, is logged as a purchase and
    the price becomes its cost basis. Otherwise it is logged as added.
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "card name is required"}), 400
    try:
        price = _money(body.get("price"), "price")
        quantity = _quantity(body.get("quantity"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if quantity < 1 or quantity > 999:
        return jsonify({"error": "quantity must be between 1 and 999"}), 400
    condition = str(body.get("condition") or "NM").upper()
    if condition not in CONDITIONS:
        return jsonify({"error": f"condition must be one of {CONDITIONS}"}), 400
    purchased = bool(body.get("purchased")) or price is not None
    channel = body.get("channel") or "manual"
    if channel not in EVENT_CHANNELS:
        return jsonify({"error": f"channel must be one of {EVENT_CHANNELS}"}), 400
    when = _parse_date(body.get("acquired_on"))

    uid = current_user_id()
    s = get_session()
    try:
        cc = get_or_create_catalog_card(s, {
            "name": name,
            "set_name": (body.get("set_name") or "").strip() or None,
            "set_code": (body.get("set_code") or "").strip().lower() or None,
            "card_number": (body.get("card_number") or "").strip() or None,
            "rarity": (body.get("rarity") or "").strip() or None,
            "is_holo": bool(body.get("is_holo")),
        })
        item = InventoryItem(
            user_id=uid, catalog_card_id=cc.id, quantity=quantity,
            condition=condition, acquired_price=price, acquired_at=when,
            notes=body.get("note") or None,
        )
        s.add(item)
        log_event(
            s, uid, cc.id, "purchased" if purchased else "added",
            channel=channel, quantity=quantity, unit_price=price,
            condition=condition, note=body.get("note"), occurred_at=when,
        )
        s.commit()
        out = _item_dict(s, item)
        out["needs_price"] = cc.final_price is None
    finally:
        s.close()
    return jsonify(out)


@library_bp.route("/api/catalog/search")
@login_required
def catalog_search():
    """Name autocomplete over cards the catalog already knows, so a hand-entered
    card lands on the same catalog row (and price) as a scanned one."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"cards": []})
    s = get_session()
    try:
        rows = (s.query(CatalogCard)
                .filter(CatalogCard.name.ilike(f"%{q}%"))
                .order_by(CatalogCard.name).limit(20).all())
        cards = [{
            "name": c.name, "set_name": c.set_name, "set_code": c.set_code,
            "card_number": c.card_number, "rarity": c.rarity, "is_holo": c.is_holo,
            "price": c.final_price,
        } for c in rows]
    finally:
        s.close()
    return jsonify({"cards": cards})
