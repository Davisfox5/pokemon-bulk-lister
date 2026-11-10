"""Flask web app for the Pokémon bulk-lister pipeline.

Single-page UI backed by SQLite. Wires existing pipeline scripts:
  - 01_split_grids.process_image() for upload-time cropping
  - lib.tcgplayer_client + lib.ebay_client + lib.pricing for per-card pricing
  - lib.cloudinary_client for image upload
  - scripts/05_generate_csvs for CSV export

Run:
    pip install -r requirements.txt
    python -m webapp.app           # http://localhost:5050

Or:
    flask --app webapp.app run --port 5050
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import cv2
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

# Allow direct python -m webapp.app from project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.cardmarket_client import CardmarketPriceGuide  # noqa: E402
from lib.ebay_client import EbayAuthError, EbayClient  # noqa: E402
from lib.pricing import aggregate  # noqa: E402
from lib.tcgplayer_client import TCGPlayerClient  # noqa: E402
from webapp import db  # noqa: E402


def _load_split_grids():
    """Load the numbered split-grids script as a module."""
    path = ROOT / "scripts" / "01_split_grids.py"
    spec = importlib.util.spec_from_file_location("split_grids", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_csv_generator():
    path = ROOT / "scripts" / "05_generate_csvs.py"
    spec = importlib.util.spec_from_file_location("generate_csvs", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


load_dotenv(ROOT / ".env")

INPUT_DIR = ROOT / os.getenv("INPUT_DIR", "input/grids")
OUTPUT_DIR = ROOT / os.getenv("OUTPUT_DIR", "output")
CROPS_DIR = OUTPUT_DIR / "crops"
CSVS_DIR = OUTPUT_DIR / "csvs"
DB_PATH = str(OUTPUT_DIR / "db.sqlite")

EUR_USD_RATE = float(os.getenv("EUR_USD_RATE", "1.08"))

# Cardmarket daily price-guide JSON as a fallback signal when pokemontcg.io
# has no data (its catalog lags new sets and its CDN 404s under load).
CARDMARKET_FALLBACK = os.getenv("CARDMARKET_FALLBACK", "1") == "1"

# Cards confidently priced below this skip the Terapeak scrape in run-all —
# it's slow (~5s/card) and carries account risk, and a 365-day comp adds
# nothing to a $0.15 bulk common. 0 disables the gate.
TERAPEAK_MIN_VALUE = float(os.getenv("TERAPEAK_MIN_VALUE", "3.0"))

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

# Lazy singletons
_tcg_client: Optional[TCGPlayerClient] = None
_ebay_client: Optional[EbayClient] = None
_split_grids = None
_csv_gen = None

# Background job state (single in-process slot — fine for a one-off personal app).
_job_lock = threading.Lock()
_job_state: dict[str, Any] = {"running": False, "progress": 0, "total": 0, "message": ""}

# Separate slot for publish jobs so a publish run doesn't clobber pricing state.
_publish_lock = threading.Lock()
_publish_state: dict[str, Any] = {"running": False, "progress": 0, "total": 0, "message": "", "site": ""}


# ----------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------

def _init() -> None:
    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    CSVS_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db(DB_PATH)
    with db.connect(DB_PATH) as conn:
        imported = db.maybe_import_legacy_json(conn)
        if imported:
            print(f"[webapp] imported {imported} cards from legacy JSON")


_init()


def get_tcg() -> TCGPlayerClient:
    global _tcg_client
    if _tcg_client is None:
        _tcg_client = TCGPlayerClient()
    return _tcg_client


def get_ebay() -> EbayClient:
    global _ebay_client
    if _ebay_client is None:
        _ebay_client = EbayClient()
    return _ebay_client


_cardmarket_guide: Optional[CardmarketPriceGuide] = None


def get_cardmarket() -> CardmarketPriceGuide:
    global _cardmarket_guide
    if _cardmarket_guide is None:
        _cardmarket_guide = CardmarketPriceGuide(cache_dir=str(OUTPUT_DIR / "cache" / "cardmarket"))
    return _cardmarket_guide


def get_split_grids():
    global _split_grids
    if _split_grids is None:
        _split_grids = _load_split_grids()
    return _split_grids


def get_csv_gen():
    global _csv_gen
    if _csv_gen is None:
        _csv_gen = _load_csv_generator()
    return _csv_gen


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ----------------------------------------------------------------------
# Cards
# ----------------------------------------------------------------------

@app.route("/api/cards")
def api_list_cards():
    sort = request.args.get("sort", "confidence_asc")
    needs_review_only = request.args.get("needs_review") == "1"
    unidentified_only = request.args.get("unidentified") == "1"
    with db.connect(DB_PATH) as conn:
        rows = db.list_cards(
            conn,
            sort=sort,
            needs_review_only=needs_review_only,
            unidentified_only=unidentified_only,
        )
        stats = db.card_stats(conn)
    return jsonify({"cards": rows, "stats": stats})


@app.route("/api/cards/<int:card_id>", methods=["GET"])
def api_get_card(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    return jsonify(card)


@app.route("/api/cards/<int:card_id>", methods=["PATCH"])
def api_patch_card(card_id: int):
    patch = request.get_json(silent=True) or {}
    with db.connect(DB_PATH) as conn:
        card = db.update_card(conn, card_id, patch)
    if not card:
        abort(404)
    return jsonify(card)


@app.route("/api/cards/bulk", methods=["POST"])
def api_bulk_update_cards():
    """Bulk update — paste a JSON list with crop_path keys to apply identifications.

    Body:
      [{"crop_path": "output/crops/page01_r0c0.jpg", "name": "Charizard", ...}, ...]
    """
    body = request.get_json(silent=True) or []
    if not isinstance(body, list):
        abort(400, "expected a JSON list")

    applied = 0
    with db.connect(DB_PATH) as conn:
        for entry in body:
            crop_path = entry.get("crop_path")
            if not crop_path:
                continue
            row = conn.execute("SELECT id FROM cards WHERE crop_path = ?", (crop_path,)).fetchone()
            if not row:
                continue
            patch = {k: v for k, v in entry.items() if k != "crop_path"}
            db.update_card(conn, int(row["id"]), patch)
            applied += 1
    return jsonify({"applied": applied})


# ----------------------------------------------------------------------
# Upload + split
# ----------------------------------------------------------------------

@app.route("/api/grids/upload", methods=["POST"])
def api_upload_grid():
    """Form fields: file (repeatable), mode = "grid" (binder page, default)
    or "single" (each photo is one card)."""
    files = request.files.getlist("file") or []
    if not files:
        abort(400, "no file uploaded")
    single = request.form.get("mode") == "single"

    sg = get_split_grids()
    total_crops = 0
    grids_added = []
    skipped = []

    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            skipped.append(file.filename or "")
            continue
        safe_name = _unique_upload_name(_safe_filename(file.filename or "grid.jpg"))
        dest = INPUT_DIR / safe_name
        file.save(dest)

        n = _record_single(sg, dest) if single else _split_and_record(sg, dest)
        total_crops += n
        grids_added.append({"filename": safe_name, "crops": n})

    return jsonify({"grids": grids_added, "total_crops": total_crops, "skipped": skipped})


def _unique_upload_name(name: str) -> str:
    """Phone cameras reuse names like image.jpg; a second upload with the same
    name must not overwrite the first photo or collide with its crops."""
    path = Path(name)
    candidate, n = name, 1
    while (INPUT_DIR / candidate).exists():
        n += 1
        candidate = f"{path.stem}-{n}{path.suffix}"
    return candidate


def _record_single(sg, photo_path: Path) -> int:
    """One photo, one card: find the card's outline and square it up. Falls
    back to the whole frame when no outline is found."""
    img = cv2.imread(str(photo_path))
    if img is None:
        return 0
    if img.shape[1] > img.shape[0]:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    quad = sg._detect_card_in_cell(img)
    if quad is not None:
        card = sg._warp_card_quad(img, quad)
    else:
        scale = 671 / img.shape[0]
        card = cv2.resize(img, (max(1, int(img.shape[1] * scale)), 671))
    stem = photo_path.stem
    out_path = CROPS_DIR / f"{stem}_r0c0.jpg"
    cv2.imwrite(str(out_path), card, [cv2.IMWRITE_JPEG_QUALITY, 92])
    with db.connect(DB_PATH) as conn:
        grid_id = db.get_or_create_grid(conn, stem, str(photo_path.relative_to(ROOT)))
        db.insert_card_stub(conn, grid_id, str(out_path.relative_to(ROOT)), 0, 0)
        db.update_grid_count(conn, grid_id)
    return 1


def _split_and_record(sg, grid_path: Path) -> int:
    img = cv2.imread(str(grid_path))
    if img is None:
        return 0
    img = sg.auto_orient(img)
    # Page-level identification reads this copy: its orientation matches the
    # row/col the crops are filed under, which the raw upload may not.
    pages_dir = OUTPUT_DIR / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(pages_dir / f"{grid_path.stem}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    cells, method = sg.split_page(img)
    print(f"[split] {grid_path.name}: {sum(1 for c in cells if c is not None)} crops via {method}", file=sys.stderr)

    stem = grid_path.stem
    written = 0
    with db.connect(DB_PATH) as conn:
        grid_id = db.get_or_create_grid(conn, stem, str(grid_path.relative_to(ROOT)))
        for idx, cell in enumerate(cells):
            if cell is None:
                continue
            r, c = divmod(idx, 3)
            out_path = CROPS_DIR / f"{stem}_r{r}c{c}.jpg"
            cv2.imwrite(str(out_path), cell, [cv2.IMWRITE_JPEG_QUALITY, 92])
            rel = str(out_path.relative_to(ROOT))
            db.insert_card_stub(conn, grid_id, rel, r, c)
            written += 1
        db.update_grid_count(conn, grid_id)
    return written


# ----------------------------------------------------------------------
# Pricing
# ----------------------------------------------------------------------

@app.route("/api/cards/<int:card_id>/price", methods=["POST"])
def api_price_card(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    if not card.get("name"):
        return jsonify({"error": "card has no name — identify it first"}), 400
    try:
        result = _price_card(card)
        with db.connect(DB_PATH) as conn:
            updated = db.update_card(conn, card_id, result)
        return jsonify(updated)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pricing/run-all", methods=["POST"])
def api_run_pricing_all():
    """?terapeak=1 adds the 365-day comps pass. ?only_unpriced=1 skips cards
    that already have a final price."""
    body = request.get_json(silent=True) or {}
    use_terapeak = request.args.get("terapeak") == "1" or body.get("terapeak")
    only_unpriced = request.args.get("only_unpriced") == "1" or bool(body.get("only_unpriced"))
    with _job_lock:
        if _job_state["running"]:
            return jsonify({"error": "another job is running"}), 409
        _job_state.update(
            running=True, progress=0, total=0,
            message="starting…", terapeak=bool(use_terapeak),
        )

    def worker():
        try:
            _pricing_job(use_terapeak, only_unpriced)
        finally:
            _job_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


def _pricing_job(use_terapeak, only_unpriced: bool = False) -> None:
    """Price bench cards. Runs inside a job slot the caller owns: it updates
    progress on _job_state but never touches `running`."""
    n_terapeak_workers = int(os.getenv("TERAPEAK_WORKERS", "4"))
    n_tcg_workers = int(os.getenv("TCG_WORKERS", "6"))

    # Local copy so we can flip it on import error without touching the closure.
    terapeak_on = use_terapeak
    pool = None
    terapeak_thread = None
    pass1_progress = [0]
    pass2_progress = [0]
    progress_lock = threading.Lock()

    try:
        with db.connect(DB_PATH) as conn:
            cards = db.list_cards(conn, sort="newest")
        targets = [c for c in cards if c.get("name")]
        if only_unpriced:
            targets = [c for c in targets if c.get("final_price") is None]
        _job_state["progress"] = 0
        if not targets:
            _job_state["message"] = "nothing to price"
            return
        total_units = len(targets) * (2 if terapeak_on else 1)
        _job_state["total"] = total_units

        # ---- Pass 2 (Terapeak) starts CONCURRENTLY with Pass 1. ----
        # Pass 2 queries depend on resolved set-names from Pass 1, so we
        # gate each Terapeak submission on its card's Pass-1 completion.
        terapeak_ready = None  # unused
        terapeak_queries_ready: dict[int, str] = {}  # card_id -> query string
        queries_lock = threading.Lock()

        def update_message():
            with progress_lock:
                p1 = pass1_progress[0]
                p2 = pass2_progress[0]
                if terapeak_on:
                    _job_state["progress"] = p1 + p2
                    _job_state["message"] = f"TCG/CM {p1}/{len(targets)} · Terapeak {p2}/{len(targets)}"
                else:
                    _job_state["progress"] = p1
                    _job_state["message"] = f"TCG/CM {p1}/{len(targets)}"

        def price_one_pass1(card: dict) -> None:
            """TCG + CM + eBay-MI for one card."""
            tcg_card = None
            skip_terapeak = False
            try:
                patch, tcg_card = _price_card_pass1(card)
                with db.connect(DB_PATH) as conn:
                    db.update_card(conn, int(card["id"]), patch)
                # Value gate: a confident sub-threshold price gains nothing
                # from a slow, account-risky Terapeak scrape.
                fp = patch.get("final_price")
                skip_terapeak = (
                    TERAPEAK_MIN_VALUE > 0
                    and fp is not None
                    and fp < TERAPEAK_MIN_VALUE
                    and not patch.get("needs_review")
                )
            except Exception as exc:
                print(f"[pricing pass1] {card.get('name')}: {exc}", file=sys.stderr)
            finally:
                # ALWAYS enqueue the Terapeak entry — even on error — or the
                # drain thread waits forever for this card and the job hangs
                # with running=True (frontend button stays disabled).
                # None means "skip this card" and is counted, not scraped.
                if terapeak_on:
                    set_name = ((tcg_card or {}).get("set") or {}).get("name") or card.get("set_name") or ""
                    bits = [card.get("name") or "", set_name, card.get("card_number") or "", "pokemon"]
                    query = None if skip_terapeak else " ".join(b for b in bits if b)
                    with queries_lock:
                        terapeak_queries_ready[int(card["id"])] = query
                with progress_lock:
                    pass1_progress[0] += 1
                update_message()

        # Spin Pass 1 across n_tcg_workers threads.
        pass1_executor = ThreadPoolExecutor(max_workers=n_tcg_workers)
        pass1_futures = [pass1_executor.submit(price_one_pass1, c) for c in targets]

        # ---- Pass 2: Terapeak in parallel via TerapeakPool. ----
        if terapeak_on:
            try:
                from lib.terapeak_pool import TerapeakPool
                from lib.terapeak_client import TerapeakNotLoggedIn
            except ImportError as exc:
                _job_state["message"] = f"terapeak import failed: {exc}"
                terapeak_on = False
            else:
                pool = TerapeakPool(n_workers=n_terapeak_workers)

                def on_terapeak(card_id: int, res):
                    if isinstance(res, Exception):
                        print(f"[terapeak] id={card_id}: {res}", file=sys.stderr)
                    else:
                        tp_med = res.get("median")
                        tp_count = res.get("count", 0)
                        try:
                            with db.connect(DB_PATH) as conn:
                                row = db.get_card(conn, card_id) or {}
                            result = aggregate(
                                tcgplayer_market=row.get("tcgplayer_market"),
                                ebay_median_30d=row.get("ebay_median_30d"),
                                ebay_max_30d=row.get("ebay_max_30d"),
                                cardmarket_trend_usd=row.get("cardmarket_trend_usd"),
                                terapeak_median_usd=tp_med,
                            )
                            # Merge with Pass-1 state instead of clobbering it:
                            # Pass 1 may have written match warnings ("⚠") and
                            # clamped confidence — a wrong-card match must stay
                            # low-confidence no matter how well sources agree.
                            existing_notes = row.get("pricing_notes") or ""
                            new_notes = result.notes or ""
                            if new_notes and new_notes not in existing_notes:
                                merged_notes = f"{existing_notes}; {new_notes}" if existing_notes else new_notes
                            else:
                                merged_notes = existing_notes
                            conf = result.confidence
                            needs_review = result.needs_review
                            if "⚠" in existing_notes:
                                conf = min(conf, 0.2)
                                needs_review = True
                            patch = {
                                "terapeak_median_usd": tp_med,
                                "terapeak_sold_count_365d": tp_count,
                                "final_price": result.price,
                                "pricing_confidence": conf,
                                "outlier_flag": result.outlier_flag,
                                "needs_review": needs_review,
                                "pricing_notes": merged_notes,
                            }
                            with db.connect(DB_PATH) as conn:
                                db.update_card(conn, card_id, patch)
                        except Exception as exc:
                            print(f"[terapeak merge] id={card_id}: {exc}", file=sys.stderr)
                    with progress_lock:
                        pass2_progress[0] += 1
                    update_message()

                def drain_terapeak():
                    """Wait for each card's Pass-1 query string, submit to pool, attach callback."""
                    submitted: set[int] = set()
                    futures = {}
                    while len(submitted) < len(targets):
                        # find ready queries that haven't been submitted yet
                        with queries_lock:
                            ready = [
                                (cid, q) for cid, q in terapeak_queries_ready.items()
                                if cid not in submitted
                            ]
                        for cid, q in ready:
                            submitted.add(cid)
                            if q is None:  # value-gated skip — count it, don't scrape
                                with progress_lock:
                                    pass2_progress[0] += 1
                                update_message()
                                continue
                            fut = pool.submit(q, 365)
                            futures[fut] = cid
                        if not ready:
                            time.sleep(0.2)
                    for fut in as_completed(list(futures.keys())):
                        cid = futures[fut]
                        try:
                            res = fut.result()
                        except Exception as exc:
                            res = exc
                        on_terapeak(cid, res)

                terapeak_thread = threading.Thread(target=drain_terapeak, daemon=True)
                terapeak_thread.start()

        # Wait for Pass 1 to complete
        for f in pass1_futures:
            try:
                f.result()
            except Exception:
                pass
        pass1_executor.shutdown(wait=False)

        # Wait for Terapeak drain to finish
        if terapeak_thread is not None:
            terapeak_thread.join()

        _job_state["message"] = f"done, priced {len(targets)} card(s)"
    finally:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass


@app.route("/api/terapeak/status")
def api_terapeak_status():
    state_path = ROOT / os.getenv("TERAPEAK_STATE_PATH", "output/cache/terapeak_state.json")
    return jsonify({"logged_in": state_path.exists(), "state_path": str(state_path)})


@app.route("/api/pricing/status")
def api_pricing_status():
    return jsonify(_job_state)


def _price_card_pass1(card: dict) -> tuple[dict, Optional[dict]]:
    """Pass-1 pricing wrapper: returns (patch, resolved_card)."""
    return _price_card(card, terapeak=None, return_card=True)


def _price_card(card: dict, terapeak=None, return_card: bool = False):
    tcg = get_tcg()
    ebay = get_ebay()

    name = (card.get("name") or "").strip()
    set_name = card.get("set_name") or None
    set_code = card.get("set_code") or None
    card_number = card.get("card_number") or None
    is_holo = bool(card.get("is_holo"))

    tcg_price: Optional[float] = None
    cm_eur: Optional[float] = None
    tcg_card: Optional[dict] = None
    tcg_error: Optional[str] = None
    match_level: Optional[str] = None  # "exact", "no_number", "no_set", "name_only"
    # Try the most specific query first; if it 404s or returns nothing,
    # progressively drop fields. pokemontcg.io has been observed 404-ing
    # narrow queries under load.
    attempts: list[tuple[dict, str]] = [
        ({"set_code": set_code, "card_number": card_number}, "exact"),
        ({"set_code": set_code, "card_number": None},        "no_number"),
        ({"set_code": None, "card_number": card_number},     "no_set"),
        ({"set_code": None, "card_number": None},            "name_only"),
    ]
    seen: set = set()
    for kwargs, level in attempts:
        key = (kwargs.get("set_code"), kwargs.get("card_number"))
        if key in seen:
            continue
        seen.add(key)
        try:
            prices = tcg.lookup_prices(
                name=name,
                set_name=set_name,
                set_code=kwargs.get("set_code"),
                card_number=kwargs.get("card_number"),
                is_holo=is_holo,
            )
            if prices.get("card") is not None:
                tcg_price = prices.get("tcgplayer_market_usd")
                cm_eur = prices.get("cardmarket_trend_eur")
                tcg_card = prices.get("card")
                match_level = level
                tcg_error = None
                break
        except Exception as exc:
            tcg_error = str(exc)
            time.sleep(0.6)
    # Cardmarket daily price-guide fallback: only when pokemontcg.io gave no
    # Cardmarket price. Name-only match, so the guide refuses ambiguous cards.
    cm_fallback_note: Optional[str] = None
    if cm_eur is None and CARDMARKET_FALLBACK and name:
        try:
            cm_eur, cm_fallback_note = get_cardmarket().lookup_trend_eur(name, is_holo=is_holo)
            if cm_eur is None:
                cm_fallback_note = None  # refusal reasons aren't worth surfacing per-card
        except Exception as exc:
            print(f"[cardmarket] {name}: {exc}", file=sys.stderr)

    cm_usd = round(cm_eur * EUR_USD_RATE, 2) if cm_eur else None

    # Accuracy guard: if the match came from a name-only fallback (set_code
    # dropped), flag it so the user knows the match is approximate. Same if
    # TCG and CM disagree wildly — that's a signal the matched product is
    # not the variant the seller actually has.
    match_warning: Optional[str] = None
    if tcg_card is not None:
        matched_set_id = (tcg_card.get("set") or {}).get("id", "")
        if match_level in ("no_set", "name_only"):
            match_warning = (
                f"name-only match to {matched_set_id} — set_code was not provided, "
                "match may not reflect the actual printing"
            )
        if tcg_price and cm_usd and tcg_price > 0 and cm_usd > 0:
            ratio = max(tcg_price, cm_usd) / min(tcg_price, cm_usd)
            if ratio > 3.0:
                match_warning = (
                    (match_warning + "; " if match_warning else "")
                    + f"TCG/CM disagree {ratio:.1f}x on {matched_set_id} — variant mismatch?"
                )

    ebay_stats: dict[str, Any] = {"median": None, "max": None, "count": 0}
    try:
        query = ebay.build_query(name=name, set_name=set_name, card_number=card_number)
        ebay_stats = ebay.sold_stats(query=query, days=30)
    except EbayAuthError as exc:
        print(f"[ebay auth] {exc}", file=sys.stderr)

    terapeak_stats: dict[str, Any] = {"median": None, "count": 0}
    if terapeak is not None:
        try:
            tp_query = " ".join(p for p in [name, set_name, card_number, "pokemon"] if p)
            terapeak_stats = terapeak.search(tp_query, days=365)
        except Exception as exc:
            print(f"[terapeak] {name}: {exc}", file=sys.stderr)

    result = aggregate(
        tcgplayer_market=tcg_price,
        ebay_median_30d=ebay_stats.get("median"),
        ebay_max_30d=ebay_stats.get("max"),
        cardmarket_trend_usd=cm_usd,
        terapeak_median_usd=terapeak_stats.get("median"),
    )

    # Build pricing_notes from all signals so the user has full context.
    notes_parts = []
    if result.notes:
        notes_parts.append(result.notes)
    if tcg_error and tcg_card is None:
        notes_parts.append(f"tcg lookup: {tcg_error}")
    if match_warning:
        notes_parts.append(f"⚠ {match_warning}")
    if match_level and match_level != "exact":
        notes_parts.append(f"match: {match_level}")
    if cm_fallback_note:
        notes_parts.append(cm_fallback_note)

    # A match warning forces low confidence + review regardless of source agreement,
    # because high source agreement on a wrong card is the worst case (looks reliable
    # but mis-prices the listing).
    final_conf = result.confidence
    final_needs_review = result.needs_review
    if match_warning:
        final_conf = min(final_conf, 0.2)
        final_needs_review = True

    patch: dict[str, Any] = {
        "tcgplayer_market": tcg_price,
        "cardmarket_trend_eur": cm_eur,
        "cardmarket_trend_usd": cm_usd,
        "ebay_median_30d": ebay_stats.get("median"),
        "ebay_max_30d": ebay_stats.get("max"),
        "ebay_sold_count_30d": ebay_stats.get("count", 0),
        "terapeak_median_usd": terapeak_stats.get("median"),
        "terapeak_sold_count_365d": terapeak_stats.get("count", 0),
        "final_price": result.price,
        "pricing_confidence": final_conf,
        "outlier_flag": result.outlier_flag,
        "needs_review": final_needs_review,
        "pricing_notes": "; ".join(notes_parts),
    }
    if tcg_card is not None:
        patch["tcgplayer_product_id"] = tcg_card.get("id")
        patch["tcgplayer_url"] = (tcg_card.get("tcgplayer") or {}).get("url")
        patch["cardmarket_url"] = (tcg_card.get("cardmarket") or {}).get("url")
    if return_card:
        return patch, tcg_card
    return patch


# ----------------------------------------------------------------------
# Identification (Claude vision)
# ----------------------------------------------------------------------

# Page-level results below this confidence get a per-crop retry (better
# small-text legibility on the set symbol / collector number).
IDENTIFY_RETRY_CONF = float(os.getenv("VISION_RETRY_CONFIDENCE", "0.6"))

# Free identification before any AI call: (1) learned cache of crops we
# already identified, (2) perceptual-hash match against the official
# pokemontcg.io art index (built by scripts/08_build_art_index.py).
# Both accept only high-confidence matches; everything else falls through
# to the AI, so this can only cut cost, never quality. ART_MATCH=0 disables.
ART_MATCH_ENABLED = os.getenv("ART_MATCH", "1") == "1"
ART_INDEX_DIR = OUTPUT_DIR / "cache" / "art_index" / "catalog"
LEARNED_CACHE_PATH = OUTPUT_DIR / "cache" / "art_index" / "learned.jsonl"
ART_MATCH_CONFIDENCE = 0.9   # reported id_confidence for a catalog art match
LEARN_MIN_CONFIDENCE = 0.6   # only cache AI results at least this confident

_art_lock = threading.Lock()
_art_state: dict[str, Any] = {"index": None, "learned": None}


def _art_matchers():
    with _art_lock:
        if _art_state["index"] is None:
            from lib.art_match import ArtIndex, LearnedCache
            _art_state["index"] = ArtIndex.load(ART_INDEX_DIR)
            _art_state["learned"] = LearnedCache(LEARNED_CACHE_PATH)
        return _art_state["index"], _art_state["learned"]


def _match_crop(crop_path: Path) -> Optional[dict]:
    """Identify a crop by perceptual hash. Returns a vision-shaped result dict
    with a `_source` of "cache" or "match", or None if nothing confident."""
    if not ART_MATCH_ENABLED:
        return None
    from lib.art_match import query_views
    views = query_views(crop_path)
    if views is None:
        return None
    index, learned = _art_matchers()
    hit = learned.match(views)
    source = "cache"
    if hit is None:
        hit = index.match(views)
        source = "match"
    if hit is None:
        return None
    m = hit["meta"]
    return {
        "name": m.get("name") or "",
        "set_name": m.get("set_name") or "",
        "set_code": m.get("set_code") or "",
        "card_number": m.get("card_number") or "",
        "rarity": m.get("rarity") or "",
        "is_holo": bool(m.get("is_holo")),
        "confidence": float(m.get("confidence") or ART_MATCH_CONFIDENCE),
        "_source": source,
    }


def _remember_identification(crop_path: Path, res: dict) -> None:
    """Feed a confident AI identification into the learned cache so duplicate
    cards and re-uploads never pay for AI again."""
    if not ART_MATCH_ENABLED or res.get("_source"):
        return
    if float(res.get("confidence") or 0.0) < LEARN_MIN_CONFIDENCE:
        return
    if not (res.get("name") or "").strip():
        return
    try:
        from lib.art_match import index_descriptor
        desc = index_descriptor(crop_path)
        if desc is None:
            return
        _, learned = _art_matchers()
        learned.add(desc, {
            k: res.get(k)
            for k in ("name", "set_name", "set_code", "card_number",
                      "rarity", "is_holo", "confidence")
        })
    except Exception:
        traceback.print_exc()  # cache failures must never break identification


def _identification_patch(res: dict, card: dict) -> dict[str, Any]:
    num = (res.get("card_number") or "").strip()
    num = num.lstrip("0") or num  # pokemontcg.io numbers carry no leading zeros
    patch: dict[str, Any] = {
        "name": (res.get("name") or "").strip(),
        "set_name": (res.get("set_name") or "").strip(),
        "set_code": (res.get("set_code") or "").strip().lower(),
        "card_number": num,
        "rarity": (res.get("rarity") or "").strip(),
        "is_holo": bool(res.get("is_holo")),
        "id_confidence": round(float(res.get("confidence") or 0.0), 3),
    }
    if not (card.get("condition_guess") or "").strip():
        patch["condition_guess"] = "NM"  # inventory default: Near Mint unless noted
    return patch


@app.route("/api/cards/<int:card_id>/identify", methods=["POST"])
def api_identify_card(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    crop = ROOT / card["crop_path"]
    if not crop.exists():
        return jsonify({"error": f"crop not found: {card['crop_path']}"}), 404
    body = request.get_json(silent=True) or {}
    res = None if body.get("force_ai") else _match_crop(crop)
    if res is None:
        try:
            from lib.vision_identify import identify_crop
            res = identify_crop(str(crop))
        except Exception as exc:
            traceback.print_exc()
            return jsonify({"error": f"identification failed: {exc}"}), 502
        _remember_identification(crop, res)
    with db.connect(DB_PATH) as conn:
        updated = db.update_card(conn, card_id, _identification_patch(res, card))
    updated["identified_by"] = {"cache": "image cache", "match": "art match"}.get(
        res.get("_source"), "AI"
    )
    return jsonify(updated)


@app.route("/api/identify/run-all", methods=["POST"])
def api_identify_all():
    with _job_lock:
        if _job_state["running"]:
            return jsonify({"error": "another job is running"}), 409
        _job_state.update(running=True, progress=0, total=0, message="identifying…", terapeak=False)

    def worker():
        try:
            _identify_job()
        finally:
            _job_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scan/process", methods=["POST"])
def api_scan_process():
    """The scan flow in one click: identify every unidentified card, then
    price every identified card that has no price yet. Cards that are already
    identified or priced are left alone, so re-running costs nothing extra."""
    with _job_lock:
        if _job_state["running"]:
            return jsonify({"error": "another job is running"}), 409
        _job_state.update(running=True, progress=0, total=0, message="identifying…", terapeak=False)

    def worker():
        try:
            identified = _identify_job()
            id_message = _job_state["message"]
            _job_state.update(progress=0, total=0, message="pricing…")
            _pricing_job(False, only_unpriced=True)
            if identified is not None:
                _job_state["message"] = f"{id_message}; {_job_state['message']}"
        finally:
            _job_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


def _identify_job() -> Optional[int]:
    """Identify unidentified bench cards. Returns how many were identified,
    or None if the job failed. Never touches _job_state["running"]."""
    try:
        from lib.vision_identify import identify_crop, identify_page

        with db.connect(DB_PATH) as conn:
            cards = db.list_cards(conn, sort="oldest", unidentified_only=True)
            grids = {int(r["id"]): dict(r) for r in conn.execute("SELECT * FROM grids").fetchall()}
        _job_state["total"] = len(cards)
        if not cards:
            _job_state["message"] = "nothing to identify"
            return 0

        by_grid: dict[Any, list[dict]] = {}
        for c in cards:
            by_grid.setdefault(c.get("grid_id"), []).append(c)

        done = 0
        by_image = 0
        by_ai = 0
        last_error = ""   # shown to the user; a missing API key fails every card the same way

        def _apply(card: dict, res: dict) -> None:
            with db.connect(DB_PATH) as conn:
                db.update_card(conn, int(card["id"]), _identification_patch(res, card))

        def _tick() -> None:
            _job_state["progress"] = done
            _job_state["message"] = (
                f"Identifying {done}/{len(cards)} "
                f"({by_image} by image match, {by_ai} by AI)"
            )

        for grid_id, group in by_grid.items():
            # Pass 0 (free): learned cache + art-index hash match.
            unmatched: list[dict] = []
            for card in group:
                crop = ROOT / card["crop_path"]
                res = _match_crop(crop) if crop.exists() else None
                if res is not None:
                    _apply(card, res)
                    by_image += 1
                    done += 1
                    _tick()
                else:
                    unmatched.append(card)
            if not unmatched:
                continue

            # Pass 1 (AI): one page-level call when several crops on the
            # page still need it (~9x cheaper than per-crop); otherwise go
            # straight to per-crop calls for the stragglers.
            page_results: dict[tuple, dict] = {}
            grid = grids.get(grid_id) if grid_id is not None else None
            if grid and len(unmatched) >= 3:
                p = Path(grid["original_path"])
                page_path = p if p.is_absolute() else ROOT / p
                # Prefer the leveled page saved at split time — its
                # orientation matches the crops' row/col assignments.
                prepared = OUTPUT_DIR / "pages" / f"{page_path.stem}.jpg"
                if prepared.exists():
                    page_path = prepared
                if page_path.exists():
                    try:
                        page_results = {
                            (r.get("row"), r.get("col")): r
                            for r in identify_page(str(page_path))
                        }
                    except Exception as exc:
                        last_error = str(exc)
                        print(f"[identify] page grid={grid_id}: {exc}", file=sys.stderr)

            for card in unmatched:
                res = page_results.get((card.get("row"), card.get("col")))
                crop = ROOT / card["crop_path"]
                if res is None or float(res.get("confidence") or 0.0) < IDENTIFY_RETRY_CONF:
                    if crop.exists():
                        try:
                            crop_res = identify_crop(str(crop))
                            if res is None or float(crop_res.get("confidence") or 0.0) >= float(res.get("confidence") or 0.0):
                                res = crop_res
                        except Exception as exc:
                            last_error = str(exc)
                            print(f"[identify] crop id={card['id']}: {exc}", file=sys.stderr)
                if res and (res.get("name") or "").strip():
                    _apply(card, res)
                    by_ai += 1
                    if crop.exists():
                        _remember_identification(crop, res)
                done += 1
                _tick()

        _job_state["message"] = (
            f"identified {by_image + by_ai}/{len(cards)} "
            f"({by_image} free by image match, {by_ai} by AI)"
        )
        missed = len(cards) - by_image - by_ai
        if missed and last_error:
            _job_state["message"] += f", {missed} failed: {last_error[:160]}"
        return by_image + by_ai
    except Exception as exc:
        traceback.print_exc()
        _job_state["message"] = f"identify failed: {exc}"
        return None


# ----------------------------------------------------------------------
# Export + image upload
# ----------------------------------------------------------------------

def _cards_payload(cards: list[dict]) -> list[dict]:
    """Coerce DB rows into the dict shape the CSV generator + listers expect."""
    payload = []
    for c in cards:
        payload.append({
            "id": c["id"],
            "crop_path": c["crop_path"],
            "needs_review": bool(c["needs_review"]),
            "name": c["name"] or "",
            "set_name": c["set_name"] or "",
            "set_code": c["set_code"] or "",
            "card_number": c["card_number"] or "",
            "rarity": c["rarity"] or "",
            "is_holo": bool(c["is_holo"]),
            "condition_guess": c["condition_guess"] or "",
            "price": c["final_price"],
            "final_price": c["final_price"],
            "tcgplayer_product_id": c["tcgplayer_product_id"],
            "image_url": c["image_url"] or "",
            "confidence": c["pricing_confidence"],
        })
    return payload


@app.route("/api/export/csvs", methods=["POST"])
def api_export_csvs():
    gc = get_csv_gen()
    with db.connect(DB_PATH) as conn:
        cards = db.list_cards(conn, sort="newest")
    payload = _cards_payload(cards)

    CSVS_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, rows_fn in (
        ("tcgplayer_bulk.csv", gc.tcgplayer_rows),
        ("whatnot_seller_hub.csv", gc.whatnot_rows),
        ("ebay_bulk.csv", gc.ebay_rows),
    ):
        rows = rows_fn(payload)
        # Use pandas only when present; otherwise write naively.
        path = CSVS_DIR / filename
        _write_csv(path, rows)
        written.append({"file": str(path.relative_to(ROOT)), "rows": len(rows)})
    return jsonify({"written": written})


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@app.route("/api/export/lot", methods=["POST"])
def api_export_lot():
    """Bundle a set of cards into a single quick-sale listing per marketplace.

    Body:
      {
        "card_ids": [int, ...],   # required, >= 2
        "title":    str,          # optional, auto-generated otherwise
        "price":    float,        # optional, defaults to 70% of sum(final_price)
        "quantity": int,          # optional, defaults to 1
        "discount": float,        # optional 0..1, used if price not provided (default 0.30)
        "note":     str,          # optional extra line for the description
        "slug":     str,          # optional folder name under output/csvs/lots/
      }
    """
    gc = get_csv_gen()
    body = request.get_json(silent=True) or {}
    card_ids = body.get("card_ids") or []
    if not isinstance(card_ids, list) or len(card_ids) < 2:
        return jsonify({"error": "card_ids must be a list of 2 or more card IDs"}), 400

    with db.connect(DB_PATH) as conn:
        cards = [db.get_card(conn, int(cid)) for cid in card_ids]
    cards = [c for c in cards if c]
    if len(cards) < 2:
        return jsonify({"error": "fewer than 2 cards resolved from card_ids"}), 400

    sum_price = sum((c.get("final_price") or 0.0) for c in cards)
    discount = float(body.get("discount") if body.get("discount") is not None else 0.30)
    discount = max(0.0, min(0.9, discount))
    default_price = round(sum_price * (1.0 - discount), 2) if sum_price > 0 else 0.0
    price = float(body["price"]) if body.get("price") is not None else default_price

    slug_raw = str(body.get("slug") or f"lot-{int(time.time())}")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug_raw).strip("-") or f"lot-{int(time.time())}"

    bundle = {
        "title": body.get("title"),
        "price": price,
        "quantity": int(body.get("quantity") or 1),
        "note": body.get("note"),
        "sku": slug,
        "image_url": body.get("image_url"),
    }

    payload = []
    for c in cards:
        payload.append({
            "crop_path": c["crop_path"],
            "name": c.get("name") or "",
            "set_name": c.get("set_name") or "",
            "card_number": c.get("card_number") or "",
            "rarity": c.get("rarity") or "",
            "is_holo": bool(c.get("is_holo")),
            "condition_guess": c.get("condition_guess") or "",
            "image_url": c.get("image_url") or "",
            "final_price": c.get("final_price"),
        })

    out_dir = CSVS_DIR / "lots" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = (
        ("whatnot_lot.csv", gc.whatnot_lot_row(payload, bundle)),
        ("ebay_lot.csv", gc.ebay_lot_row(payload, bundle)),
        ("tcgplayer_lot.csv", gc.tcgplayer_lot_row(payload, bundle)),
    )
    written = []
    for filename, row in targets:
        path = out_dir / filename
        _write_csv(path, [row])
        written.append({"file": str(path.relative_to(ROOT)), "rows": 1})

    return jsonify({
        "written": written,
        "summary": {
            "card_count": len(cards),
            "sum_of_final_prices": round(sum_price, 2),
            "bundle_price": round(price, 2),
            "discount_applied": round(discount, 2) if body.get("price") is None else None,
            "slug": slug,
        },
        "note": "TCGPlayer's bulk inventory CSV has no native lot row — the tcgplayer_lot.csv row needs a manual TCGplayer Id (or skip TCGPlayer for this lot).",
    })


@app.route("/api/cards/<int:card_id>/upload-image", methods=["POST"])
def api_upload_card_image(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)

    try:
        from lib import cloudinary_client
        cloudinary_client.configure()
        crop = ROOT / card["crop_path"]
        url = cloudinary_client.upload(str(crop))
        with db.connect(DB_PATH) as conn:
            updated = db.update_card(conn, card_id, {"image_url": url})
        return jsonify(updated)
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


# ----------------------------------------------------------------------
# Publish to marketplaces
# ----------------------------------------------------------------------

@app.route("/api/publish/status")
def api_publish_status():
    """Readiness probe for each site + the current publish job state."""
    # eBay: official API — are we authorized?
    try:
        from lib.ebay_lister import preflight
        ebay = preflight()
    except Exception as exc:  # pragma: no cover - import/runtime guard
        ebay = {"ready": False, "reason": str(exc)}

    # TCGPlayer / Whatnot: headless portal upload — is a session saved?
    def _portal_ready(env_key: str, default: str) -> dict:
        path = ROOT / os.getenv(env_key, default)
        return {
            "ready": path.exists(),
            "reason": "" if path.exists() else "no saved session — run python -m webapp.setup_portal",
        }

    # What a publish would touch right now, so the UI can say it in plain
    # words before anything goes live.
    with db.connect(DB_PATH) as conn:
        cards = db.list_cards(conn, sort="newest")
    priced = [c for c in cards if c.get("name") and c.get("final_price")]
    ebay_eligible = [c for c in priced if c.get("image_url")]
    counts = {
        "priced": len(priced),
        "priced_flagged": sum(1 for c in priced if c.get("needs_review")),
        "priced_total": round(sum(c["final_price"] for c in priced), 2),
        "ebay_eligible": len(ebay_eligible),
        "ebay_flagged": sum(1 for c in ebay_eligible if c.get("needs_review")),
        "ebay_total": round(sum(c["final_price"] for c in ebay_eligible), 2),
        "ebay_missing_image": len(priced) - len(ebay_eligible),
    }

    return jsonify({
        "ebay": ebay,
        "tcgplayer": _portal_ready("TCGPLAYER_STATE_PATH", "output/cache/tcgplayer_state.json"),
        "whatnot": _portal_ready("WHATNOT_STATE_PATH", "output/cache/whatnot_state.json"),
        "job": _publish_state,
        "counts": counts,
    })


@app.route("/api/publish/job-status")
def api_publish_job_status():
    return jsonify(_publish_state)


@app.route("/api/publish/ebay", methods=["POST"])
def api_publish_ebay():
    """Publish every eligible card as a live eBay listing (background job).
    Body: {"skip_flagged": true} leaves out cards whose price needs review."""
    skip_flagged = bool((request.get_json(silent=True) or {}).get("skip_flagged"))
    with _publish_lock:
        if _publish_state["running"]:
            return jsonify({"error": "a publish job is already running"}), 409
        _publish_state.update(running=True, progress=0, total=0, message="starting…", site="ebay")

    def worker():
        try:
            from lib.ebay_lister import EbayLister, EbayListingError
            from lib.ebay_oauth import EbayUserNotAuthorized
            try:
                lister = EbayLister()
            except Exception as exc:
                _publish_state["message"] = f"cannot start: {exc}"
                return

            with db.connect(DB_PATH) as conn:
                cards = _cards_payload(db.list_cards(conn, sort="newest"))
            eligible = [c for c in cards if c.get("final_price") and c.get("image_url")]
            if skip_flagged:
                eligible = [c for c in eligible if not c.get("needs_review")]
            _publish_state["total"] = len(eligible)

            done = 0
            for card in eligible:
                title = card.get("name") or Path(card.get("crop_path", "")).stem
                try:
                    result = lister.publish_card(card)
                    patch = {
                        "ebay_listing_id": result.get("listing_id"),
                        "ebay_offer_id": result.get("offer_id"),
                        "ebay_listing_url": result.get("url"),
                        "ebay_listing_status": "listed",
                        "listed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                    with db.connect(DB_PATH) as conn:
                        db.update_card(conn, int(card["id"]), patch)
                except EbayUserNotAuthorized as exc:
                    _publish_state["message"] = str(exc)
                    return
                except EbayListingError as exc:
                    print(f"[publish ebay] FAILED {title}: {exc}", file=sys.stderr)
                    with db.connect(DB_PATH) as conn:
                        db.update_card(conn, int(card["id"]), {"ebay_listing_status": f"error: {exc}"[:200]})
                except Exception as exc:  # pragma: no cover
                    print(f"[publish ebay] error {title}: {exc}", file=sys.stderr)
                finally:
                    done += 1
                    _publish_state["progress"] = done
                    _publish_state["message"] = f"listed {done}/{len(eligible)}"
            _publish_state["message"] = f"done — {done} processed"
        finally:
            _publish_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/publish/portal", methods=["POST"])
def api_publish_portal():
    """Generate the site's CSV from the DB and upload it via headless browser."""
    body = request.get_json(silent=True) or {}
    site = body.get("site") or request.args.get("site")
    if site not in ("tcgplayer", "whatnot"):
        return jsonify({"error": "site must be 'tcgplayer' or 'whatnot'"}), 400

    with _publish_lock:
        if _publish_state["running"]:
            return jsonify({"error": "a publish job is already running"}), 409
        _publish_state.update(running=True, progress=0, total=1, message="preparing CSV…", site=site)

    def worker():
        try:
            gc = get_csv_gen()
            with db.connect(DB_PATH) as conn:
                payload = _cards_payload(db.list_cards(conn, sort="newest"))
            CSVS_DIR.mkdir(parents=True, exist_ok=True)
            if site == "tcgplayer":
                filename, rows_fn = "tcgplayer_bulk.csv", gc.tcgplayer_rows
                from lib.tcgplayer_lister import TCGPlayerLister as Lister
            else:
                filename, rows_fn = "whatnot_seller_hub.csv", gc.whatnot_rows
                from lib.whatnot_lister import WhatnotLister as Lister
            path = CSVS_DIR / filename
            _write_csv(path, rows_fn(payload))

            _publish_state["message"] = f"uploading {filename} to {site}…"
            with Lister() as lister:
                result = lister.upload_csv(str(path))
            _publish_state["progress"] = 1
            _publish_state["message"] = (
                f"upload {'ok' if result.get('ok') else 'uncertain'}: {result.get('detail')}"
            )
        except Exception as exc:
            traceback.print_exc()
            _publish_state["message"] = f"failed: {exc}"
        finally:
            _publish_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


# ----------------------------------------------------------------------
# Static crops + CSV downloads
# ----------------------------------------------------------------------

@app.route("/crops/<path:filename>")
@app.route("/output/crops/<path:filename>")
def serve_crop(filename: str):
    return send_from_directory(CROPS_DIR, filename)


@app.route("/csvs/<path:filename>")
@app.route("/output/csvs/<path:filename>")
def download_csv(filename: str):
    if ".." in filename.split("/"):
        abort(404)
    path = CSVS_DIR / filename
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    name = Path(name).name
    # Allow letters, numbers, dot, dash, underscore.
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    return safe or "grid.jpg"


# ----------------------------------------------------------------------
# Multi-user portfolio layer (append-only zone — see COORDINATION.md).
# Blueprints live in webapp/{auth,catalog,portfolio}.py; the scheduler gets
# _price_card injected so pricing logic stays defined once, above.
# ----------------------------------------------------------------------

from webapp import scheduler as price_scheduler  # noqa: E402
from webapp.auth import auth_bp, ensure_secret_key  # noqa: E402
from webapp.catalog import catalog_bp  # noqa: E402
from webapp.marketplace import market_bp  # noqa: E402
from webapp.models import init_models  # noqa: E402
from webapp.portfolio import portfolio_bp  # noqa: E402

ensure_secret_key(app)
init_models()
app.register_blueprint(auth_bp)
app.register_blueprint(catalog_bp)
app.register_blueprint(portfolio_bp)
app.register_blueprint(market_bp)
price_scheduler.start(price_fn=_price_card)

# [B] Advanced settings (per-user eBay API keys) — see webapp/settings.py.
from webapp.settings import ensure_settings_tables, settings_bp  # noqa: E402

app.register_blueprint(settings_bp)
ensure_settings_tables()

# Library ledger (sold / purchased history) and deck building.
from webapp.decks import decks_bp  # noqa: E402
from webapp.library import backfill_order_events, library_bp  # noqa: E402

app.register_blueprint(library_bp)
app.register_blueprint(decks_bp)
backfill_order_events()


@app.route("/api/session")
def api_session():
    """Lets the open bench page know whether library actions need a login."""
    from flask import session as flask_session
    return jsonify({"logged_in": bool(flask_session.get("uid"))})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
