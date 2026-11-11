"""Read sales back from the places cards are listed, into the library.

Two ways in, one recorder:

  eBay       the Fulfillment API (lib/ebay_orders.py), for the operator's
             account and for each user who connected their own. Runs on a
             timer and from "Sync eBay sales" on the Scan page.
  Whatnot / TCGplayer
             neither has an orders API for regular sellers, so sales come in
             as the CSV each site lets a seller export. "Import sales CSV".

Every listing this app creates carries a SKU: the card photo's file stem
(page01_r0c2). That SKU is how a sale finds its card. For each sale line:

  1. Already seen (source + external id)? Skip. Reading twice records once.
  2. Find the library card with that SKU. Take the copies out, cancel its
     marketplace listing and any unpaid order on it (the card is gone), and
     write a 'sold' row in the ledger with the price and fees.
  3. No library card? Keep the line as 'unmatched' so a person can deal with
     it, and say why. Nothing is guessed from the title.

The scan bench's own row is marked sold either way, so the Scan page stops
offering the card.
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
from webapp.models import (
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

SOURCES = ("ebay", "whatnot", "tcgplayer")
_sync_lock = threading.Lock()
_state: dict[str, Any] = {"last_sync_at": None, "last_result": None, "last_error": None}


# ----------------------------------------------------------------------
# Matching a SKU to a card
# ----------------------------------------------------------------------

def _stem(path: Optional[str]) -> str:
    return Path(path or "").stem


def _find_inventory(s, sku: str, owner_uid: Optional[int]) -> Optional[InventoryItem]:
    """The held library card a SKU refers to. SKUs are the crop file stem,
    or card-inv<id> for a card listed from the library with no photo."""
    q = s.query(InventoryItem).filter(InventoryItem.quantity > 0)
    if owner_uid is not None:
        q = q.filter(InventoryItem.user_id == owner_uid)
    m = re.fullmatch(r"card-inv(\d+)", sku)
    if m:
        return q.filter(InventoryItem.id == int(m.group(1))).one_or_none()
    candidates = q.filter(InventoryItem.source_crop_path.like(f"%{sku}%")).all()
    exact = [i for i in candidates if _stem(i.source_crop_path) == sku]
    return exact[0] if exact else None


def _mark_bench_sold(sku: str, source: str) -> bool:
    """Flag the scan-bench row so the card is not offered for listing again.
    Only eBay has a status column on the bench."""
    try:
        with bench_db.connect(BENCH_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, crop_path FROM cards WHERE crop_path LIKE ?", (f"%{sku}%",)
            ).fetchall()
            hit = next((r for r in rows if _stem(r["crop_path"]) == sku), None)
            if hit is None:
                return False
            if source == "ebay":
                bench_db.update_card(conn, int(hit["id"]), {"ebay_listing_status": "sold"})
            return True
    except Exception as exc:   # the bench DB is optional context, never fatal
        print(f"[sales] bench lookup for {sku}: {exc}", file=sys.stderr)
        return False


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
    bench uses this so a sold card is never published or exported again."""
    from webapp.models import LibraryEvent

    s = get_session()
    try:
        stems = {row[0] for row in s.query(ExternalSale.sku)
                 .filter(ExternalSale.status.in_(("recorded", "unmatched", "dismissed")),
                         ExternalSale.sku.isnot(None)).all()}
        stems |= {_stem(row[0]) for row in s.query(LibraryEvent.crop_path)
                  .filter(LibraryEvent.kind == "sold", LibraryEvent.crop_path.isnot(None)).all()}
    finally:
        s.close()
    stems.discard("")
    return stems


# ----------------------------------------------------------------------
# The recorder
# ----------------------------------------------------------------------

def record_sale_lines(lines: list[dict], source: str, owner_uid: Optional[int] = None) -> dict:
    """Record sale lines. Each line: {external_id, sku, title, quantity,
    unit_price, fees, sold_at, paid, void}. Returns counts."""
    out = {"recorded": 0, "unmatched": 0, "duplicates": 0, "void": 0, "not_paid_yet": 0,
           "cards_sold": 0, "total": 0.0}
    for line in lines:
        external_id = str(line.get("external_id") or "").strip()
        if not external_id:
            continue
        if not line.get("void") and not line.get("paid", True):
            out["not_paid_yet"] += 1      # picked up by a later sync once paid
            continue
        s = get_session()
        try:
            if s.query(ExternalSale).filter_by(source=source, external_id=external_id).count():
                out["duplicates"] += 1
                continue
            sku = (line.get("sku") or "").strip()
            sale = ExternalSale(
                source=source, external_id=external_id[:160], sku=sku or None,
                title=line.get("title"), quantity=max(1, int(line.get("quantity") or 1)),
                unit_price=line.get("unit_price"), fees=line.get("fees"),
                sold_at=line.get("sold_at") or utcnow(),
            )
            if line.get("void"):
                sale.status, sale.detail = "void", "cancelled or refunded at the source"
                out["void"] += 1
            else:
                item = _find_inventory(s, sku, owner_uid) if sku else None
                on_bench = _mark_bench_sold(sku, source) if sku else False
                if item is None:
                    sale.status = "unmatched"
                    sale.detail = (
                        "no SKU on this sale, so it cannot be tied to a card" if not sku
                        else "this card was scanned but never added to a library" if on_bench
                        else "no card in any library has this SKU")
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
                    out["recorded"] += 1
                    out["cards_sold"] += qty
                    out["total"] += (sale.unit_price or 0) * qty
            s.add(sale)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()              # another sync recorded it first
                out["duplicates"] += 1
        finally:
            s.close()
    out["total"] = round(out["total"], 2)
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

    with _sync_lock:
        totals = {"recorded": 0, "unmatched": 0, "duplicates": 0, "void": 0, "not_paid_yet": 0,
                  "cards_sold": 0, "total": 0.0}
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
            # A per-user account only ever matches that user's cards.
            result = record_sale_lines(lines, "ebay", owner_uid=owner)
            for k in totals:
                totals[k] += result[k]
        totals["total"] = round(totals["total"], 2)
        out = {"ok": not problems or totals["recorded"] > 0 or len(problems) < len(accounts),
               "accounts": len(accounts), "problems": problems, **totals}
        _state.update(last_sync_at=utcnow().isoformat() + "Z", last_result=out,
                      last_error="; ".join(problems) or None)
        return out


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
    for n, row in enumerate(reader, start=2):
        info["rows"] += 1
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
        lines.append({
            # Without an order id, the row's own content is its identity, so
            # importing the same file twice still records each sale once.
            "external_id": f"{order}:{sku}" if order else f"row:{sku}:{price}:{row.get(cols['date'] or '', '')}:{n}",
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
# Routes (scan bench: local operator, guarded by the same-origin hook)
# ----------------------------------------------------------------------

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
            **_state,
        }
    finally:
        s.close()
    return jsonify(out)


@sales_bp.route("/api/sales/sync/ebay", methods=["POST"])
def sales_sync_ebay():
    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days") or 30)
    except (TypeError, ValueError):
        return jsonify({"error": "days must be a number"}), 400
    result = sync_ebay(days=days)
    return jsonify(result), (200 if result.get("ok") else 409)


@sales_bp.route("/api/sales/import", methods=["POST"])
def sales_import():
    """multipart: file=<csv>, source=whatnot|tcgplayer. dry_run=1 reads the
    file and reports what it found without recording anything."""
    source = (request.form.get("source") or "").lower()
    if source not in ("whatnot", "tcgplayer"):
        return jsonify({"error": "source must be whatnot or tcgplayer"}), 400
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
    result = record_sale_lines(lines, source)
    return jsonify({"dry_run": False, "sales": len(lines), **info, **result})


@sales_bp.route("/api/sales/external/<int:sale_id>/dismiss", methods=["POST"])
def sales_dismiss(sale_id: int):
    """Stop showing an unmatched sale (it was handled by hand, or is not ours)."""
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
