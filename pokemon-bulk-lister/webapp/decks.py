"""Decks: build popular decks from the library, see what is missing, find it.

Three sources of decklists, one view:
  popular   archetypes from recent Limitless tournaments (lib/decks.py),
            ranked by how much of each the user's library already covers
  saved     a popular list the user kept, or any list pasted in Pokemon TCG
            Live export format

For a deck, every line is matched against the library (lib.decks.match_deck).
Each missing card then gets:
  - its catalog price when the catalog has one, with provenance
  - active listings on this platform's marketplace, cheapest first
  - search links to TCGplayer and eBay

A deck is addressed by `ref`: the numeric id of a saved deck, or
"p-<archetype id>" for a popular one.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from flask import Blueprint, abort, jsonify, render_template, request

from lib import decks as deck_lib
from webapp import scheduler
from webapp.auth import current_user_id, login_required
from webapp.catalog import price_provenance
from webapp.models import (
    CatalogCard,
    Deck,
    DeckCard,
    InventoryItem,
    MarketListing,
    Order,
    User,
    get_or_create_catalog_card,
    get_session,
)

ROOT = Path(__file__).resolve().parent.parent
POPULAR_CACHE = ROOT / os.getenv("OUTPUT_DIR", "output") / "cache" / "decks" / "popular.json"

# Ceiling on catalog cards priced by one "price missing cards" click. Each one
# costs a pokemontcg.io lookup and an eBay comps query.
PRICE_MISSING_MAX = int(os.getenv("DECK_PRICE_MISSING_MAX", "40"))
MAX_SAVED_DECKS = 200
MAX_LISTINGS_PER_CARD = 5

decks_bp = Blueprint("decks", __name__)

_set_codes: Optional[deck_lib.SetCodes] = None
_refresh_lock = threading.Lock()


def set_codes() -> deck_lib.SetCodes:
    global _set_codes
    if _set_codes is None:
        _set_codes = deck_lib.SetCodes.load()
    return _set_codes


def _popular() -> dict:
    return deck_lib.load_popular(POPULAR_CACHE)


def _mask_email(email: str) -> str:
    local = email.split("@")[0]
    return (local[:1] + "***") if local else "***"


# ----------------------------------------------------------------------
# Library + deck resolution
# ----------------------------------------------------------------------

def _owned(s, uid: int) -> list[dict]:
    rows = (
        s.query(InventoryItem, CatalogCard)
        .join(CatalogCard, InventoryItem.catalog_card_id == CatalogCard.id)
        .filter(InventoryItem.user_id == uid, InventoryItem.quantity > 0)
        .all()
    )
    return [{
        "name": cc.name, "set_code": cc.set_code, "set_name": cc.set_name,
        "card_number": cc.card_number, "quantity": item.quantity,
    } for item, cc in rows]


def _saved_cards(deck: Deck) -> list[dict]:
    return [{
        "section": c.section, "count": c.count, "name": c.name,
        "set": c.set_code, "number": c.card_number,
    } for c in deck.cards]


def _resolve(s, uid: int, ref: str) -> Optional[dict]:
    """ref -> {ref, name, source, cards, meta, saved}."""
    if ref.startswith("p-"):
        data = _popular()
        for d in data.get("decks") or []:
            if d["id"] == ref[2:]:
                return {
                    "ref": ref, "name": d["name"], "source": "popular", "saved": False,
                    "cards": d["cards"],
                    "meta": _popular_meta(d, data),
                }
        return None
    if not ref.isdigit():
        return None
    deck = s.query(Deck).filter_by(id=int(ref), user_id=uid).one_or_none()
    if deck is None:
        return None
    try:
        meta = json.loads(deck.source_meta) if deck.source_meta else {}
    except ValueError:
        meta = {}
    return {
        "ref": str(deck.id), "name": deck.name, "source": deck.source, "saved": True,
        "cards": _saved_cards(deck), "meta": meta,
        "created_at": deck.created_at.isoformat() + "Z",
    }


def _popular_meta(d: dict, data: dict) -> dict:
    return {
        "share_pct": d.get("share_pct"),
        "decks_entered": d.get("decks_entered"),
        "sample": d.get("sample"),
        "source": data.get("source"),
        "format": data.get("format"),
        "fetched_at": data.get("fetched_at"),
        "origin": data.get("origin"),
    }


def _summary(match: dict) -> dict:
    return {k: match[k] for k in ("total", "have", "missing", "maybe", "completion_pct")}


# ----------------------------------------------------------------------
# Pricing + listings for deck cards
# ----------------------------------------------------------------------

def _name_variants(name: str) -> set[str]:
    """Lower-cased spellings a catalog row for this card might use."""
    base = name.strip().lower()
    out = {base, base.replace("'", "’"), base.replace("’", "'")}
    if deck_lib.is_basic_energy(name):
        n = deck_lib.norm_name(name)
        out |= {n, f"basic {n}"}
    return out


def _catalog_by_name(s, cards: list[dict]) -> dict[str, list[CatalogCard]]:
    from sqlalchemy import func

    variants: set[str] = set()
    for c in cards:
        variants |= _name_variants(c["name"])
    if not variants:
        return {}
    rows = s.query(CatalogCard).filter(func.lower(CatalogCard.name).in_(variants)).all()
    out: dict[str, list[CatalogCard]] = {}
    for cc in rows:
        out.setdefault(deck_lib.norm_name(cc.name), []).append(cc)
    return out


def _is_exact(card: dict, cc: CatalogCard) -> bool:
    return bool(
        card.get("set") and card.get("number")
        and deck_lib.norm_number(cc.card_number) == deck_lib.norm_number(card["number"])
        and set_codes().same_set(card["set"], cc.set_code, cc.set_name)
    )


def _any_printing(card: dict) -> bool:
    return card.get("section") in ("trainer", "energy") or deck_lib.is_basic_energy(card["name"])


def _price_for(card: dict, candidates: list[CatalogCard]) -> Optional[dict]:
    """Catalog price for a deck line. The exact printing wins. For Trainers
    and Energy, where any printing plays the same, the cheapest known
    printing is used and labelled as such. Pokemon never borrow a price from
    another printing."""
    priced = [cc for cc in candidates if cc.final_price is not None]
    exact = [cc for cc in priced if _is_exact(card, cc)]
    if exact:
        cc = min(exact, key=lambda c: c.final_price)
        basis = "this printing"
    elif priced and _any_printing(card):
        cc = min(priced, key=lambda c: c.final_price)
        basis = "cheapest printing in the catalog"
    else:
        return None
    return {
        "price": cc.final_price,
        "basis": basis,
        "catalog_card_id": cc.id,
        "set_name": cc.set_name,
        "card_number": cc.card_number,
        "tcgplayer_url": cc.tcgplayer_url,
        "provenance": price_provenance(cc),
    }


def _listings_for(s, uid: int, cards: list[dict],
                  catalog: dict[str, list[CatalogCard]]) -> dict[int, list[dict]]:
    """Active marketplace listings from other sellers, keyed by card index."""
    by_ccid: dict[int, CatalogCard] = {cc.id: cc for group in catalog.values() for cc in group}
    if not by_ccid:
        return {}
    rows = (
        s.query(MarketListing, InventoryItem, User)
        .join(InventoryItem, MarketListing.inventory_item_id == InventoryItem.id)
        .join(User, MarketListing.seller_id == User.id)
        .filter(MarketListing.status == "active",
                MarketListing.seller_id != uid,
                MarketListing.catalog_card_id.in_(list(by_ccid)))
        .all()
    )
    pending = {row[0] for row in s.query(Order.listing_id).filter_by(status="pending_payment").all()}
    out: dict[int, list[dict]] = {}
    for idx, card in enumerate(cards):
        found = []
        for listing, item, seller in rows:
            if listing.id in pending:
                continue
            cc = by_ccid[listing.catalog_card_id]
            if deck_lib.norm_name(cc.name) != deck_lib.norm_name(card["name"]):
                continue
            exact = _is_exact(card, cc)
            same_set = set_codes().same_set(card.get("set"), cc.set_code, cc.set_name)
            # A same-named Pokemon from another set may be a different card.
            playable = exact or _any_printing(card) or same_set or not card.get("set")
            found.append({
                "listing_id": listing.id,
                "price": listing.price,
                "condition": item.condition,
                "seller": _mask_email(seller.email),
                "set_name": cc.set_name,
                "card_number": cc.card_number,
                "exact": exact,
                "check_printing": not playable,
                "market_price": cc.final_price,
            })
        found.sort(key=lambda l: (l["check_printing"], not l["exact"], l["price"]))
        if found:
            out[idx] = found
    return out


def _detail(s, uid: int, resolved: dict) -> dict:
    match = deck_lib.match_deck(resolved["cards"], _owned(s, uid), set_codes())
    cards = match["cards"]
    catalog = _catalog_by_name(s, cards)
    listings = _listings_for(s, uid, [c for c in cards], catalog)

    cost = 0.0
    priced_missing = unpriced_missing = basic_energy_missing = 0
    on_platform_missing = 0
    for idx, c in enumerate(cards):
        c["set_name"] = set_codes().name(c.get("set"))
        c["basic_energy"] = deck_lib.is_basic_energy(c["name"])
        c["links"] = deck_lib.search_links(c["name"], c.get("set"), c.get("number"))
        c["price"] = _price_for(c, catalog.get(deck_lib.norm_name(c["name"]), []))
        all_listings = listings.get(idx, [])
        c["listing_count"] = len(all_listings)
        c["listings"] = all_listings[:MAX_LISTINGS_PER_CARD]
        if c["missing"] <= 0:
            continue
        if any(not l["check_printing"] for l in all_listings):
            on_platform_missing += 1
        if c["basic_energy"]:
            basic_energy_missing += c["missing"]
        elif c["price"]:
            priced_missing += c["missing"]
            cost += c["price"]["price"] * c["missing"]
        else:
            unpriced_missing += c["missing"]

    return {
        **{k: v for k, v in resolved.items() if k != "cards"},
        "cards": cards,
        "summary": _summary(match),
        "cost": {
            # A total is only as good as its coverage, so the counts ship
            # with it and the UI states them next to the number.
            "estimate": round(cost, 2),
            "priced_missing": priced_missing,
            "unpriced_missing": unpriced_missing,
            "basic_energy_missing": basic_energy_missing,
            "source": "catalog prices (TCGplayer, Cardmarket and eBay sold, aggregated)",
        },
        "missing_lines_on_platform": on_platform_missing,
        "missing_list": deck_lib.format_decklist(cards, only_missing=True),
        "pricing_running": bool(scheduler.status().get("running_cycle")),
    }


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------

@decks_bp.route("/decks")
@login_required
def decks_page():
    return render_template("decks.html")


# ----------------------------------------------------------------------
# Lists
# ----------------------------------------------------------------------

@decks_bp.route("/api/decks")
@login_required
def list_decks():
    """Popular decks and saved decks, each with how much the library covers."""
    uid = current_user_id()
    s = get_session()
    try:
        owned = _owned(s, uid)
        data = _popular()
        popular = []
        for d in data.get("decks") or []:
            m = deck_lib.match_deck(d["cards"], owned, set_codes())
            popular.append({
                "ref": f"p-{d['id']}", "name": d["name"],
                "share_pct": d.get("share_pct"), "decks_entered": d.get("decks_entered"),
                "sample": d.get("sample"), **_summary(m),
            })
        saved = []
        for deck in (s.query(Deck).filter_by(user_id=uid)
                     .order_by(Deck.created_at.desc()).all()):
            m = deck_lib.match_deck(_saved_cards(deck), owned, set_codes())
            saved.append({
                "ref": str(deck.id), "name": deck.name, "source": deck.source,
                "created_at": deck.created_at.isoformat() + "Z", **_summary(m),
            })
    finally:
        s.close()
    return jsonify({
        "popular": popular,
        "saved": saved,
        "library_cards": sum(o["quantity"] for o in owned),
        "popular_source": {
            "source": data.get("source"),
            "format": data.get("format"),
            "fetched_at": data.get("fetched_at"),
            "origin": data.get("origin"),
            "tournaments": data.get("tournaments") or [],
        },
    })


@decks_bp.route("/api/decks/popular/refresh", methods=["POST"])
@login_required
def refresh_popular():
    """Pull fresh tournament lists from Limitless (4 HTTP requests)."""
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"error": "a refresh is already running"}), 409
    try:
        data = deck_lib.fetch_popular()
        deck_lib.save_popular(data, POPULAR_CACHE)
    except Exception as exc:
        return jsonify({"error": f"could not reach Limitless: {exc}"}), 502
    finally:
        _refresh_lock.release()
    return jsonify({"decks": len(data["decks"]), "fetched_at": data["fetched_at"]})


# ----------------------------------------------------------------------
# Save / import / delete
# ----------------------------------------------------------------------

@decks_bp.route("/api/decks", methods=["POST"])
@login_required
def create_deck():
    """Body: {"popular_ref": "p-..."} to keep a popular list, or
    {"name": str, "text": str} to import a pasted decklist."""
    body = request.get_json(silent=True) or {}
    uid = current_user_id()
    s = get_session()
    try:
        if s.query(Deck).filter_by(user_id=uid).count() >= MAX_SAVED_DECKS:
            return jsonify({"error": f"you have {MAX_SAVED_DECKS} saved decks, delete one first"}), 400
        warnings: list[str] = []
        if body.get("popular_ref"):
            resolved = _resolve(s, uid, str(body["popular_ref"]))
            if resolved is None or resolved["source"] != "popular":
                return jsonify({"error": "unknown popular deck"}), 404
            name, cards, source = resolved["name"], resolved["cards"], "popular"
            meta = resolved["meta"]
        else:
            parsed = deck_lib.parse_decklist(body.get("text") or "")
            cards, warnings = parsed["cards"], parsed["warnings"]
            if not cards:
                return jsonify({"error": "no cards found. Paste a list with one card per "
                                         "line, like: 4 Dragapult ex TWM 130"}), 400
            name = (body.get("name") or "").strip() or "Imported deck"
            source, meta = "paste", {}
        deck = Deck(user_id=uid, name=name[:200], source=source,
                    source_meta=json.dumps(meta) if meta else None)
        for i, c in enumerate(cards):
            deck.cards.append(DeckCard(
                position=i, section=c["section"], count=c["count"], name=c["name"][:200],
                set_code=c.get("set"), card_number=c.get("number"),
            ))
        s.add(deck)
        s.commit()
        out = {"ref": str(deck.id), "name": deck.name, "warnings": warnings}
    finally:
        s.close()
    return jsonify(out)


@decks_bp.route("/api/decks/<ref>")
@login_required
def get_deck(ref: str):
    uid = current_user_id()
    s = get_session()
    try:
        resolved = _resolve(s, uid, ref)
        if resolved is None:
            abort(404)
        out = _detail(s, uid, resolved)
    finally:
        s.close()
    return jsonify(out)


@decks_bp.route("/api/decks/<ref>", methods=["DELETE"])
@login_required
def delete_deck(ref: str):
    if not ref.isdigit():
        abort(404)
    s = get_session()
    try:
        deck = s.query(Deck).filter_by(id=int(ref), user_id=current_user_id()).one_or_none()
        if deck is None:
            abort(404)
        s.delete(deck)
        s.commit()
    finally:
        s.close()
    return jsonify({"deleted": ref})


# ----------------------------------------------------------------------
# Price the missing cards
# ----------------------------------------------------------------------

@decks_bp.route("/api/decks/<ref>/price-missing", methods=["POST"])
@login_required
def price_missing(ref: str):
    """Look up prices for missing cards the catalog has no price for.

    Adds them to the shared catalog (so the scheduler keeps them fresh) and
    prices them in the background. Basic energy is skipped. Body:
    {"dry_run": true} returns the count without starting anything.
    """
    dry_run = bool((request.get_json(silent=True) or {}).get("dry_run"))
    uid = current_user_id()
    s = get_session()
    try:
        resolved = _resolve(s, uid, ref)
        if resolved is None:
            abort(404)
        match = deck_lib.match_deck(resolved["cards"], _owned(s, uid), set_codes())
        catalog = _catalog_by_name(s, match["cards"])
        todo = [
            c for c in match["cards"]
            if c["missing"] > 0 and not deck_lib.is_basic_energy(c["name"])
            and _price_for(c, catalog.get(deck_lib.norm_name(c["name"]), [])) is None
        ]
        capped = todo[:PRICE_MISSING_MAX]
        if dry_run or not capped:
            return jsonify({"to_price": len(capped), "over_cap": len(todo) - len(capped),
                            "started": False})
        ids = []
        for c in capped:
            mapped = set_codes().ids(c.get("set"))
            cc = get_or_create_catalog_card(s, {
                "name": c["name"],
                "set_code": mapped[0] if mapped else None,
                "set_name": set_codes().name(c.get("set")),
                "card_number": c.get("number"),
                "is_holo": False,
            })
            ids.append(cc.id)
        s.commit()
    finally:
        s.close()
    threading.Thread(
        target=scheduler.refresh_cards, args=(ids,), kwargs={"batch_size": len(ids)},
        daemon=True,
    ).start()
    return jsonify({"to_price": len(ids), "over_cap": len(todo) - len(capped), "started": True})
