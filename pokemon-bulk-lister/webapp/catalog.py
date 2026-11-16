"""Inventory endpoints: promote bench cards into a user's library + CRUD.

"Promote" is the bridge between the single-user processing bench (webapp/db.py
`cards` table) and the multi-user layer: each identified bench card becomes
(a) a canonical catalog_cards row shared by everyone, seeded with the bench's
pricing as the first history snapshot, and (b) an inventory_items row owned by
the promoting user.
"""
from __future__ import annotations

import math
import os
from datetime import timedelta
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

from webapp import db as bench_db
from webapp.auth import current_user_id, login_required
from webapp.models import (
    CatalogCard,
    InventoryItem,
    LibraryEvent,
    MarketListing,
    Order,
    get_or_create_catalog_card,
    get_session,
    log_event,
    price_at,
    record_price,
    utcnow,
)

from webapp.runtime import DATA_ROOT

ROOT = Path(__file__).resolve().parent.parent
BENCH_DB_PATH = str(DATA_ROOT / os.getenv("OUTPUT_DIR", "output") / "db.sqlite")

# Prices under this confidence get the "needs a human look" treatment.
LOW_CONFIDENCE = float(os.getenv("LOW_CONFIDENCE", "0.5"))

# A scanned card is added to the library by itself only above this
# identification confidence (the catalog confirmed it, or a person did).
AUTO_ADD_ABOVE = 0.6

catalog_bp = Blueprint("catalog", __name__)


@catalog_bp.route("/api/inventory/promote", methods=["POST"])
@login_required
def promote():
    """Body: {"card_ids": [..]} or {"all_priced": true}. With "auto": true
    (the Scan page adding cards by itself as they are priced) a card whose
    name the catalog has not confirmed is held back: a person adds it, or
    settles which card it is first."""
    body = request.get_json(silent=True) or {}
    uid = current_user_id()
    auto = body.get("auto") is True

    with bench_db.connect(BENCH_DB_PATH) as conn:
        if body.get("all_priced"):
            bench_cards = [
                c for c in bench_db.list_cards(conn, sort="newest")
                if c.get("name") and c.get("final_price") is not None
            ]
        else:
            ids = body.get("card_ids") or []
            if not isinstance(ids, list) or not ids:
                return jsonify({"error": "card_ids (list) or all_priced:true required"}), 400
            bench_cards = [c for c in (bench_db.get_card(conn, int(i)) for i in ids) if c]

    if not bench_cards:
        return jsonify({"error": "no matching bench cards"}), 400

    s = get_session()
    promoted, skipped_unidentified, skipped_dup, held = 0, 0, 0, 0
    try:
        for card in bench_cards:
            if not card.get("name"):
                skipped_unidentified += 1
                continue
            if auto and (card.get("final_price") is None or float(card.get("id_confidence") or 0) <= AUTO_ADD_ABOVE):
                held += 1
                continue
            cc = get_or_create_catalog_card(s, card)
            # Seed catalog pricing from the bench only if the catalog has no
            # price yet — repeated promotes must not fabricate history.
            if cc.final_price is None and card.get("final_price") is not None:
                record_price(s, cc, card)

            crop = card.get("crop_path")
            # "Already imported" is judged by the ledger as well as by current
            # holdings: a card sold on the marketplace moves to the buyer's
            # row, and must not come back on the next import.
            dup = crop and (
                s.query(InventoryItem)
                .filter_by(user_id=uid, catalog_card_id=cc.id, source_crop_path=crop)
                .first()
                or s.query(LibraryEvent)
                .filter_by(user_id=uid, kind="added", channel="scan", crop_path=crop)
                .first()
            )
            if dup:
                skipped_dup += 1
                continue

            condition = (card.get("condition_guess") or "NM").upper()
            s.add(InventoryItem(
                user_id=uid,
                catalog_card_id=cc.id,
                quantity=1,
                condition=condition,
                source_crop_path=crop,
                image_url=card.get("image_url"),
            ))
            log_event(
                s, uid, cc.id, "added", channel="scan", condition=condition,
                image_url=card.get("image_url"), crop_path=crop,
            )
            promoted += 1
        s.commit()
    finally:
        s.close()

    return jsonify({
        "promoted": promoted,
        "skipped_unidentified": skipped_unidentified,
        "skipped_duplicates": skipped_dup,
        "held_for_a_look": held,
    })


def in_library_crops(uid: int, crops: list) -> set:
    """Which of these scan photos this account has already added to its
    library (now or ever: a card since sold still counts)."""
    crops = [c for c in crops if c]
    if not uid or not crops:
        return set()
    s = get_session()
    try:
        held = s.query(InventoryItem.source_crop_path).filter(
            InventoryItem.user_id == uid, InventoryItem.source_crop_path.in_(crops)).all()
        logged = s.query(LibraryEvent.crop_path).filter(
            LibraryEvent.user_id == uid, LibraryEvent.kind == "added", LibraryEvent.channel == "scan",
            LibraryEvent.crop_path.in_(crops)).all()
    finally:
        s.close()
    return {row[0] for row in held} | {row[0] for row in logged}


def price_provenance(cc: CatalogCard) -> dict:
    """Where a catalog price came from and how far to trust it. Every screen
    that shows a price shows this next to it."""
    if (cc.pricing_notes or "").startswith("From your spreadsheet"):
        return {"sources": {}, "confidence": 0, "priced_at": None, "outlier": False,
                "notes": "No shared market price yet", "flagged": True}
    sources = {
        "tcgplayer": cc.tcgplayer_market,
        "cardmarket": cc.cardmarket_trend_usd,
        "ebay_30d": cc.ebay_median_30d,
    }
    return {
        "sources": {k: v for k, v in sources.items() if v is not None},
        "confidence": cc.pricing_confidence,
        "priced_at": cc.last_priced_at.isoformat() + "Z" if cc.last_priced_at else None,
        "outlier": bool(cc.outlier_flag),
        "notes": cc.pricing_notes,
        # Same bar the bench uses for "needs a human look".
        "flagged": bool(
            cc.final_price is not None
            and (cc.outlier_flag or (cc.pricing_confidence or 0) < LOW_CONFIDENCE
                 or "\u26a0" in (cc.pricing_notes or ""))
        ),
    }


def item_valuation(item: InventoryItem) -> tuple:
    """Live catalog price first; an estimate belongs only to its owner."""
    cc = item.catalog_card
    provenance = price_provenance(cc)
    price = cc.final_price
    legacy = (cc.pricing_notes or "").startswith("From your spreadsheet")
    if legacy:
        price = None  # Old shared estimates have no reliable owner attribution.
        provenance = {**provenance, "priced_at": None, "sources": {}, "notes": None, "flagged": False}
    saved = item.imported_value
    if price is None and saved:
        price = saved.price
        provenance = {"sources": {}, "confidence": 0.4, "outlier": False, "flagged": True,
                      "priced_at": saved.updated_at.isoformat() + "Z",
                      "notes": "From your spreadsheet" + (f" ({saved.source})" if saved.source else ""),
                      "estimate": True}
    return price, provenance


def _item_dict(s, item: InventoryItem) -> dict:
    cc = item.catalog_card
    price, provenance = item_valuation(item)
    week_ago = price_at(s, cc.id, utcnow() - timedelta(days=7))
    change_7d = None
    if price is not None and week_ago and not provenance.get("estimate"):
        change_7d = round((price - week_ago) / week_ago * 100, 1)
    return {
        "id": item.id,
        "catalog_card_id": cc.id,
        "name": cc.name,
        "set_name": cc.set_name,
        "set_code": cc.set_code,
        "card_number": cc.card_number,
        "rarity": cc.rarity,
        "is_holo": cc.is_holo,
        "printing": cc.printing,
        "quantity": item.quantity,
        "condition": item.condition,
        "acquired_price": item.acquired_price,
        "notes": item.notes,
        "image_url": item.image_url or cc.image_url,
        "crop_path": item.source_crop_path,
        "price": price,
        "value": round(price * item.quantity, 2) if price is not None else None,
        "pricing_confidence": provenance["confidence"],
        "last_priced_at": provenance["priced_at"],
        "change_7d_pct": change_7d,
        "tcgplayer_url": cc.tcgplayer_url,
        "provenance": provenance,
    }


@catalog_bp.route("/api/inventory", methods=["GET"])
@login_required
def list_inventory():
    s = get_session()
    try:
        items = (
            s.query(InventoryItem)
            .filter(InventoryItem.user_id == current_user_id(), InventoryItem.quantity > 0)
            .join(CatalogCard)
            .all()
        )
        payload = [_item_dict(s, i) for i in items]
        from webapp import runtime
        runtime.add_photo_urls(payload)
        from webapp.listing_import import listed_on
        elsewhere = listed_on(s, [i.id for i in items])
        for row in payload:
            row["listed_on"] = elsewhere.get(row["id"], [])
    finally:
        s.close()
    payload.sort(key=lambda d: (d["value"] is None, -(d["value"] or 0)))
    return jsonify({"items": payload})


@catalog_bp.route("/api/inventory/<int:item_id>", methods=["PATCH"])
@login_required
def patch_inventory(item_id: int):
    patch = request.get_json(silent=True) or {}
    s = get_session()
    try:
        item = s.query(InventoryItem).filter_by(id=item_id, user_id=current_user_id()).one_or_none()
        if item is None:
            abort(404)
        if "quantity" in patch:
            try:
                quantity = int(patch["quantity"])
            except (TypeError, ValueError):
                return jsonify({"error": "quantity must be a whole number"}), 400
            if quantity < 0 or quantity > 9999:
                return jsonify({"error": "quantity must be between 0 and 9999"}), 400
            if quantity == 0:
                # An empty row cannot stay listed: a buyer would pay for nothing.
                blocked = release_listings(s, item)
                if blocked:
                    return jsonify({"error": blocked}), 409
            # A hand edit still goes in the ledger, so the history adds up:
            # copies taken out are 'removed', copies put in are 'added'.
            delta = quantity - item.quantity
            if delta:
                log_event(
                    s, item.user_id, item.catalog_card_id,
                    "added" if delta > 0 else "removed",
                    channel="manual", quantity=abs(delta), condition=item.condition,
                    image_url=item.image_url, crop_path=item.source_crop_path,
                    note="quantity edited",
                )
            item.quantity = quantity
        if "condition" in patch:
            item.condition = str(patch["condition"]).upper()[:20]
        if "notes" in patch:
            item.notes = str(patch["notes"])[:2000] if patch["notes"] not in (None, "") else None
        if "acquired_price" in patch:
            raw = patch["acquired_price"]
            try:
                paid = float(raw) if raw not in (None, "") else None
            except (TypeError, ValueError):
                return jsonify({"error": "price paid must be a number"}), 400
            if paid is not None and not (math.isfinite(paid) and 0 <= paid <= 1_000_000):
                return jsonify({"error": "price paid must be between 0 and 1,000,000"}), 400
            item.acquired_price = paid
        s.commit()
        out = _item_dict(s, item)
    finally:
        s.close()
    return jsonify(out)


@catalog_bp.route("/api/inventory/<int:item_id>", methods=["DELETE"])
@login_required
def delete_inventory(item_id: int):
    """Remove a card from the library (a correction, not a sale; use
    record-sale for that). Active listings on it are cancelled. If order
    history points at the row it is emptied instead of deleted, so past
    sales and purchases stay intact."""
    s = get_session()
    try:
        item = s.query(InventoryItem).filter_by(id=item_id, user_id=current_user_id()).one_or_none()
        if item is None:
            abort(404)
        blocked = release_listings(s, item)
        if blocked:
            return jsonify({"error": blocked}), 409
        if item.quantity > 0:
            log_event(
                s, item.user_id, item.catalog_card_id, "removed",
                channel="manual", quantity=item.quantity, condition=item.condition,
                image_url=item.image_url, crop_path=item.source_crop_path,
            )
        if s.query(MarketListing).filter_by(inventory_item_id=item.id).count():
            item.quantity = 0
        else:
            s.delete(item)
        s.commit()
    finally:
        s.close()
    return jsonify({"deleted": item_id})


def release_listings(s, item: InventoryItem) -> str | None:
    """Cancel the item's active marketplace listing. Returns an error string
    (and changes nothing) when a buyer already has a pending order on it."""
    active = s.query(MarketListing).filter_by(inventory_item_id=item.id, status="active").all()
    for listing in active:
        if s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").count():
            return "a buyer has a pending order on this card; settle or wait for it first"
    for listing in active:
        listing.status = "cancelled"
    return None
