"""Read sales back from the places cards are listed, into the library.

Two ways in, one recorder:

  eBay       the Fulfillment API (lib/ebay_orders.py), for the operator's
             account and for each user who connected their own. Runs on a
             timer and from "Sync eBay sales" on the Scan page.
  Whatnot    no orders API for regular sellers, so sales come in as the CSV
             a seller exports there. "Import sales CSV".

Every listing this app creates carries a SKU: the card photo's file stem
(page01_r0c2). That SKU is how a sale finds its card. For each sale line:

  1. Already seen (source + external id)? Skip. Reading twice records once.
  2. Find the card with that SKU in the library of the account the sale
     belongs to. Take the copies out, cancel its marketplace listing and any
     unpaid order on it (the card is gone), and write a 'sold' ledger row.
  3. No such card? Keep the line as 'unmatched' so a person can deal with
     it, and say why. Nothing is guessed from the title.

Whose library: a sale only ever touches the library of the account it came
from. The operator's eBay account and the operator's CSV imports match the
operator's own cards; a user's connected eBay account matches that user's.
A sale can never take a card out of someone else's library.

These routes change libraries, so unlike the rest of the scan bench they
need the operator to be logged in.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError

from webapp import db as bench_db
from webapp.auth import admin_emails, current_user_id, is_operator, is_verified
from webapp.models import (
    LibraryEvent,
    User,
    ExternalSale,
    InventoryItem,
    MarketListing,
    Order,
    get_session,
    log_event,
    utcnow,
)

ROOT = Path(__file__).resolve().parent.parent
BENCH_DB_PATH = str(ROOT / os.getenv("OUTPUT_DIR", "output") / "db.sqlite")

sales_bp = Blueprint("sales", __name__)

CSV_SOURCES = ("whatnot",)
RETURN_CHANNELS = ("ebay", "whatnot")   # 'added' rows written when a sale is undone
MAX_CSV_ROWS = 5000
# Why an unmatched sale still hides its card on the Scan page: it did sell,
# it just was never in a library.
BENCH_ONLY = "this card was scanned but never added to a library"
_sync_lock = threading.Lock()
_state: dict[str, Any] = {"last_sync_at": None, "last_result": None, "last_error": None}


# ----------------------------------------------------------------------
# Matching a SKU to a card
# ----------------------------------------------------------------------

def _stem(path: Optional[str]) -> str:
    return Path(path or "").stem


def operator_ids(s) -> set[int]:
    """Accounts that own the operator's sales: confirmed ADMIN_EMAILS users."""
    emails = admin_emails()
    return {u.id for u in s.query(User).all()
            if u.email.lower() in emails and is_verified(s, u.id)}


def _find_inventory(s, sku: str, owners: set[int], include_empty: bool = False) -> Optional[InventoryItem]:
    """The library card a SKU refers to, among `owners` only. SKUs are the
    crop file stem, or card-inv<id> for a card listed from the library with
    no photo. Returns None when no owner holds it, or when more than one
    does (ambiguous: a person decides, not this code)."""
    if not owners:
        return None
    q = s.query(InventoryItem).filter(InventoryItem.user_id.in_(owners))
    if not include_empty:
        q = q.filter(InventoryItem.quantity > 0)
    m = re.fullmatch(r"card-inv(\d+)", sku)
    if m:
        return q.filter(InventoryItem.id == int(m.group(1))).one_or_none()
    like = sku.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = q.filter(InventoryItem.source_crop_path.like(f"%{like}%", escape="\\")).all()
    exact = [i for i in candidates if _stem(i.source_crop_path) == sku]
    return exact[0] if len(exact) == 1 else None


def _bench_card_id(sku: str) -> Optional[int]:
    """The scan-bench row with this SKU, if any. Read only."""
    try:
        with bench_db.connect(BENCH_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, crop_path FROM cards WHERE crop_path LIKE ?", (f"%{sku}%",)
            ).fetchall()
            hit = next((r for r in rows if _stem(r["crop_path"]) == sku), None)
            return int(hit["id"]) if hit else None
    except Exception as exc:   # the bench DB is optional context, never fatal
        print(f"[sales] bench lookup for {sku}: {exc}", file=sys.stderr)
        return None


def _set_bench_status(card_id: Optional[int], status: Optional[str]) -> None:
    """Flag (or unflag) the bench row, so a sold card is not offered again.
    Only eBay has a status column on the bench."""
    if card_id is None:
        return
    try:
        with bench_db.connect(BENCH_DB_PATH) as conn:
            bench_db.update_card(conn, card_id, {"ebay_listing_status": status})
    except Exception as exc:
        print(f"[sales] bench update for card {card_id}: {exc}", file=sys.stderr)


def _release(s, item: InventoryItem) -> None:
    """The card sold elsewhere: take down its marketplace listing and cancel
    any order still waiting on payment for it."""
    from webapp import marketplace

    for listing in s.query(MarketListing).filter_by(inventory_item_id=item.id, status="active").all():
        for order in s.query(Order).filter_by(listing_id=listing.id, status="pending_payment").all():
            order.status = "cancelled"
            marketplace._expire_session(order)   # noqa: SLF001  close its card checkout
        listing.status = "cancelled"


def sold_stems() -> set[str]:
    """SKUs (crop file stems) of cards that have sold anywhere: read back
    from a site, sold on this marketplace, or recorded by hand. The scan
    bench uses this so a sold card is never published or exported again.

    An unmatched sale only counts when it is known to be one of the bench's
    own cards (BENCH_ONLY). A SKU nobody recognises hides nothing."""
    s = get_session()
    try:
        stems = {row[0] for row in s.query(ExternalSale.sku)
                 .filter(ExternalSale.sku.isnot(None),
                         (ExternalSale.status == "recorded")
                         | (ExternalSale.status.in_(("unmatched", "dismissed"))
                            & (ExternalSale.detail == BENCH_ONLY))).all()}
        # Ledger: copies sold, less copies that came back when a sale was
        # refunded (those 'added' rows carry the site as their channel).
        net: dict[str, int] = {}
        for kind, channel, crop, qty in (
                s.query(LibraryEvent.kind, LibraryEvent.channel, LibraryEvent.crop_path,
                        LibraryEvent.quantity)
                .filter(LibraryEvent.crop_path.isnot(None),
                        LibraryEvent.kind.in_(("sold", "added"))).all()):
            if kind == "sold":
                net[_stem(crop)] = net.get(_stem(crop), 0) + qty
            elif channel in RETURN_CHANNELS:
                net[_stem(crop)] = net.get(_stem(crop), 0) - qty
        stems |= {stem for stem, n in net.items() if n > 0}
        # A recorded sale that was later reversed no longer counts either.
        stems -= {stem for stem, n in net.items() if n <= 0}
    finally:
        s.close()
    stems.discard("")
    return stems


# ----------------------------------------------------------------------
# The recorder
# ----------------------------------------------------------------------

def _undo(s, sale: ExternalSale) -> None:
    """A sale that was recorded has since been cancelled or fully refunded at
    the source: put the card back. The ledger is append-only, so the return
    is its own 'added' row next to the original 'sold' one."""
    event = s.get(LibraryEvent, sale.library_event_id) if sale.library_event_id else None
    if event is not None:
        item = _find_inventory(s, sale.sku or "", {event.user_id}, include_empty=True)
        if item is None:
            item = InventoryItem(
                user_id=event.user_id, catalog_card_id=event.catalog_card_id, quantity=0,
                condition=event.condition or "NM", acquired_price=event.unit_cost,
                source_crop_path=event.crop_path, image_url=event.image_url)
            s.add(item)
        item.quantity += event.quantity
        log_event(
            s, event.user_id, event.catalog_card_id, "added", channel=sale.source,
            quantity=event.quantity, condition=event.condition, image_url=event.image_url,
            crop_path=event.crop_path, note=f"sale {sale.external_id} was refunded or cancelled",
        )
    sale.status, sale.detail = "void", "refunded or cancelled after it was recorded; the card was put back"


def _net_unit_price(line: dict, quantity: int) -> Optional[float]:
    price = line.get("unit_price")
    if price is None:
        return None
    return round(max(price * quantity - (line.get("refunded") or 0.0), 0.0) / quantity, 2)


def _apply_partial_refund(s, sale: ExternalSale, line: dict) -> bool:
    """A recorded sale now shows money refunded (or more than before): bring
    the recorded price down to what the seller kept. The ledger row for the
    sale is corrected in place and says why, so Sold shows the true take."""
    net = _net_unit_price(line, sale.quantity)
    if net is None or sale.unit_price is None or abs(net - sale.unit_price) < 0.005:
        return False
    event = s.get(LibraryEvent, sale.library_event_id) if sale.library_event_id else None
    if event is not None:
        event.unit_price = net
        event.note = (f"read back from {sale.source}; partly refunded, "
                      f"{(line.get('refunded') or 0.0):.2f} given back")
    sale.unit_price = net
    sale.detail = f"partly refunded: {(line.get('refunded') or 0.0):.2f} given back"
    return True


def record_sale_lines(lines: list[dict], source: str, owners: Optional[set[int]] = None,
                      operator_account: bool = True) -> dict:
    """Record sale lines. Each line: {external_id, sku, title, quantity,
    unit_price, fees, sold_at, paid, void}. Returns counts.

    owners: the accounts whose libraries these sales may touch (default: the
    operator's). operator_account: the sales come from the operator's own
    listings, which are the scan bench's cards, so the bench row is flagged
    too. A user's own eBay account never touches the bench.
    """
    out = {"recorded": 0, "unmatched": 0, "duplicates": 0, "void": 0, "not_paid_yet": 0,
           "reversed": 0, "adjusted": 0, "cards_sold": 0, "total": 0.0}
    for line in lines:
        external_id = str(line.get("external_id") or "").strip()[:160]
        if not external_id:
            continue
        s = get_session()
        try:
            who = owners if owners is not None else operator_ids(s)
            sku = (line.get("sku") or "").strip()
            bench_id = _bench_card_id(sku) if (sku and operator_account) else None
            existing = s.query(ExternalSale).filter_by(source=source, external_id=external_id).one_or_none()
            if existing is not None:
                if line.get("void") and existing.status == "recorded":
                    _undo(s, existing)
                    s.commit()
                    _set_bench_status(bench_id, None)
                    out["reversed"] += 1
                elif existing.status == "recorded" and _apply_partial_refund(s, existing, line):
                    s.commit()
                    out["adjusted"] += 1
                else:
                    out["duplicates"] += 1
                continue
            if not line.get("void") and not line.get("paid", True):
                out["not_paid_yet"] += 1      # not stored: picked up once it is paid
                continue

            quantity = max(1, int(line.get("quantity") or 1))
            sale = ExternalSale(
                source=source, external_id=external_id, sku=sku or None,
                title=line.get("title"), quantity=quantity,
                # What the seller kept per card: the price less anything
                # already refunded when the sale is first seen.
                unit_price=_net_unit_price(line, quantity), fees=line.get("fees"),
                sold_at=line.get("sold_at") or utcnow(),
            )
            mark_bench, qty = False, 0
            if line.get("void"):
                sale.status, sale.detail = "void", "cancelled or refunded at the source"
                counted = "void"
                out["void"] += 1
            else:
                item = _find_inventory(s, sku, who) if sku else None
                if item is None:
                    sale.status = "unmatched"
                    sale.detail = (
                        "no SKU on this sale, so it cannot be tied to a card" if not sku
                        else BENCH_ONLY if bench_id is not None
                        else "no card with this SKU in the library of the account that sold it")
                    mark_bench = bench_id is not None
                    counted = "unmatched"
                    out["unmatched"] += 1
                else:
                    qty = min(sale.quantity, item.quantity)
                    _release(s, item)
                    event = log_event(
                        s, item.user_id, item.catalog_card_id, "sold",
                        channel=source, quantity=qty, unit_price=sale.unit_price,
                        unit_cost=item.acquired_price, fees=sale.fees,
                        condition=item.condition, image_url=item.image_url,
                        crop_path=item.source_crop_path,
                        note=f"read back from {source}", occurred_at=sale.sold_at,
                    )
                    item.quantity -= qty
                    s.flush()
                    sale.status, sale.user_id, sale.library_event_id = "recorded", item.user_id, event.id
                    if qty < sale.quantity:
                        sale.detail = f"sold {sale.quantity} but the library held {qty}"
                    mark_bench = bench_id is not None
                    counted = "recorded"
                    out["recorded"] += 1
                    out["cards_sold"] += qty
                    out["total"] += (sale.unit_price or 0) * qty
            s.add(sale)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()              # another sync recorded it first
                out[counted] -= 1
                if counted == "recorded":
                    out["cards_sold"] -= qty
                    out["total"] -= (line.get("unit_price") or 0) * qty
                out["duplicates"] += 1
                continue
            # Only once the sale is safely stored, and only for eBay, which
            # is the one site the bench tracks a status for.
            if mark_bench and source == "ebay":
                _set_bench_status(bench_id, "sold")
        finally:
            s.close()
    out["total"] = round(max(out["total"], 0.0), 2)
    return out


# ----------------------------------------------------------------------
# eBay
# ----------------------------------------------------------------------

def sync_ebay(days: int = 30) -> dict:
    """Pull recent orders for the operator's eBay account and for every user
    who connected their own. One sync at a time."""
    from lib import ebay_orders
    from webapp import connections

    since = utcnow() - timedelta(days=max(1, min(int(days), 730)))
    accounts: list[tuple[str, Any, Optional[int]]] = []
    try:
        auth = connections._ebay_auth()   # noqa: SLF001
        if auth.is_authorized():
            accounts.append(("your eBay account", auth, None))
    except Exception as exc:
        print(f"[sales] operator eBay auth: {exc}", file=sys.stderr)
    try:
        from webapp.settings import UserEbayKeys, _auth_for
        s = get_session()
        try:
            for keys in s.query(UserEbayKeys).all():
                user_auth = _auth_for(keys.user_id, keys)
                if user_auth.is_authorized():
                    accounts.append((f"user {keys.user_id}", user_auth, keys.user_id))
        finally:
            s.close()
    except Exception as exc:
        print(f"[sales] per-user eBay accounts: {exc}", file=sys.stderr)

    if not accounts:
        return {"ok": False, "error": "eBay is not connected. Connect it under Connections."}

    # eBay can take a while to answer. A second sync started meanwhile would
    # only repeat the same requests, so it is turned away instead of queued.
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "an eBay sync is already running; try again in a minute"}
    try:
        totals = {"recorded": 0, "unmatched": 0, "duplicates": 0, "void": 0, "not_paid_yet": 0,
                  "reversed": 0, "adjusted": 0, "cards_sold": 0, "total": 0.0}
        problems: list[str] = []
        for label, auth, owner in accounts:
            try:
                lines = ebay_orders.fetch_sale_lines(auth, since=since)
            except ebay_orders.EbayOrdersError as exc:
                problems.append(f"{label}: {exc}")
                continue
            except Exception as exc:
                problems.append(f"{label}: could not reach eBay ({exc})")
                continue
            # A user's own account only ever matches that user's cards, and
            # never touches the bench. The operator's matches the operator's.
            if owner is None:
                result = record_sale_lines(lines, "ebay")
            else:
                result = record_sale_lines(lines, "ebay", owners={owner}, operator_account=False)
            for k in totals:
                totals[k] += result[k]
        totals["total"] = round(totals["total"], 2)
        out = {"ok": len(problems) < len(accounts),
               "accounts": len(accounts), "problems": problems, **totals}
        _state.update(last_sync_at=utcnow().isoformat() + "Z", last_result=out,
                      last_error="; ".join(problems) or None)
        return out
    finally:
        _sync_lock.release()


def start_background(interval_minutes: Optional[int] = None) -> None:
    """Sync eBay sales on a timer. EBAY_SALES_SYNC_MINUTES=0 turns it off."""
    minutes = interval_minutes if interval_minutes is not None else int(
        os.getenv("EBAY_SALES_SYNC_MINUTES", "30"))
    if minutes <= 0:
        return

    def loop():
        import time
        while True:
            time.sleep(minutes * 60)
            try:
                result = sync_ebay()
                if result.get("recorded"):
                    print(f"[sales] eBay sync recorded {result['recorded']} sale(s)", file=sys.stderr)
            except Exception as exc:
                print(f"[sales] eBay sync failed: {exc}", file=sys.stderr)

    threading.Thread(target=loop, daemon=True, name="ebay-sales-sync").start()


# ----------------------------------------------------------------------
# CSV import (Whatnot, TCGplayer)
# ----------------------------------------------------------------------

# Header spellings seen across seller exports, most specific first. Matching
# is on the header with case, spaces and punctuation removed.
_COLUMNS = {
    "sku": ("customlabelsku", "sku", "customlabel", "productsku", "itemsku"),
    "order": ("orderid", "ordernumber", "orderno", "order", "transactionid", "saleid"),
    "title": ("producttitle", "listingtitle", "productname", "itemname", "title", "product", "item", "name"),
    "quantity": ("quantitysold", "quantity", "qty", "units"),
    "price": ("soldprice", "saleprice", "itemprice", "unitprice", "price", "subtotal", "total"),
    "fees": ("totalfees", "fees", "fee", "commission"),
    "date": ("solddate", "saledate", "orderdate", "datesold", "createdat", "date"),
}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _amount(value) -> Optional[float]:
    text = re.sub(r"[^0-9.\-]", "", str(value or ""))
    try:
        return float(text) if text not in ("", "-", ".") else None
    except ValueError:
        return None


def _date(value) -> Optional[datetime]:
    text = str(value or "").strip().replace("Z", "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m/%d/%y",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_sales_csv(text: str) -> tuple[list[dict], dict]:
    """Turn a seller's sales export into sale lines. Returns (lines, info);
    info says which columns were used so the user can see what was read."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = reader.fieldnames or []
    by_norm = {_norm_header(h): h for h in headers}
    cols = {}
    for field, names in _COLUMNS.items():
        cols[field] = next((by_norm[n] for n in names if n in by_norm), None)
    info = {"columns": cols, "rows": 0, "skipped": 0}
    if not cols["sku"]:
        raise ValueError(
            "no SKU column found (looked for: SKU, Custom label). The SKU is how a sale is "
            f"matched to a card. Columns in this file: {', '.join(headers) or 'none'}")
    if not cols["price"]:
        raise ValueError(
            f"no price column found. Columns in this file: {', '.join(headers)}")

    lines = []
    seen: dict[str, int] = {}
    for row in reader:
        info["rows"] += 1
        if info["rows"] > MAX_CSV_ROWS:
            raise ValueError(f"that file has more than {MAX_CSV_ROWS} rows; export a shorter date range")
        sku = (row.get(cols["sku"]) or "").strip()
        price = _amount(row.get(cols["price"]))
        if not sku or price is None:
            info["skipped"] += 1
            continue
        try:
            qty = max(1, int(float(row.get(cols["quantity"]) or 1))) if cols["quantity"] else 1
        except ValueError:
            qty = 1
        order = (row.get(cols["order"]) or "").strip() if cols["order"] else ""
        # Without an order id, the row's own content is its identity, so the
        # same sale gets the same id wherever it sits in a later export. Two
        # truly identical rows in one file are told apart by a counter.
        base = f"{order}:{sku}" if order else f"row:{sku}:{price}:{row.get(cols['date'] or '', '')}"
        seen[base] = seen.get(base, 0) + 1
        lines.append({
            "external_id": base if seen[base] == 1 else f"{base}#{seen[base]}",
            "sku": sku,
            "title": (row.get(cols["title"]) or "").strip() if cols["title"] else None,
            "quantity": qty,
            "unit_price": round(price, 2),
            "fees": _amount(row.get(cols["fees"])) if cols["fees"] else None,
            "sold_at": _date(row.get(cols["date"])) if cols["date"] else None,
            "paid": True,
            "void": False,
        })
    return lines, info


# ----------------------------------------------------------------------
# Routes. On the Scan page, but operator only: they change libraries.
# ----------------------------------------------------------------------

def _operator_or_error():
    """(uid, None) for a logged-in operator, else (None, response)."""
    uid = current_user_id()
    if not uid:
        return None, (jsonify({"error": "Log in as the operator to read sales back.",
                               "code": "login_required"}), 401)
    s = get_session()
    try:
        ok = is_operator(s, uid)
    finally:
        s.close()
    if not ok:
        return None, (jsonify({"error": "Only the operator (a confirmed address in ADMIN_EMAILS) "
                                        "can read sales back.", "code": "operator_only"}), 403)
    return uid, None


def _sale_dict(sale: ExternalSale) -> dict:
    return {
        "id": sale.id, "source": sale.source, "external_id": sale.external_id,
        "sku": sale.sku, "title": sale.title, "quantity": sale.quantity,
        "unit_price": sale.unit_price, "fees": sale.fees, "status": sale.status,
        "detail": sale.detail,
        "sold_at": sale.sold_at.isoformat() + "Z" if sale.sold_at else None,
    }


@sales_bp.route("/api/sales/status")
def sales_status():
    from lib.ebay_oauth import SALES_SCOPE
    from webapp import connections

    uid, error = _operator_or_error()
    if error:
        return error
    try:
        auth = connections._ebay_auth()   # noqa: SLF001
        connected, can_read = auth.is_authorized(), auth.has_scope(SALES_SCOPE)
    except Exception:
        connected = can_read = False
    s = get_session()
    try:
        unmatched = (s.query(ExternalSale).filter_by(status="unmatched")
                     .order_by(ExternalSale.sold_at.desc()).limit(200).all())
        recent = (s.query(ExternalSale).filter_by(status="recorded")
                  .order_by(ExternalSale.sold_at.desc()).limit(20).all())
        out = {
            "ebay": {"connected": connected, "can_read_sales": can_read},
            "unmatched": [_sale_dict(x) for x in unmatched],
            "recent": [_sale_dict(x) for x in recent],
            "sync_minutes": int(os.getenv("EBAY_SALES_SYNC_MINUTES", "30")),
            **_state,
        }
    finally:
        s.close()
    return jsonify(out)


@sales_bp.route("/api/sales/sync/ebay", methods=["POST"])
def sales_sync_ebay():
    uid, error = _operator_or_error()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days") or 30)
    except (TypeError, ValueError):
        return jsonify({"error": "days must be a number"}), 400
    result = sync_ebay(days=days)
    return jsonify(result), (200 if result.get("ok") else 409)


@sales_bp.route("/api/sales/import", methods=["POST"])
def sales_import():
    """multipart: file=<csv>, source=whatnot. dry_run=1 reads the file and
    reports what it found without recording anything. Sales are matched
    against the library of the operator doing the import, nobody else's."""
    uid, error = _operator_or_error()
    if error:
        return error
    source = (request.form.get("source") or "").lower()
    if source not in CSV_SOURCES:
        return jsonify({"error": f"source must be one of: {', '.join(CSV_SOURCES)}"}), 400
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "choose the sales CSV exported from the site"}), 400
    raw = upload.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        return jsonify({"error": "that file is over 5 MB; export a shorter date range"}), 400
    try:
        lines, info = parse_sales_csv(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if request.form.get("dry_run") == "1":
        s = get_session()
        try:
            seen = sum(1 for l in lines if s.query(ExternalSale).filter_by(
                source=source, external_id=l["external_id"][:160]).count())
        finally:
            s.close()
        return jsonify({"dry_run": True, "sales": len(lines), "already_imported": seen,
                        "total": round(sum(l["unit_price"] * l["quantity"] for l in lines), 2), **info})
    result = record_sale_lines(lines, source, owners={uid})
    return jsonify({"dry_run": False, "sales": len(lines), **info, **result})


@sales_bp.route("/api/sales/external/<int:sale_id>/dismiss", methods=["POST"])
def sales_dismiss(sale_id: int):
    """Stop showing an unmatched sale (it was handled by hand, or is not ours)."""
    uid, error = _operator_or_error()
    if error:
        return error
    s = get_session()
    try:
        sale = s.query(ExternalSale).filter_by(id=sale_id, status="unmatched").one_or_none()
        if sale is None:
            return jsonify({"error": "no such unmatched sale"}), 404
        sale.status = "dismissed"
        s.commit()
    finally:
        s.close()
    return jsonify({"dismissed": sale_id})
