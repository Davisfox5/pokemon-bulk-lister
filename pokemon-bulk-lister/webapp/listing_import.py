"""Bring cards that are already listed on eBay, TCGplayer or Whatnot into the
library.

Where the listings come from:
  eBay       the connected account, read through eBay's API (lib/ebay_listings)
  TCGplayer  the pricing export from the seller portal (the same file that
             supplies TCGplayer Ids); rows with a quantity are what is listed
  Whatnot    a CSV of listings (Whatnot has no public API for sellers)

Each listing lands in `external_listings`, once per (account, site, id), so
reading the same listings again adds nothing. What happens to it:

  linked     its SKU is a card this library already holds (this app listed
             it); nothing is added
  imported   it was identified and a library row was added
  unmatched  it could not be tied to exactly one card; kept for a person to
             assign or dismiss
  dismissed  a person said to ignore it

Identifying a card never guesses. A TCGplayer row says its set, name, number,
condition and printing outright. An eBay or Whatnot listing only has a title,
so the title must contain a collector number written as number/total
("130/167"); the card is the one with that number in a set of that size whose
name appears in the title. Zero or several candidates leave it unmatched.

Imported cards are added to the library only. They are not put up for sale
on this app's marketplace, and nothing is changed on the other site.
"""
from __future__ import annotations

import csv
import io
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from lib.tcgplayer_catalog import _header, _norm, _norm_number, _norm_set
from webapp.auth import current_user_id, is_operator, login_required
from webapp.models import (
    CatalogCard,
    ExternalListing,
    InventoryItem,
    get_or_create_catalog_card,
    get_session,
    log_event,
    record_price,
)

listings_bp = Blueprint("listings", __name__)

SOURCES = {"ebay": "eBay", "tcgplayer": "TCGplayer", "whatnot": "Whatnot", "cards": "Spreadsheet"}
CSV_SOURCES = ("tcgplayer", "whatnot", "cards")
# "cards" is a list of cards you own, not listings on another site: its rows
# say what each card is, and nothing is shown as listed elsewhere.
STATED_SOURCES = ("tcgplayer", "cards")
CONDITIONS = ("NM", "LP", "MP", "HP", "DMG")
MAX_ROWS = 5000
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_QUANTITY = 9999

# Looking a title up costs a network call. One request does a bounded number
# and leaves the rest unmatched, to be continued with "Try matching again".
LOOKUPS_PER_RUN = 40
LOOKUP_SECONDS = 20.0

_import_lock = threading.Lock()


# ----------------------------------------------------------------------
# Reading the files
# ----------------------------------------------------------------------

_FULL_CONDITIONS = (("near mint", "NM"), ("lightly played", "LP"), ("moderately played", "MP"),
                    ("heavily played", "HP"), ("damaged", "DMG"))


def _amount(value) -> Optional[float]:
    try:
        number = float(re.sub(r"[^0-9.\-]", "", str(value or "")))
    except ValueError:
        return None
    return round(number, 2) if number > 0 else None


def _count(value) -> int:
    try:
        return max(0, min(int(float(str(value or "0").replace(",", ""))), MAX_QUANTITY))
    except ValueError:
        return 0


def _columns(headers: list[str], wanted: dict[str, tuple[str, ...]]) -> dict[str, Optional[str]]:
    by_norm = {_header(h): h for h in headers}
    return {field: next((by_norm[n] for n in names if n in by_norm), None)
            for field, names in wanted.items()}


def _split_condition(text: Optional[str]) -> tuple[Optional[str], str]:
    """TCGplayer writes condition and printing in one cell: "Near Mint
    Holofoil", "Lightly Played 1st Edition". -> ("NM", "holofoil")."""
    norm = _norm(text)
    for full, short in _FULL_CONDITIONS:
        if norm.startswith(full):
            return short, norm[len(full):].strip()
    return None, norm


_TCG_COLUMNS = {
    "id": ("tcgplayerid",), "name": ("productname", "name"), "number": ("number", "cardnumber"),
    "condition": ("condition",), "set": ("setname", "set"), "line": ("productline",),
    "quantity": ("totalquantity", "quantity"), "price": ("tcgmarketplaceprice",),
    "rarity": ("rarity",),
}


def parse_tcgplayer_listings(text: str) -> tuple[list[dict], dict]:
    """The seller portal's pricing export -> listing lines. Only rows with a
    quantity are listed for sale; the rest of the file is TCGplayer's whole
    catalog and is ignored."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    cols = _columns(headers, _TCG_COLUMNS)
    labels = {"id": "TCGplayer Id", "name": "Product Name", "condition": "Condition",
              "quantity": "Total Quantity"}
    missing = [labels[f] for f in labels if not cols[f]]
    if missing:
        raise ValueError(
            "this does not look like a TCGplayer pricing export: no " + ", ".join(missing)
            + " column. Columns found: " + (", ".join(headers) or "none"))
    info = {"rows": 0, "skipped": 0, "not_listed": 0}
    lines: list[dict] = []
    for raw in reader:
        info["rows"] += 1
        tcg_id = (raw.get(cols["id"]) or "").strip()
        if not tcg_id or (cols["line"] and "pokemon" not in _norm(raw.get(cols["line"]))):
            info["skipped"] += 1
            continue
        quantity = _count(raw.get(cols["quantity"]))
        if quantity < 1:
            info["not_listed"] += 1
            continue
        if len(lines) >= MAX_ROWS:
            raise ValueError(f"that file lists more than {MAX_ROWS} cards for sale; split it up")
        name = re.sub(r"\s+-\s+\d[\w/]*\s*$", "", (raw.get(cols["name"]) or "")).strip()
        number_raw = (raw.get(cols["number"]) or "").strip() if cols["number"] else ""
        number = number_raw.split("/")[0].lstrip("0") or number_raw.split("/")[0]
        set_name = re.sub(r"^\s*[A-Za-z0-9.]+:\s*", "", (raw.get(cols["set"]) or "")).strip() if cols["set"] else ""
        condition, printing = _split_condition(raw.get(cols["condition"]))
        line = {
            "external_id": tcg_id, "sku": None, "quantity": quantity,
            "title": " · ".join(p for p in (name, set_name, number_raw, raw.get(cols["condition"]) or "") if p),
            "price": _amount(raw.get(cols["price"])) if cols["price"] else None,
            "url": None, "image_url": None,
        }
        # A row with no number (sealed product) or a condition this app does
        # not know is kept, unidentified, for a person to look at.
        if name and number and set_name and condition:
            line["condition"] = condition
            line["card"] = {
                "name": name, "set_name": set_name, "card_number": number,
                "rarity": (raw.get(cols["rarity"]) or "").strip() or None if cols["rarity"] else None,
                # The catalog knows two finishes. A reverse holo is priced
                # with the plain card and says so in its note.
                "is_holo": "holofoil" in printing and "reverse" not in printing,
            }
            line["note"] = printing.title() if printing and printing != "holofoil" else None
        lines.append(line)
    return lines, info


_CARD_COLUMNS = {
    "name": ("name", "card", "cardname", "productname", "pokemon", "title"),
    "set": ("set", "setname", "expansion", "series", "edition"),
    "set_code": ("setcode", "setid", "setabbreviation"),
    "number": ("number", "cardnumber", "no", "collectornumber", "cardno", "num"),
    "quantity": ("quantity", "qty", "count", "owned", "amount", "copies"),
    "condition": ("condition", "cond", "grade"),
    "holo": ("holo", "foil", "isholo", "holofoil"),
    "printing": ("printing", "variant", "finish", "version"),
    "rarity": ("rarity",),
    "cost": ("pricepaid", "paid", "cost", "purchaseprice", "boughtfor", "costbasis", "acquiredprice"),
    "market": ("marketprice", "marketvalue", "tcgmarketprice", "currentprice", "tcgplayermarket"),
    "price_source": ("pricesource", "pricesourceurl", "sourceurl"),
    "note": ("notes", "note", "comment", "comments", "description"),
}
# Shown to the user so they can see what each of their columns became.
_CARD_FIELD_LABELS = {
    "name": "card name", "set": "set", "set_code": "set code", "number": "card number",
    "quantity": "quantity", "condition": "condition", "holo": "holo",
    "printing": "printing (holo or not; the wording is kept in the note)",
    "rarity": "rarity (and holo or not, if it says foil or holo)",
    "cost": "price paid", "market": "market price (used until a live price is found)",
    "price_source": "where the market price came from", "note": "note",
}
_YES = ("y", "yes", "true", "1", "x", "holo", "foil", "holofoil")
_PLAIN_PRINTINGS = ("", "holo", "holofoil", "foil", "normal", "non holo", "nonholo")


def _card_columns(headers: list[str]) -> dict[str, Optional[str]]:
    """Which header is which field. An exact header name wins; then a header
    that starts with one ("Condition (est.)", "Rarity/variant", "Market price
    USD"). A header is used for one field only."""
    by_norm = {_header(h): h for h in headers if h}
    cols: dict[str, Optional[str]] = {}
    taken: set[str] = set()
    for field, names in _CARD_COLUMNS.items():
        hit = next((by_norm[n] for n in names if n in by_norm and by_norm[n] not in taken), None)
        cols[field] = hit
        if hit:
            taken.add(hit)
    for field, names in _CARD_COLUMNS.items():
        if cols[field]:
            continue
        for norm, original in by_norm.items():
            if original not in taken and any(len(n) >= 4 and norm.startswith(n) for n in names):
                cols[field] = original
                taken.add(original)
                break
    return cols


def card_identity(external_id: str) -> str:
    """What makes two card-list rows "the same row": name, set and number
    (plus the counter that tells identical rows apart). Condition and holo
    are left out, so reading the sheet again after they changed, or after
    this app learned to read them, finds the row it added before."""
    core, _, counter = external_id.partition("#")
    return "|".join(core.split("|")[:3]) + (f"#{counter}" if counter else "")


def parse_card_list(text: str) -> tuple[list[dict], dict]:
    """A spreadsheet of cards you own, read by header name. Only a name
    column is required. Set, set code, number, quantity, condition, holo,
    printing, rarity, price paid, market price and notes are read when
    present, under the usual spellings of those headers.

    Nothing in a row is dropped: a column this app has no field for is kept
    in the card's note as "Header: value". Two values are filled in when a
    row does not give them, because a library row cannot be without them:
    quantity 1 and condition NM. `info` says which column was read as what,
    which went to the note, and how many rows were filled in; the preview
    shows that before anything is added."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = [h for h in (reader.fieldnames or []) if h]
    cols = _card_columns(headers)
    if not cols["name"]:
        raise ValueError("no Name column found (looked for: Name, Card, Card name). "
                         "Columns in this sheet: " + (", ".join(headers) or "none"))
    used = {cols[f]: _CARD_FIELD_LABELS[f] for f in _CARD_COLUMNS if cols[f]}
    leftover = [h for h in headers if h not in used]
    info = {
        "rows": 0, "skipped": 0, "not_listed": 0,
        "columns_read": [{"column": c, "as": used[c]} for c in headers if c in used],
        "columns_noted": leftover,
        "assumed_condition": 0, "assumed_quantity": 0, "with_market_price": 0,
    }
    lines: list[dict] = []
    seen: dict[str, int] = {}

    def cell(raw: dict, field: str) -> str:
        return (raw.get(cols[field]) or "").strip() if cols[field] else ""

    for raw in reader:
        info["rows"] += 1
        name = cell(raw, "name")
        if not name:
            info["skipped"] += 1
            continue
        if cols["quantity"] and cell(raw, "quantity"):
            quantity = _count(cell(raw, "quantity"))
        else:
            quantity = 1
            info["assumed_quantity"] += 1
        if quantity < 1:
            info["not_listed"] += 1
            continue
        if len(lines) >= MAX_ROWS:
            raise ValueError(f"that sheet has more than {MAX_ROWS} cards; split it up")
        set_name, number_raw = cell(raw, "set"), cell(raw, "number")
        first = number_raw.split("/")[0].strip()
        number = first.lstrip("0") or first
        given = cell(raw, "condition")
        condition = given.upper() if given.upper() in CONDITIONS else _split_condition(given)[0]
        assumed = condition is None
        if assumed:
            condition = "NM"
            info["assumed_condition"] += 1
        printing, rarity = cell(raw, "printing"), cell(raw, "rarity")
        # "Holofoil", "Reverse Holo", "Rare (sparkle foil)": the finish can sit
        # in a printing column or inside the rarity.
        finish = _norm(f"{printing} {rarity}")
        holo = cell(raw, "holo").lower() in _YES or (
            ("holo" in finish or "foil" in finish)
            and "reverse" not in finish and "non" not in finish.split())
        market = _amount(cell(raw, "market"))
        if market is not None:
            info["with_market_price"] += 1

        # Kept word for word, so nothing the sheet said is lost.
        extras = [cell(raw, "note")]
        if printing and _norm(printing) not in _PLAIN_PRINTINGS:
            extras.append(f"Printing: {printing}")
        if given and (assumed or _norm(given) not in [full for full, _ in _FULL_CONDITIONS] + [c.lower() for c in CONDITIONS]):
            extras.append(f"Condition as written: {given}")
        if number_raw and number_raw not in (number, first) and "/" in number_raw:
            extras.append(f"Number as written: {number_raw}")
        extras += [f"{h}: {(raw.get(h) or '').strip()}" for h in leftover if (raw.get(h) or "").strip()]
        note = "; ".join(x for x in extras if x)[:1000] or None

        # The row's identity: name, set and number, so the same sheet read
        # again finds the rows it added. Identical rows get a counter.
        base = "|".join((_norm(name), _norm(set_name), number))
        seen[base] = seen.get(base, 0) + 1
        lines.append({
            "external_id": base if seen[base] == 1 else f"{base}#{seen[base]}",
            "sku": None, "quantity": quantity, "price": None, "url": None, "image_url": None,
            "title": " · ".join(p for p in (name, set_name, number_raw, condition) if p),
            "condition": condition, "condition_assumed": assumed,
            "cost": _amount(cell(raw, "cost")), "note": note,
            "sheet_price": market, "sheet_price_source": cell(raw, "price_source")[:200] or None,
            "card": {"name": name, "set_name": set_name or None,
                     "set_code": cell(raw, "set_code").lower() or None, "card_number": number or None,
                     "rarity": rarity[:80] or None, "is_holo": holo},
        })
    return lines, info


_WHATNOT_COLUMNS = {
    "title": ("title", "listingtitle", "productname", "name"),
    "quantity": ("quantity", "qty", "availablequantity"),
    "price": ("price", "buyitnowprice", "startingprice", "startingbid"),
    "sku": ("sku", "customlabel"),
    "id": ("listingid", "productid", "id"),
}


def parse_whatnot_listings(text: str) -> tuple[list[dict], dict]:
    """A Whatnot listings CSV, read by header name. The same columns as
    Whatnot's bulk upload template (Title, Quantity, Price, SKU)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    cols = _columns(headers, _WHATNOT_COLUMNS)
    if not cols["title"]:
        raise ValueError("no Title column found. Columns in this file: " + (", ".join(headers) or "none"))
    info = {"rows": 0, "skipped": 0, "not_listed": 0}
    lines: list[dict] = []
    seen: dict[str, int] = {}
    for raw in reader:
        info["rows"] += 1
        title = (raw.get(cols["title"]) or "").strip()
        if not title:
            info["skipped"] += 1
            continue
        quantity = _count(raw.get(cols["quantity"])) if cols["quantity"] else 1
        if quantity < 1:
            info["not_listed"] += 1
            continue
        if len(lines) >= MAX_ROWS:
            raise ValueError(f"that file has more than {MAX_ROWS} listings; split it up")
        sku = (raw.get(cols["sku"]) or "").strip() or None if cols["sku"] else None
        own_id = (raw.get(cols["id"]) or "").strip() if cols["id"] else ""
        # Without an id from Whatnot, the SKU or the title stands in, so the
        # same listing gets the same identity in a later file.
        base = own_id or (f"sku:{sku}" if sku else f"title:{title}")
        seen[base] = seen.get(base, 0) + 1
        lines.append({
            "external_id": base if seen[base] == 1 else f"{base}#{seen[base]}",
            "sku": sku, "title": title, "quantity": quantity,
            "price": _amount(raw.get(cols["price"])) if cols["price"] else None,
            "url": None, "image_url": None,
        })
    return lines, info


# ----------------------------------------------------------------------
# Identifying a card from a listing title
# ----------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w/])(\d{1,3})\s*/\s*(\d{1,3})(?![\w/])")
_LOT = re.compile(r"\b(lot|bundle|playset|bulk|choose|pick)\b", re.I)
_GRADED = re.compile(r"\b(PSA|BGS|CGC|SGC|TAG)\s*\d", re.I)
_SHORT_CONDITION = re.compile(r"\b(NM|LP|MP|HP|DMG)\b")

_lookup_cache: dict[tuple[str, int], list[dict]] = {}


def lookup_cards(number: str, total: int) -> list[dict]:
    """Every card numbered `number` in a set that prints `total` cards, from
    pokemontcg.io. Tests replace this."""
    key = (number, total)
    if key not in _lookup_cache:
        from lib.tcgplayer_client import TCGPlayerClient

        data = TCGPlayerClient()._get("/cards", {   # noqa: SLF001
            "q": f"number:{number} set.printedTotal:{total}", "pageSize": 50,
            "select": "id,name,number,rarity,set,images",
        })
        _lookup_cache[key] = [{
            "name": c.get("name") or "", "card_number": c.get("number"),
            "set_name": (c.get("set") or {}).get("name"), "set_code": (c.get("set") or {}).get("id"),
            "rarity": c.get("rarity"), "image_url": (c.get("images") or {}).get("small"),
        } for c in data.get("data") or []]
    return _lookup_cache[key]


class _Budget:
    def __init__(self) -> None:
        self.calls = LOOKUPS_PER_RUN
        self.deadline = time.monotonic() + LOOKUP_SECONDS

    def spend(self, number: str, total: int) -> bool:
        if (number, total) in _lookup_cache:
            return True
        if self.calls <= 0 or time.monotonic() > self.deadline:
            return False
        self.calls -= 1
        return True


def _named_in(title_norm: str, candidates: list, name_of) -> list:
    """Candidates whose whole name appears in the title. When one name
    contains another ("Pikachu ex" and "Pikachu"), the longer one wins."""
    hits = [c for c in candidates if _norm(name_of(c)) and f" {_norm(name_of(c))} " in title_norm]
    if not hits:
        return []
    longest = max(len(_norm(name_of(c))) for c in hits)
    return [c for c in hits if len(_norm(name_of(c))) == longest]


def title_condition(title: str) -> str:
    norm = _norm(title)
    for full, short in _FULL_CONDITIONS:
        if full in norm:
            return short
    hit = _SHORT_CONDITION.search(title)
    return hit.group(1) if hit else "NM"


def match_title(s, title: str, budget: _Budget) -> tuple[Optional[dict], str]:
    """(card, "") when the title names exactly one card, else (None, why)."""
    if _LOT.search(title):
        return None, "looks like a lot or a multi-card listing"
    hit = _NUMBER.search(title)
    if not hit:
        return None, "no collector number like 130/167 in the title"
    number = hit.group(1).lstrip("0") or "0"
    total = int(hit.group(2))
    title_norm = f" {_norm(title)} "
    holo = " holo " in title_norm and " non holo " not in title_norm and " reverse holo " not in title_norm

    # Cards the catalog already has, when the title also names their set.
    local = [c for c in s.query(CatalogCard).filter(CatalogCard.card_number.isnot(None)).all()
             if _norm_number(c.card_number) == number
             and _norm_set(c.set_name) and f" {_norm_set(c.set_name)} " in title_norm]
    local = _named_in(title_norm, local, lambda c: c.name)
    if len({(c.name, c.set_code or c.set_name) for c in local}) == 1:
        finishes = [c for c in local if bool(c.is_holo) == holo] or local
        if len(finishes) == 1:
            c = finishes[0]
            return {"name": c.name, "set_name": c.set_name, "set_code": c.set_code,
                    "card_number": c.card_number, "rarity": c.rarity, "is_holo": bool(c.is_holo)}, ""

    if not budget.spend(number, total):
        return None, "not looked up yet; use Try matching again"
    try:
        found = lookup_cards(number, total)
    except Exception as exc:
        print(f"[listings] card lookup failed for {number}/{total}: {exc}", file=sys.stderr)
        return None, "the card lookup could not be reached; use Try matching again"
    fits = _named_in(title_norm, found, lambda c: c["name"])
    if not fits:
        return None, f"no card numbered {hit.group(1)}/{hit.group(2)} has its name in this title"
    if len(fits) > 1:
        return None, f"{len(fits)} different cards fit this title"
    card = dict(fits[0])
    card["is_holo"] = holo or (card.get("rarity") or "").lower().startswith("rare holo")
    return card, ""


# ----------------------------------------------------------------------
# Putting listings in the library
# ----------------------------------------------------------------------

def _catalog_card(s, card: dict) -> CatalogCard:
    """The catalog row for a card, preferring one that is already there
    under the same name, number, set and finish (a scanned card carries a set
    code a TCGplayer row does not, so the plain key would miss it)."""
    rows = s.query(CatalogCard).filter(func.lower(CatalogCard.name) == card["name"].lower()).all()
    same = [r for r in rows
            if _norm_number(r.card_number) == _norm_number(card.get("card_number"))
            and _norm_set(r.set_name) == _norm_set(card.get("set_name"))
            and bool(r.is_holo) == bool(card.get("is_holo"))]
    if len(same) == 1:
        if card.get("image_url") and not same[0].image_url:
            same[0].image_url = card["image_url"]
        return same[0]
    return get_or_create_catalog_card(s, card)


def _place(s, row: ExternalListing, card: dict, condition: str, note: Optional[str] = None,
           cost: Optional[float] = None) -> InventoryItem:
    cc = _catalog_card(s, card)
    came_from = "a spreadsheet" if row.source == "cards" else f"a {SOURCES[row.source]} listing"
    item = InventoryItem(
        user_id=row.user_id, catalog_card_id=cc.id, quantity=row.quantity, condition=condition,
        image_url=row.image_url, notes=note, acquired_price=cost)
    s.add(item)
    s.flush()
    log_event(s, row.user_id, cc.id, "added", channel="other" if row.source == "cards" else row.source,
              quantity=row.quantity, condition=condition, image_url=row.image_url,
              note=f"imported from {came_from}" + (f" ({note})" if note else ""))
    row.status, row.detail, row.inventory_item_id, row.condition = "imported", None, item.id, condition
    return item


def _use_sheet_price(s, cc: CatalogCard, line: dict) -> bool:
    """A card with no price takes the market price its owner's sheet gives,
    marked as such. A live price found later replaces it."""
    price = line.get("sheet_price")
    if price is None or cc.final_price is not None:
        return False
    source = line.get("sheet_price_source")
    record_price(s, cc, {
        "final_price": price, "pricing_confidence": 0.5,
        "pricing_notes": "From your spreadsheet" + (f" ({source})" if source else ""),
    })
    return True


def _repair_from_sheet(s, row: ExternalListing, line: dict) -> bool:
    """A sheet row this account imported before, read again. The library row
    it made is brought up to what the sheet says now: finish, rarity, note,
    price paid, market price, and a condition the sheet states. Quantity is
    left alone (cards may have been sold or added by hand since). Returns
    whether anything changed."""
    item = s.get(InventoryItem, row.inventory_item_id) if row.inventory_item_id else None
    if item is None or item.user_id != row.user_id or item.quantity < 1:
        return False
    changed = False
    cc = _catalog_card(s, line["card"])
    if cc.id != item.catalog_card_id:           # the finish was read differently before
        item.catalog_card_id, changed = cc.id, True
    if line["card"].get("rarity") and not cc.rarity:
        cc.rarity, changed = line["card"]["rarity"], True
    if line.get("note") and item.notes != line["note"]:
        item.notes, changed = line["note"], True
    if line.get("cost") is not None and item.acquired_price is None:
        item.acquired_price, changed = line["cost"], True
    if not line.get("condition_assumed") and item.condition != line["condition"]:
        item.condition = row.condition = line["condition"]
        changed = True
    if _use_sheet_price(s, cc, line):
        changed = True
    row.title = (line.get("title") or row.title or "")[:500]
    return changed


def _sku_index(s, uid: int) -> dict[str, int]:
    """SKU -> library row. A card this app listed carries its photo's file
    stem as its SKU; a listing imported earlier keeps the SKU it came with."""
    index: dict[str, int] = {}
    for row in s.query(ExternalListing).filter(
            ExternalListing.user_id == uid, ExternalListing.sku.isnot(None),
            ExternalListing.inventory_item_id.isnot(None)).all():
        index[row.sku] = row.inventory_item_id
    for item in s.query(InventoryItem).filter(
            InventoryItem.user_id == uid, InventoryItem.source_crop_path.isnot(None)).all():
        index[Path(item.source_crop_path).stem] = item.id
    return index


def _existing(s, uid: int, source: str, lines: list[dict]) -> dict[str, ExternalListing]:
    """Rows already on file for these lines, keyed by the line's id."""
    rows = s.query(ExternalListing).filter_by(user_id=uid, source=source).all()
    wanted = {line["external_id"][:160] for line in lines}
    if source == "cards":
        # Matched on name, set and number: an older import wrote condition
        # and holo into the id as well.
        by_identity = {card_identity(r.external_id): r for r in rows}
        return {w: by_identity[card_identity(w)] for w in wanted if card_identity(w) in by_identity}
    return {r.external_id: r for r in rows if r.external_id in wanted}


def preview(uid: int, source: str, lines: list[dict]) -> dict:
    s = get_session()
    try:
        seen = _existing(s, uid, source, lines)
        skus = _sku_index(s, uid)
    finally:
        s.close()
    fresh = [l for l in lines if l["external_id"][:160] not in seen and l["quantity"] > 0]
    linked = [l for l in fresh if l.get("sku") and l["sku"] in skus]
    return {
        "dry_run": True, "source": source, "listings": len(lines),
        # A card list read again brings the rows it added before up to date.
        "will_update": len(seen) if source == "cards" else 0,
        "already_imported": len(lines) - len([l for l in lines if l["external_id"][:160] not in seen]),
        "already_in_library": len(linked),
        "new": len(fresh) - len(linked),
        "new_cards": sum(l["quantity"] for l in fresh) - sum(l["quantity"] for l in linked),
    }


def import_lines(uid: int, source: str, lines: list[dict]) -> dict:
    """Record listings and add the identified ones to `uid`'s library."""
    out = {"dry_run": False, "source": source, "listings": len(lines), "added": 0, "cards": 0,
           "linked": 0, "unmatched": 0, "duplicates": 0, "updated": 0}
    budget = _Budget()
    s = get_session()
    try:
        seen = _existing(s, uid, source, lines)
        skus = _sku_index(s, uid)
        for line in lines:
            ext = line["external_id"][:160]
            if ext in seen:
                # Known already. Keep what the site says now; the library is
                # not touched (the card may have been edited or sold since).
                row = seen[ext]
                row.price, row.url = line.get("price"), line.get("url") or row.url
                out["duplicates"] += 1
                if source == "cards" and row.status == "imported" and _repair_from_sheet(s, row, line):
                    out["updated"] += 1
                continue
            if line["quantity"] < 1:
                continue
            row = ExternalListing(
                user_id=uid, source=source, external_id=ext, sku=(line.get("sku") or None),
                title=(line.get("title") or "")[:500], quantity=line["quantity"],
                price=line.get("price"), url=line.get("url"), image_url=line.get("image_url"),
                status="unmatched")
            s.add(row)
            seen[ext] = row
            if row.sku and row.sku in skus:
                row.status, row.inventory_item_id = "linked", skus[row.sku]
                out["linked"] += 1
                continue
            card, why = line.get("card"), ""
            condition = line.get("condition")
            if line.get("multi"):
                card, why = None, "one listing with several variations"
            elif source in STATED_SOURCES:
                # These rows say what they are outright; one that does not is
                # not a single card (sealed product) or has an unknown condition.
                why = "" if card else "the row has no card number, set or known condition"
            else:
                card, why = match_title(s, row.title, budget)
                condition = title_condition(row.title)
            if card is None:
                row.detail = why
                out["unmatched"] += 1
                continue
            note = line.get("note")
            if _GRADED.search(row.title or ""):
                note = "graded; see the listing title"
            item = _place(s, row, card, condition or "NM", note, cost=line.get("cost"))
            _use_sheet_price(s, s.get(CatalogCard, item.catalog_card_id), line)
            if row.sku:
                skus[row.sku] = item.id
            out["added"] += 1
            out["cards"] += row.quantity
        s.commit()
    finally:
        s.close()
    return out


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------

def _ebay_auths(s, uid: int) -> list:
    """eBay connections this account may read: its own, and for the operator
    the one set up under Connections."""
    found = []
    try:
        from webapp.settings import UserEbayKeys, _auth_for

        keys = s.query(UserEbayKeys).filter_by(user_id=uid).one_or_none()
        if keys is not None:
            auth = _auth_for(uid, keys)
            if auth.is_authorized():
                found.append(auth)
    except Exception as exc:
        print(f"[listings] user eBay connection: {exc}", file=sys.stderr)
    if is_operator(s, uid):
        try:
            from webapp import connections

            auth = connections._ebay_auth()   # noqa: SLF001
            if auth.is_authorized():
                found.append(auth)
        except Exception as exc:
            print(f"[listings] operator eBay connection: {exc}", file=sys.stderr)
    return found


def _row_dict(row: ExternalListing) -> dict:
    return {"id": row.id, "source": row.source, "site": SOURCES.get(row.source, row.source),
            "title": row.title, "quantity": row.quantity, "price": row.price, "url": row.url,
            "sku": row.sku, "status": row.status, "detail": row.detail}


def _run(uid: int, source: str, lines: list[dict], dry_run: bool, info: Optional[dict] = None):
    if dry_run:
        return jsonify({**preview(uid, source, lines), **(info or {})})
    if not _import_lock.acquire(blocking=False):
        return jsonify({"error": "an import is already running; try again in a moment"}), 409
    try:
        return jsonify({**import_lines(uid, source, lines), **(info or {})})
    finally:
        _import_lock.release()


_PARSERS = {"tcgplayer": parse_tcgplayer_listings, "whatnot": parse_whatnot_listings,
            "cards": parse_card_list}


def import_text(uid: int, source: str, text: str, dry_run: bool, extra: Optional[dict] = None):
    """One CSV, wherever it came from (an upload, Google Drive): preview it
    or import it. Returns a Flask response."""
    try:
        lines, info = _PARSERS[source](text)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return _run(uid, source, lines, dry_run, {**info, **(extra or {})})


@listings_bp.route("/api/listings/external", methods=["GET"])
@login_required
def external_status():
    uid = current_user_id()
    s = get_session()
    try:
        rows = s.query(ExternalListing).filter_by(user_id=uid).all()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        unmatched = sorted((r for r in rows if r.status == "unmatched"), key=lambda r: -r.id)
        return jsonify({
            "ebay_connected": bool(_ebay_auths(s, uid)),
            "counts": counts,
            "unmatched": [_row_dict(r) for r in unmatched[:200]],
            "unmatched_total": len(unmatched),
        })
    finally:
        s.close()


@listings_bp.route("/api/listings/import", methods=["POST"])
@login_required
def import_file():
    """multipart: source=tcgplayer|whatnot|cards, file=<csv>. dry_run=1 reads the
    file and reports what it holds without changing anything."""
    source = (request.form.get("source") or "").lower()
    if source not in CSV_SOURCES:
        return jsonify({"error": f"source must be one of: {', '.join(CSV_SOURCES)}"}), 400
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "choose the CSV exported from the site"}), 400
    raw = upload.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        return jsonify({"error": "that file is over 40 MB"}), 400
    return import_text(current_user_id(), source, raw.decode("utf-8", errors="replace"),
                       request.form.get("dry_run") == "1")


@listings_bp.route("/api/listings/import/ebay", methods=["POST"])
@login_required
def import_ebay():
    """Body: {dry_run?}. Reads the active listings of the eBay account this
    user connected."""
    from lib import ebay_listings

    uid = current_user_id()
    s = get_session()
    try:
        auths = _ebay_auths(s, uid)
    finally:
        s.close()
    if not auths:
        return jsonify({"error": "eBay is not connected for this account. Connect it first."}), 409
    lines: list[dict] = []
    seen: set[str] = set()
    for auth in auths:
        try:
            found = ebay_listings.fetch_active_listings(auth)
        except ebay_listings.EbayListingsError as exc:
            return jsonify({"error": str(exc)}), 502
        except Exception as exc:
            return jsonify({"error": f"could not reach eBay ({exc})"}), 502
        for line in found:
            if line["external_id"] not in seen:
                seen.add(line["external_id"])
                lines.append(line)
    body = request.get_json(silent=True) or {}
    return _run(uid, "ebay", lines, bool(body.get("dry_run")))


@listings_bp.route("/api/listings/external/rematch", methods=["POST"])
@login_required
def rematch():
    """Try again to identify listings left unmatched (more lookups, or the
    catalog has grown since)."""
    uid = current_user_id()
    if not _import_lock.acquire(blocking=False):
        return jsonify({"error": "an import is already running; try again in a moment"}), 409
    budget = _Budget()
    out = {"added": 0, "cards": 0, "still_unmatched": 0, "waiting": 0}
    s = get_session()
    try:
        for row in (s.query(ExternalListing)
                    .filter_by(user_id=uid, status="unmatched").order_by(ExternalListing.id).all()):
            if row.source in STATED_SOURCES or (row.detail or "").startswith("one listing"):
                out["still_unmatched"] += 1
                continue
            card, why = match_title(s, row.title or "", budget)
            if card is None:
                row.detail = why
                out["still_unmatched"] += 1
                out["waiting"] += int(why.startswith("not looked up yet"))
                continue
            note = "graded; see the listing title" if _GRADED.search(row.title or "") else None
            _place(s, row, card, title_condition(row.title or ""), note)
            out["added"] += 1
            out["cards"] += row.quantity
        s.commit()
    finally:
        s.close()
        _import_lock.release()
    return jsonify(out)


@listings_bp.route("/api/listings/external/<int:row_id>/assign", methods=["POST"])
@login_required
def assign(row_id: int):
    """Body: {name, set_name?, set_code?, card_number?, is_holo?, condition?}.
    A person says which card an unmatched listing is."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "card name is required"}), 400
    condition = str(body.get("condition") or "NM").upper()
    if condition not in CONDITIONS:
        return jsonify({"error": f"condition must be one of {CONDITIONS}"}), 400
    s = get_session()
    try:
        row = s.query(ExternalListing).filter_by(
            id=row_id, user_id=current_user_id(), status="unmatched").one_or_none()
        if row is None:
            return jsonify({"error": "no such unmatched listing"}), 404
        item = _place(s, row, {
            "name": name,
            "set_name": (body.get("set_name") or "").strip() or None,
            "set_code": (body.get("set_code") or "").strip().lower() or None,
            "card_number": (body.get("card_number") or "").strip() or None,
            "is_holo": bool(body.get("is_holo")),
        }, condition)
        s.commit()
        return jsonify({"id": row.id, "inventory_item_id": item.id, "name": name, "quantity": row.quantity})
    finally:
        s.close()


@listings_bp.route("/api/listings/external/<int:row_id>/dismiss", methods=["POST"])
@login_required
def dismiss(row_id: int):
    s = get_session()
    try:
        row = s.query(ExternalListing).filter_by(
            id=row_id, user_id=current_user_id(), status="unmatched").one_or_none()
        if row is None:
            return jsonify({"error": "no such unmatched listing"}), 404
        row.status = "dismissed"
        s.commit()
        return jsonify({"id": row.id, "dismissed": True})
    finally:
        s.close()


def listed_on(s, item_ids: list[int]) -> dict[int, list[dict]]:
    """Library row id -> the outside listings it came from, for the Have tab."""
    out: dict[int, list[dict]] = {}
    if not item_ids:
        return out
    for row in s.query(ExternalListing).filter(
            ExternalListing.inventory_item_id.in_(item_ids),
            ExternalListing.source != "cards",          # a list of cards owned, not a listing
            ExternalListing.status.in_(("imported", "linked"))).all():
        out.setdefault(row.inventory_item_id, []).append(
            {"source": row.source, "site": SOURCES.get(row.source, row.source),
             "price": row.price, "url": row.url})
    return out
