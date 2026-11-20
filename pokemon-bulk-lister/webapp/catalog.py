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
from typing import Optional

from flask import Blueprint, abort, jsonify, request

from lib import price_notes
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
            item = InventoryItem(
                user_id=uid,
                catalog_card_id=cc.id,
                quantity=1,
                condition=condition,
                source_crop_path=crop,
                image_url=card.get("image_url"),
            )
            s.add(item)
            if card.get("list_price"):
                s.flush()                      # the price the owner set on Scan comes with the card
                set_your_price(s, item.id, card["list_price"])
            if card.get("checked"):
                s.flush()                      # so is the owner's check: not flagged in the Library either
                set_checked(s, item.id)
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
        # In plain words, including notes saved by older versions.
        "notes": price_notes.plain(
            cc.pricing_notes, prices=[("TCGplayer", cc.tcgplayer_market), ("Cardmarket", cc.cardmarket_trend_usd),
                                      ("eBay", cc.ebay_median_30d)],
            using=cc.final_price, outlier=bool(cc.outlier_flag)) or None,
        # Held for a person only by a warning about which card it is. Prices
        # that are far apart, or from one source, are a note beside the
        # price ("caution"), not something to resolve.
        "flagged": bool(cc.final_price is not None and "\u26a0" in (cc.pricing_notes or "")),
        "caution": ("Prices vary" if cc.outlier_flag and len([v for v in sources.values() if v]) >= 2
                    else "Single pricing source" if len([v for v in sources.values() if v]) == 1 else None),
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
                      "notes": "From the imported spreadsheet" + (f" ({saved.source})" if saved.source else ""),
                      "estimate": True}
    return price, provenance


def your_prices(s, item_ids: list) -> dict:
    """{inventory item id: the owner's own price} for the items that have one."""
    from webapp.models import InventoryPrice

    ids = [i for i in item_ids if i]
    if not ids:
        return {}
    return {r.inventory_item_id: r.price
            for r in s.query(InventoryPrice).filter(InventoryPrice.inventory_item_id.in_(ids)).all()}


def checked_items(s, item_ids: list) -> set:
    """The Library cards the owner has saved as right (models.InventoryCheck)."""
    from webapp.models import InventoryCheck

    ids = [i for i in item_ids if i]
    if not ids:
        return set()
    return {r.inventory_item_id
            for r in s.query(InventoryCheck).filter(InventoryCheck.inventory_item_id.in_(ids)).all()}


def set_checked(s, item_id: int) -> None:
    from webapp.models import InventoryCheck

    if s.get(InventoryCheck, item_id) is None:
        s.add(InventoryCheck(inventory_item_id=item_id))


def sync_checked(uid: Optional[int], crop_paths: list) -> int:
    """A card saved as right on Scan is not flagged in the Library either."""
    crops = [c for c in crop_paths if c]
    if not uid or uid < 1 or not crops:
        return 0
    s = get_session()
    try:
        items = s.query(InventoryItem).filter(InventoryItem.user_id == uid,
                                              InventoryItem.source_crop_path.in_(crops)).all()
        for item in items:
            set_checked(s, item.id)
        s.commit()
        return len(items)
    finally:
        s.close()


def _checked_on_scan(crop_path: Optional[str]) -> None:
    """Resolved in the Library: the Scan card it came from clears too."""
    if not crop_path:
        return
    try:
        with bench_db.connect(BENCH_DB_PATH) as conn:
            card_id = bench_db.card_id_by_crop(conn, crop_path)
            if card_id:
                bench_db.update_card(conn, card_id, {"checked": 1})
    except Exception as exc:                        # the Library change still stands
        print(f"[library] could not mark the Scan card as checked: {exc}")


def set_your_price(s, item_id: int, price: Optional[float]) -> None:
    """Set or (None) clear the owner's own price on a Library card."""
    from webapp.models import InventoryPrice

    row = s.get(InventoryPrice, item_id)
    if price is None:
        if row is not None:
            s.delete(row)
    elif row is None:
        s.add(InventoryPrice(inventory_item_id=item_id, price=round(float(price), 2)))
    else:
        row.price = round(float(price), 2)


def sync_your_price(uid: Optional[int], crop_paths: list, price: Optional[float]) -> int:
    """A list price set on Scan follows the card into the Library: the
    account's Library cards that came from these scan photos take it."""
    crops = [c for c in crop_paths if c]
    if not uid or uid < 1 or not crops:
        return 0
    s = get_session()
    try:
        items = s.query(InventoryItem).filter(InventoryItem.user_id == uid,
                                              InventoryItem.source_crop_path.in_(crops)).all()
        for item in items:
            set_your_price(s, item.id, price)
        s.commit()
        return len(items)
    finally:
        s.close()


def _price_on_scan(crop_path: Optional[str], price: Optional[float]) -> None:
    """Keep the Scan card this Library card came from on the same price, so
    the two pages never disagree."""
    if not crop_path:
        return
    try:
        with bench_db.connect(BENCH_DB_PATH) as conn:
            card_id = bench_db.card_id_by_crop(conn, crop_path)
            if card_id:
                bench_db.update_card(conn, card_id, {"list_price": price})
    except Exception as exc:                        # the Library change still stands
        print(f"[library] could not set the price on Scan: {exc}")


def _item_dict(s, item: InventoryItem, yours: Optional[dict] = None, checked: Optional[set] = None) -> dict:
    cc = item.catalog_card
    price, provenance = item_valuation(item)
    yours = your_prices(s, [item.id]) if yours is None else yours
    checked = checked_items(s, [item.id]) if checked is None else checked
    # The owner's say settles a flag: a price of their own, or the card
    # saved as right.
    if provenance.get("flagged") and (yours.get(item.id) is not None or item.id in checked):
        provenance = {**provenance, "flagged": False}
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
        # The owner's own price: what the card lists at. The market price
        # above still values the collection.
        "your_price": yours.get(item.id),
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
        yours = your_prices(s, [i.id for i in items])
        checked = checked_items(s, [i.id for i in items])
        payload = [_item_dict(s, i, yours, checked) for i in items]
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
        if "your_price" in patch:
            raw = patch["your_price"]
            try:
                yours = round(float(raw), 2) if raw not in (None, "") else None
            except (TypeError, ValueError):
                return jsonify({"error": "the set price must be a number"}), 400
            if yours is not None and not (math.isfinite(yours) and 0 < yours <= 100_000):
                return jsonify({"error": "the set price must be more than 0"}), 400
            set_your_price(s, item.id, yours)
            _price_on_scan(item.source_crop_path, yours)
        if patch.get("checked"):
            set_checked(s, item.id)
            _checked_on_scan(item.source_crop_path)
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
            return "a buyer has a pending order on this card; settle or wait for that order first"
    for listing in active:
        listing.status = "cancelled"
    return None
