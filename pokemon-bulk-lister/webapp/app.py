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
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import cv2
from dotenv import load_dotenv
from flask import (
    g,
    Flask,
    abort,
    jsonify,
    redirect,
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
from lib.printing import of as card_printing
from lib.pricing import aggregate  # noqa: E402
from lib.tcgplayer_client import TCGPlayerClient  # noqa: E402
from webapp import db  # noqa: E402
from webapp import runtime  # noqa: E402
from webapp.runtime import DATA_ROOT, SERVERLESS, data_path, rel_path  # noqa: E402


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

# Data lives under DATA_ROOT: this directory locally, /tmp on a serverless
# host (where the lasting copies are in Blob and the database).
INPUT_DIR = DATA_ROOT / os.getenv("INPUT_DIR", "input/grids")
OUTPUT_DIR = DATA_ROOT / os.getenv("OUTPUT_DIR", "output")
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

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB



# The bench has no login, so keep other websites from driving it through the
# operator's browser (see connections.same_origin_only).
from webapp.connections import operator_only_when_public, same_origin_only  # noqa: E402

app.before_request(same_origin_only)


@app.before_request
def _keep_host_identity_token():
    """On Vercel each request carries the deployment's identity token, which
    is what signs this app in to the Claude API when no key is stored."""
    token = request.headers.get("x-vercel-oidc-token")
    if token:
        from lib import anthropic_auth
        anthropic_auth.remember_identity_token(token)

# Under a real domain the bench is on the internet: operator login required.
app.before_request(operator_only_when_public)

# Behind a reverse proxy that terminates HTTPS (a hosting platform, nginx,
# Caddy). Without this the app thinks it is served over plain http, so the
# links it mails and the URLs it gives Stripe would be wrong.
if os.getenv("TRUST_PROXY") == "1" or SERVERLESS:
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax",
                      PREFERRED_URL_SCHEME="https")


@app.route("/healthz")
def healthz():
    """For a hosting platform's health check. Says nothing about the data."""
    return jsonify({"ok": True})


# Lazy singletons
_split_grids = None
_csv_gen = None

# Background job state (single in-process slot — fine for a one-off personal app).
_job_lock = threading.Lock()
_job_state = runtime.JobState("main", running=False, progress=0, total=0, message="")

# Separate slot for publish jobs so a publish run doesn't clobber pricing state.
_publish_lock = threading.Lock()
_publish_state = runtime.JobState("publish", running=False, progress=0, total=0, message="", site="")


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


_thread_clients = threading.local()


def get_tcg() -> TCGPlayerClient:
    # One client per thread. A shared Session is not safe across the price workers.
    client = getattr(_thread_clients, "tcg", None)
    if client is None:
        client = TCGPlayerClient()
        _thread_clients.tcg = client
    return client


def get_ebay() -> EbayClient:
    client = getattr(_thread_clients, "ebay", None)
    if client is None:
        client = EbayClient()
        _thread_clients.ebay = client
    return client


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

_adopted: set[int] = set()


def _is_operator() -> bool:
    return bool(getattr(g, "bench_operator", True))


def _as_owner(fn):
    """fn, to be run on another thread, working on this request's bench.
    A new thread does not inherit the request's owner."""
    uid = db.owner()

    def run(*args, **kwargs):
        db.set_owner(uid)
        return fn(*args, **kwargs)

    return run


def _jk(base: str) -> str:
    """The stored job slot for this request's bench."""
    uid = db.owner()
    return base if uid is None else f"{base}:u{uid}"


def _mine_job(state) -> dict:
    """The in-process job state as this request's account should see it.
    Another account's job is still running, but its card counts stay hidden."""
    if state.get("owner") == db.owner():
        return dict(state)
    if state.get("running"):
        idle = dict(_IDLE)
        idle["running"] = True
        idle["message"] = "Someone else is using this. Try again in a moment."
        return idle
    return dict(_IDLE)


def _csv_dir() -> Path:
    uid = db.owner()
    return CSVS_DIR if uid is None else CSVS_DIR / f"u{uid}"


@app.route("/")
def index():
    uid = db.owner()
    if uid is not None and _is_operator() and uid not in _adopted:
        # Cards scanned before accounts had their own bench go to the operator.
        with db.connect(DB_PATH) as conn:
            db.adopt_unowned(conn, uid)
        _adopted.add(uid)
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
    sold = _sold_stems(strict=False)   # display only
    for row in rows:
        row["sold"] = Path(row["crop_path"]).stem in sold or row.get("ebay_listing_status") == "sold"
    runtime.add_photo_urls(rows)
    from webapp.auth import current_user_id
    from webapp.catalog import in_library_crops
    added = in_library_crops(current_user_id(), [row["crop_path"] for row in rows])
    for row in rows:
        row["in_library"] = row["crop_path"] in added
    return jsonify({"cards": rows, "stats": stats})


@app.route("/api/cards/<int:card_id>", methods=["GET"])
def api_get_card(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    return jsonify(card)


def _listing_fields(body: dict) -> dict:
    """The owner's own listing price and notes from a request body, checked.
    Only the keys present are returned. A blank price means "use the
    estimate". Raises ValueError with words for the person."""
    out: dict[str, Any] = {}
    if "list_price" in body:
        raw = body.get("list_price")
        if raw in (None, ""):
            out["list_price"] = None
        else:
            try:
                price = round(float(raw), 2)
            except (TypeError, ValueError):
                raise ValueError("the list price must be a number")
            if not 0 < price <= 100000:
                raise ValueError("the list price must be more than 0")
            out["list_price"] = price
    if "listing_notes" in body:
        out["listing_notes"] = (str(body.get("listing_notes") or "").strip()[:2000]) or None
    return out


def _listing_preview(card: dict) -> dict:
    """What a card would be listed as: built from its facts, no model."""
    from lib import ebay_lister

    shaped = _cards_payload([card])[0]
    return {"title": ebay_lister.card_title(shaped), "description": ebay_lister.card_description(shaped),
            "price": shaped["final_price"], "price_is_yours": card.get("list_price") is not None,
            "estimate": card.get("final_price"),
            "on_ebay": bool(card.get("ebay_listing_id")) and card.get("ebay_listing_status") == "listed",
            "written_by": card.get("description_source"),
            "ai_status": _descriptions.status_for(Path(card["crop_path"]).stem)}


@app.route("/api/cards/<int:card_id>/listing")
def api_card_listing(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    return jsonify(_listing_preview(card))


@app.route("/api/cards/listing", methods=["POST"])
def api_set_listing():
    """Set the list price and/or notes on many cards at once.
    Body: {"ids": [..], "list_price": 1.00 | null, "listing_notes": "..."}.
    Only the keys sent are changed; a null price goes back to the estimate."""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids (list) required"}), 400
    try:
        fields = _listing_fields(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not fields:
        return jsonify({"error": "list_price or listing_notes required"}), 400
    changed, live = 0, []
    with db.connect(DB_PATH) as conn:
        for raw in ids:
            card = db.update_card(conn, int(raw), fields)
            if card:
                changed += 1
                if card.get("ebay_listing_id") and card.get("ebay_listing_status") == "listed":
                    live.append(card["id"])
    return jsonify({"changed": changed, "on_ebay": live})


@app.route("/api/publish/ebay/revise", methods=["POST"])
def api_revise_ebay():
    """Bring live eBay listings up to date with their cards as they are now
    (title, description, condition, photo, price). Body: {"ids": [..]}, at
    most 25 a call. Cards that are not live on eBay are passed over."""
    from lib.ebay_lister import EbayLister, EbayListingError
    from lib.ebay_oauth import EbayUserNotAuthorized
    from webapp import connections

    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids (list) required"}), 400
    with db.connect(DB_PATH) as conn:
        rows = [c for c in (db.get_card(conn, int(i)) for i in ids[:25]) if c]
    live = [c for c in rows if c.get("ebay_listing_id") and c.get("ebay_listing_status") == "listed"]
    if not live:
        return jsonify({"updated": 0, "failed": [], "remaining": max(0, len(ids) - 25)})
    try:
        lister = EbayLister(auth=connections._ebay_auth())
    except Exception as exc:
        return jsonify({"error": f"eBay is not connected: {exc}"}), 409
    updated, failed = 0, []
    for row in live:
        card = {**_cards_payload([row])[0], "ebay_offer_id": row.get("ebay_offer_id")}
        try:
            lister.revise_card(card)
            updated += 1
        except EbayUserNotAuthorized as exc:
            return jsonify({"error": f"eBay is not connected: {exc}", "updated": updated}), 409
        except EbayListingError as exc:
            failed.append({"id": row["id"], "name": row.get("name"), "error": str(exc)[:300]})
    return jsonify({"updated": updated, "failed": failed, "remaining": max(0, len(ids) - 25)})


@app.route("/api/cards/<int:card_id>", methods=["PATCH"])
def api_patch_card(card_id: int):
    patch = request.get_json(silent=True) or {}
    try:
        patch.update(_listing_fields(patch))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with db.connect(DB_PATH) as conn:
        before = db.get_card(conn, card_id)
        card = db.update_card(conn, card_id, patch)
    if not card:
        abort(404)
    # The change made its AI description wrong (db.update_card dropped it):
    # the owner asked for one, so a new one is written.
    if before and before.get("description_source") and not card.get("ai_description"):
        _descriptions.enqueue(_cards_payload([card]))
    return jsonify(card)


@app.route("/api/cards/delete", methods=["POST"])
def api_delete_cards():
    """Remove scanned cards and their photos from this bench.
    Body: {"ids": [..]} or {"all": true}. A card already added to a library
    stays there; only its photo is kept for it."""
    body = request.get_json(silent=True) or {}
    everything = body.get("all") is True
    ids = body.get("ids")
    # Nothing is deleted unless the request says exactly what.
    if not everything and (not isinstance(ids, list) or not ids):
        return jsonify({"error": "ids (list) or all:true required"}), 400
    if everything:
        ids = None
    with db.connect(DB_PATH) as conn:
        gone = db.delete_cards(conn, ids)

    # Photos a library entry still shows are kept.
    from webapp.models import InventoryItem, get_session
    s = get_session()
    try:
        in_library = {row[0] for row in s.query(InventoryItem.source_crop_path)
                      .filter(InventoryItem.source_crop_path.isnot(None)).all()}
    finally:
        s.close()
    for card in gone["cards"]:
        if card.get("crop_path") and card["crop_path"] not in in_library:
            runtime.delete_file(data_path(card["crop_path"]))
    for grid in gone["grids"]:
        if grid.get("original_path"):
            runtime.delete_file(data_path(grid["original_path"]))
        stem = Path(grid.get("original_path") or grid.get("filename") or "").stem
        if stem:
            runtime.delete_file(OUTPUT_DIR / "pages" / f"{stem}.jpg")
    return jsonify({"deleted": len(gone["cards"]), "photos": len(gone["grids"])})


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
            found = db.card_id_by_crop(conn, crop_path)
            if found is None:
                continue
            patch = {k: v for k, v in entry.items() if k != "crop_path"}
            db.update_card(conn, found, patch)
            applied += 1
    return jsonify({"applied": applied})


# ----------------------------------------------------------------------
# Upload + split
# ----------------------------------------------------------------------

@app.route("/api/grids/upload", methods=["POST"])
def api_upload_grid():
    """Form fields: file (repeatable). Each photo may hold any number of
    cards in any layout; they are found, cropped and squared up
    (`_record_photo`). mode = "grid" or "single" forces the old fixed
    reading (a 3x3 binder page, or one card filling the frame)."""
    files = request.files.getlist("file") or []
    if not files:
        abort(400, "no file uploaded")
    mode = request.form.get("mode")

    sg = get_split_grids()
    total_crops = 0
    grids_added = []
    skipped = []
    errors = []

    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            skipped.append(file.filename or "")
            continue
        safe_name = _unique_upload_name(_safe_filename(file.filename or "grid.jpg"))
        dest = INPUT_DIR / safe_name
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        file.save(dest)
        try:
            dest = _as_readable_photo(dest)
            n = _record_photo(sg, dest, mode)
        except (ValueError, cv2.error) as exc:
            n = 0
            errors.append({"filename": file.filename, "error": str(exc)})
        if not n:
            dest.unlink(missing_ok=True)
            skipped.append(file.filename or "")
            if not any(e["filename"] == file.filename for e in errors):
                errors.append({"filename": file.filename, "error": "This photo could not be read. Try a JPEG or PNG."})
            continue
        total_crops += n
        grids_added.append({"filename": dest.name, "crops": n})

    return jsonify({"grids": grids_added, "total_crops": total_crops, "skipped": skipped, "errors": errors})


MAX_DRIVE_PHOTO_BYTES = 40 * 1024 * 1024


@app.route("/api/drive/import/photo", methods=["POST"])
def api_drive_import_photo():
    """Body: {file_id, mode?}. Fetches one photo from the
    signed-in account's Google Drive and files it exactly as an upload would
    be. The page sends one photo per request. The server reads Drive itself,
    so a host's cap on upload size does not apply."""
    from webapp import drive as drive_mod
    from webapp.auth import current_user_id

    uid = current_user_id()
    if not uid:
        return jsonify({"error": "log in to use Google Drive"}), 401
    body = request.get_json(silent=True) or {}
    try:
        content, info = drive_mod.drive_for(uid).download(str(body.get("file_id") or ""), MAX_DRIVE_PHOTO_BYTES)
    except Exception as exc:
        return drive_mod.error_response(exc)
    name = _safe_filename(info.get("name") or "drive-photo.jpg")
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return jsonify({"grids": [], "total_crops": 0, "skipped": [info.get("name") or name]})
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = INPUT_DIR / _unique_upload_name(name)
    dest.write_bytes(content)
    dest = _as_readable_photo(dest)
    sg = get_split_grids()
    n = _record_photo(sg, dest, body.get("mode"))
    return jsonify({"grids": [{"filename": dest.name, "crops": n}], "total_crops": n, "skipped": []})


def _as_readable_photo(path: Path) -> Path:
    """iPhones save HEIC, which OpenCV cannot read. Such a photo is rewritten
    as a JPEG beside it (turned the way the phone held it) and the original
    removed. Any other photo is returned as it is. A HEIC that cannot be
    decoded is left alone and is then reported as having no cards."""
    if path.suffix.lower() not in (".heic", ".heif"):
        return path
    try:
        import pillow_heif
        from PIL import Image, ImageOps

        pillow_heif.register_heif_opener()
        out = path.with_suffix(".jpg")
        with Image.open(path) as img:
            ImageOps.exif_transpose(img).convert("RGB").save(out, "JPEG", quality=92)
        path.unlink()
        return out
    except Exception as exc:
        print(f"[upload] could not read {path.name}: {exc}", file=sys.stderr)
        return path


def _unique_upload_name(name: str) -> str:
    """Phone cameras reuse names like image.jpg; a second upload with the same
    name must not overwrite the first photo or collide with its crops."""
    path = Path(name)
    # Crops and grid rows are keyed by the stem, so IMG_1.jpg and IMG_1.png
    # collide too. Compare stems, case-insensitively.
    taken = {p.stem.lower() for p in INPUT_DIR.iterdir()} | {
        p.stem.lower() for p in (OUTPUT_DIR / "pages").glob("*")
    }
    # The database knows every page ever uploaded, including ones whose files
    # are not on this instance's disk.
    with db.connect(DB_PATH) as conn:
        taken |= {str(r["filename"]).lower() for r in conn.execute("SELECT filename FROM grids").fetchall()}
    stem, n = path.stem, 1
    while stem.lower() in taken:
        n += 1
        stem = f"{path.stem}-{n}"
    return f"{stem}{path.suffix}"


def _record_photo(sg, photo_path: Path, mode: Optional[str] = None) -> int:
    """File the cards in one photo, however they are laid out. Returns how
    many were filed.

    The cards are found by their outlines (lib/card_finder: one card, a
    pair, a block, a loose scatter, a binder page on a table). When that
    finds nothing, the photo is read the old way: as a 3x3 binder page if it
    looks like one filling the frame, otherwise as one card."""
    if mode == "single":
        return _record_single(sg, photo_path)
    if mode == "grid":
        return _split_and_record(sg, photo_path)
    from lib import card_finder

    img = cv2.imread(str(photo_path))
    if img is None:
        return 0
    found = card_finder.find_cards(img)
    if not found:
        if len(sg.detect_cards(sg.auto_orient(img))) >= sg.DETECTOR_MIN_HITS:
            return _split_and_record(sg, photo_path)
        return _record_single(sg, photo_path)
    crops = [card_finder.crop(img, f["quad"]) for f in found]
    print(f"[split] {photo_path.name}: {len(crops)} card(s) found by outline", file=sys.stderr)

    stem = photo_path.stem
    if len(crops) >= 3:
        # What page-level identification reads: the squared-up cards three
        # across, so card i is at row i // 3, column i % 3 whatever the
        # layout on the table was.
        pages_dir = OUTPUT_DIR / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        sheet = pages_dir / f"{stem}.jpg"
        cv2.imwrite(str(sheet), card_finder.contact_sheet(crops), [cv2.IMWRITE_JPEG_QUALITY, 88])
        runtime.save_file(sheet)
    with db.connect(DB_PATH) as conn:
        grid_id = db.get_or_create_grid(conn, stem, rel_path(photo_path))
        for i, card in enumerate(crops):
            r, c = divmod(i, 3)
            out_path = CROPS_DIR / f"{stem}_r{r}c{c}.jpg"
            cv2.imwrite(str(out_path), card, [cv2.IMWRITE_JPEG_QUALITY, 92])
            runtime.save_file(out_path)
            db.insert_card_stub(conn, grid_id, rel_path(out_path), r, c)
        db.update_grid_count(conn, grid_id)
    return len(crops)


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
    runtime.save_file(out_path)
    with db.connect(DB_PATH) as conn:
        grid_id = db.get_or_create_grid(conn, stem, rel_path(photo_path))
        db.insert_card_stub(conn, grid_id, rel_path(out_path), 0, 0)
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
    runtime.save_file(pages_dir / f"{grid_path.stem}.jpg")
    cells, method = sg.split_page(img)
    print(f"[split] {grid_path.name}: {sum(1 for c in cells if c is not None)} crops via {method}", file=sys.stderr)

    stem = grid_path.stem
    written = 0
    with db.connect(DB_PATH) as conn:
        grid_id = db.get_or_create_grid(conn, stem, rel_path(grid_path))
        for idx, cell in enumerate(cells):
            if cell is None:
                continue
            r, c = divmod(idx, 3)
            out_path = CROPS_DIR / f"{stem}_r{r}c{c}.jpg"
            cv2.imwrite(str(out_path), cell, [cv2.IMWRITE_JPEG_QUALITY, 92])
            runtime.save_file(out_path)
            rel = rel_path(out_path)
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
    use_terapeak = (request.args.get("terapeak") == "1" or body.get("terapeak")) and _is_operator()
    only_unpriced = request.args.get("only_unpriced") == "1" or bool(body.get("only_unpriced"))
    if SERVERLESS:
        return _start_sliced_job("pricing", only_unpriced=only_unpriced)
    with _job_lock:
        if _job_state["running"]:
            return jsonify({"error": "another job is running"}), 409
        _job_state.update(
            running=True, progress=0, total=0,
            message="starting…", terapeak=bool(use_terapeak), owner=db.owner(),
        )

    def worker():
        try:
            _pricing_job(use_terapeak, only_unpriced)
        finally:
            _job_state["running"] = False

    threading.Thread(target=_as_owner(worker), daemon=True).start()
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
            _job_state["message"] = "Nothing new to price"
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
                    _job_state["message"] = f"Pricing {p1} of {len(targets)} · eBay year {p2} of {len(targets)}"
                else:
                    _job_state["progress"] = p1
                    _job_state["message"] = f"Pricing {p1} of {len(targets)}"

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
                    card_printing(card) != "normal" or (TERAPEAK_MIN_VALUE > 0
                    and fp is not None
                    and fp < TERAPEAK_MIN_VALUE
                    and not patch.get("needs_review"))
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
        pass1_futures = [pass1_executor.submit(_as_owner(price_one_pass1), c) for c in targets]

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

                terapeak_thread = threading.Thread(target=_as_owner(drain_terapeak), daemon=True)
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

        _job_state["message"] = f"Priced {len(targets)} cards"
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
    if SERVERLESS:
        return jsonify(_pump(_jk(MAIN_JOB), _main_slice))
    return jsonify(_mine_job(_job_state))


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
    printing = card_printing(card)
    is_holo = printing == "holofoil"
    # A Japanese card has no price source of its own here. The English
    # printing's price is shown as a guide and always flagged for a look.
    japanese = (set_name or "").endswith(_card_catalog.JAPANESE)
    if japanese:
        twin = (_card_catalog.japanese_set_named(set_name) or {}).get("twin")
        set_name, set_code, card_number = None, twin, None      # the English set numbers its cards differently

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
    if japanese:
        # Only the English set that reprints this one. A match by name alone
        # is some other printing, and its price says nothing about this card.
        attempts = [({"set_code": set_code, "card_number": None}, "exact")] if set_code else []
    seen: set = set()
    for kwargs, _planned in attempts:
        key = (kwargs.get("set_code"), kwargs.get("card_number"))
        if key in seen:
            continue
        seen.add(key)
        sent_set, sent_num = kwargs.get("set_code"), kwargs.get("card_number")
        if sent_set and sent_num:
            level = "exact"
        elif sent_set:
            level = "no_number"
        elif sent_num:
            level = "no_set"
        else:
            level = "name_only"
        try:
            prices = tcg.lookup_prices(
                name=name,
                set_name=set_name,
                set_code=kwargs.get("set_code"),
                card_number=kwargs.get("card_number"),
                is_holo=is_holo,
                **({"printing": printing} if card.get("printing") else {}),
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
    if cm_eur is None and CARDMARKET_FALLBACK and name and not japanese and printing == "normal":
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
                f"Matched on the name only ({matched_set_id}). Check the set before listing."
            )
        elif match_level == "no_number":
            match_warning = (
                f"The card number was not used ({matched_set_id}). "
                "This may be a different printing."
            )
        if japanese:
            match_warning = "Japanese card. This price is the English printing. Check it before listing."
        if tcg_price and cm_usd and tcg_price > 0 and cm_usd > 0:
            ratio = max(tcg_price, cm_usd) / min(tcg_price, cm_usd)
            if ratio > 3.0:
                match_warning = (
                    (match_warning + "; " if match_warning else "")
                    + f"TCGplayer and Cardmarket disagree ({ratio:.1f}×) on {matched_set_id}. Check this printing."
                )

    ebay_stats: dict[str, Any] = {"median": None, "max": None, "count": 0}
    try:
        query = ebay.build_query(name=name, set_name=set_name, card_number=card_number)
        if printing == "normal":
            ebay_stats = ebay.sold_stats(query=query, days=30)
    except EbayAuthError as exc:
        print(f"[ebay auth] {exc}", file=sys.stderr)

    terapeak_stats: dict[str, Any] = {"median": None, "count": 0}
    if terapeak is not None and printing == "normal":
        try:
            tp_query = " ".join(p for p in [name, set_name, card_number, "pokemon"] if p)
            terapeak_stats = terapeak.search(tp_query, days=365)
        except Exception as exc:
            print(f"[terapeak] {name}: {exc}", file=sys.stderr)

    if printing == "unknown":
        tcg_price = cm_eur = cm_usd = None
    result = aggregate(
        tcgplayer_market=tcg_price,
        ebay_median_30d=ebay_stats.get("median"),
        ebay_max_30d=ebay_stats.get("max"),
        cardmarket_trend_usd=cm_usd,
        terapeak_median_usd=terapeak_stats.get("median"),
    )

    if printing == "unknown":
        match_warning = "Finish not checked. Choose non-holo, holo or reverse holo before pricing."

    if card.get("id_confidence") is not None and float(card["id_confidence"] or 0) <= 0.6:
        match_warning = ((match_warning + "; ") if match_warning else "") + "identification needs review; compare the card to its photo"

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
    if japanese and not match_warning and result.price is not None:
        match_warning = "Japanese card. This price is the English printing. Check it before listing."
        notes_parts.append(f"⚠ {match_warning}")
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
        "printing_unavailable": printing == "unknown" or bool(
            tcg_card is not None and printing != "normal" and tcg_price is None and cm_eur is None),
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
# The art index is a set of files built on this machine; a serverless
# instance has none, so matching is off there unless asked for.
ART_MATCH_ENABLED = os.getenv("ART_MATCH", "0" if SERVERLESS else "1") == "1"
ART_INDEX_DIR = OUTPUT_DIR / "cache" / "art_index" / "catalog"
LEARNED_CACHE_PATH = OUTPUT_DIR / "cache" / "art_index" / "learned.jsonl"
ART_MATCH_CONFIDENCE = 0.9   # reported id_confidence for a catalog art match

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
        "printing": m.get("printing") or ("unknown" if source == "match" else card_printing(m)),
        "confidence": float(m.get("confidence") or ART_MATCH_CONFIDENCE),
        "_source": source,
    }


def _remember_identification(crop_path: Path, res: dict, patch: dict) -> None:
    """Feed a confident AI identification into the learned cache so duplicate
    cards and re-uploads never pay for AI again."""
    if not ART_MATCH_ENABLED or res.get("_source"):
        return
    if patch.get("_catalog_status") != "verified" or (patch.get("set_name") or "").endswith("(Japanese)"):
        return
    if not (res.get("name") or "").strip():
        return
    try:
        from lib.art_match import index_descriptor
        desc = index_descriptor(crop_path)
        if desc is None:
            return
        _, learned = _art_matchers()
        meta = {
            k: patch.get(k)
            for k in ("name", "set_name", "set_code", "card_number",
                      "rarity", "is_holo", "printing")
        }
        meta["confidence"] = patch["id_confidence"]
        learned.add(desc, meta)
    except Exception:
        traceback.print_exc()  # cache failures must never break identification


# What an identification is worth once it has been checked against the real
# card catalog. The reader's own confidence says how legible the card was, not
# whether such a card exists, so the check overrides it.
ID_VERIFIED = 0.97        # this name and number exist in a set that fits what is printed
ID_PROBABLE = 0.6         # the name is in the right set under another number
ID_UNVERIFIED_CAP = 0.3   # no such card: a person should look


def _checked_identity(fields: dict, total: Any = None, printed_code: Optional[str] = None) -> tuple[dict, str]:
    """(changes, status) for a card's name / set / number checked against the
    catalog, with no model call. `fields` holds name, set_name, set_code,
    card_number and id_confidence as read."""
    from lib import card_catalog

    name = (fields.get("name") or "").strip()
    if not name:
        return {}, "unnamed"
    # A card checked again later still has what was printed on it.
    read = _id_read(fields)
    if total in (None, ""):
        total = read.get("number_total")
    if not printed_code:
        printed_code = read.get("set_code_printed")
    fields = {**fields, "name_printed": fields.get("name_printed") or read.get("name_printed"),
              "language": fields.get("language") or read.get("language")}
    printed = (fields.get("name_printed") or "").strip()
    if not printed and (fields.get("set_name") or "").endswith(card_catalog.JAPANESE):
        return {}, "unknown"            # already filed as a Japanese card; nothing printed left to check
    if card_catalog.is_japanese(printed) or card_catalog.is_japanese(name) or fields.get("language") == "ja":
        found = card_catalog.resolve_japanese(printed or name, name_read=name, number=fields.get("card_number"),
                                              total=total, printed_code=printed_code,
                                              set_name_read=fields.get("set_name"))
        real = found["card"] or {}
        read = float(fields.get("id_confidence") or 0.0)
        changes = {k: real[k] for k in ("name", "set_name", "set_code", "card_number") if real.get(k) is not None}
        if real.get("rarity"):
            changes["rarity"] = real["rarity"]
        if found["status"] == "verified":
            changes["id_confidence"] = ID_VERIFIED
        elif found["status"] == "probable":
            changes["id_confidence"] = round(min(read, ID_PROBABLE), 3)
        else:
            # A Japanese trainer or item the name table does not have is not
            # a confident read, even if the model was sure.
            changes["id_confidence"] = round(min(read, ID_UNVERIFIED_CAP), 3)
        return changes, found["status"]
    try:
        found = card_catalog.resolve(name, fields.get("card_number"), total=total, printed_code=printed_code,
                                     set_code=fields.get("set_code"), set_name=fields.get("set_name"))
    except Exception as exc:
        print(f"[identify] catalog check failed for {name}: {exc}", file=sys.stderr)
        return {}, "unknown"
    status = found["status"]
    read = float(fields.get("id_confidence") or 0.0)
    if status in ("verified", "probable"):
        real = found["card"]
        changes = {"name": real["name"], "set_name": real["set_name"] or "", "set_code": real["set_code"],
                   "card_number": real["card_number"],
                   "id_confidence": ID_VERIFIED if status == "verified" else min(max(read, 0.4), ID_PROBABLE)}
        if real.get("rarity"):
            changes["rarity"] = real["rarity"]
        return changes, status
    if status == "unverified":
        return {"id_confidence": round(min(read, ID_UNVERIFIED_CAP), 3)}, status
    return {}, status           # catalog unreachable: leave the reading as it is


def _id_read(card: dict) -> dict:
    """What the reader saw printed on a card, kept with it (cards.id_read)."""
    try:
        read = json.loads(card.get("id_read") or "{}")
    except (TypeError, ValueError):
        read = {}
    return read if isinstance(read, dict) else {}


@app.route("/api/cards/<int:card_id>/choices")
def api_card_choices(card_id: int):
    """The real cards this one could be, for a person to pick from. No model call."""
    from lib import card_catalog

    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    read = _id_read(card)
    name = (card.get("name") or "").strip()
    printed = (read.get("name_printed") or "").strip()
    found: list[dict] = []
    if name:
        try:
            if card_catalog.is_japanese(printed) or (card.get("set_name") or "").endswith(card_catalog.JAPANESE):
                one = card_catalog.resolve_japanese(printed or name, name_read=name, number=card.get("card_number"),
                                                    total=read.get("number_total"),
                                                    printed_code=read.get("set_code_printed"),
                                                    set_name_read=card.get("set_name"))
                if one.get("card") and one["status"] in ("verified", "probable"):
                    found = [{**one["card"], "why": "named from the printed Japanese name"}]
            else:
                found = card_catalog.choices(name, card.get("card_number"), total=read.get("number_total"),
                                             printed_code=read.get("set_code_printed"),
                                             set_code=card.get("set_code"), set_name=card.get("set_name"))
        except Exception as exc:
            print(f"[identify] choices for {name}: {exc}", file=sys.stderr)
    current = {k: card.get(k) for k in ("name", "set_name", "set_code", "card_number", "rarity")}
    return jsonify({"choices": found, "current": current,
                    "read": {k: read.get(k) for k in ("name_printed", "number_total", "set_code_printed")}})


def _identification_patch(res: dict, card: dict) -> dict[str, Any]:
    num = (res.get("card_number") or "").strip()
    num = num.lstrip("0") or num  # pokemontcg.io numbers carry no leading zeros
    patch: dict[str, Any] = {
        "name": (res.get("name") or "").strip(),
        "set_name": (res.get("set_name") or "").strip(),
        "set_code": (res.get("set_code") or "").strip().lower(),
        "card_number": num,
        "rarity": (res.get("rarity") or "").strip(),
        "is_holo": card_printing(res) == "holofoil",
        "printing": card_printing(res),
        "id_confidence": round(float(res.get("confidence") or 0.0), 3),
    }
    checked = dict(patch, name_printed=res.get("name_printed"), language=(res.get("language") or "").strip().lower())
    patch["id_read"] = json.dumps({
        "name": patch["name"], "name_printed": (res.get("name_printed") or "").strip(),
        "language": checked["language"], "card_number": (res.get("card_number") or "").strip(),
        "number_total": str(res.get("number_total") or "").strip(),
        "set_code_printed": (res.get("set_code_printed") or "").strip(),
        "set_name": patch["set_name"], "set_code": patch["set_code"],
    }, ensure_ascii=False)
    if not (card.get("condition_guess") or "").strip():
        patch["condition_guess"] = "NM"  # inventory default: Near Mint unless noted
    # Does a card with this name and number exist? Asked of the catalog, not
    # of a model. The catalog's own spelling, set and rarity replace the
    # reading when it does.
    changes, status = _checked_identity(checked, total=res.get("number_total"), printed_code=res.get("set_code_printed"))
    patch.update(changes)
    patch["_catalog_status"] = status
    if status == "unknown":
        patch["id_confidence"] = min(patch["id_confidence"], ID_PROBABLE)
    patch["needs_review"] = patch["id_confidence"] <= ID_PROBABLE or patch["printing"] == "unknown" or bool(card.get("needs_review"))
    return patch


@app.route("/api/identify/verify", methods=["POST"])
def api_verify_identities():
    """Check every named card on this bench against the real card catalog.
    No model call. Body: {after: <card id>} to carry on where the last call
    stopped. Cards that check out take the catalog's spelling, set and rarity;
    ones that do not exist are marked down so they sort to the top for a look."""
    body = request.get_json(silent=True) or {}
    try:
        after = int(body.get("after") or 0)
    except (TypeError, ValueError):
        after = 0
    with db.connect(DB_PATH) as conn:
        cards = [c for c in db.list_cards(conn, sort="oldest") if (c.get("name") or "").strip() and int(c["id"]) > after]
    deadline = time.monotonic() + 20
    tally = {"verified": 0, "probable": 0, "unverified": 0, "unknown": 0}
    last = after
    done = 0
    for card in cards:
        if time.monotonic() > deadline:
            break
        changes, status = _checked_identity(card)
        tally[status] = tally.get(status, 0) + 1
        if changes.get("id_confidence", 1) <= ID_PROBABLE:
            changes["needs_review"] = True
        if changes:
            with db.connect(DB_PATH) as conn:
                db.update_card(conn, int(card["id"]), changes)
        last = int(card["id"])
        done += 1
    return jsonify({**tally, "checked": done, "remaining": len(cards) - done, "last_id": last})


@app.route("/api/cards/<int:card_id>/identify", methods=["POST"])
def api_identify_card(card_id: int):
    with db.connect(DB_PATH) as conn:
        card = db.get_card(conn, card_id)
    if not card:
        abort(404)
    crop = data_path(card["crop_path"])
    if not runtime.ensure_local(crop):
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
    patch = _identification_patch(res, card)
    _remember_identification(crop, res, patch)
    with db.connect(DB_PATH) as conn:
        updated = db.update_card(conn, card_id, patch)
    updated["identified_by"] = {"cache": "image cache", "match": "art match"}.get(
        res.get("_source"), "AI"
    )
    return jsonify(updated)


@app.route("/api/identify/run-all", methods=["POST"])
def api_identify_all():
    if SERVERLESS:
        return _start_sliced_job("identify")
    with _job_lock:
        if _job_state["running"]:
            return jsonify({"error": "another job is running"}), 409
        _job_state.update(running=True, progress=0, total=0, message="identifying…", terapeak=False,
                          owner=db.owner())

    def worker():
        try:
            _identify_job()
        finally:
            _job_state["running"] = False

    threading.Thread(target=_as_owner(worker), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/scan/process", methods=["POST"])
def api_scan_process():
    """The scan flow in one click: identify every unidentified card, then
    price every identified card that has no price yet. Cards that are already
    identified or priced are left alone, so re-running costs nothing extra."""
    if SERVERLESS:
        return _start_sliced_job("scan")
    with _job_lock:
        if _job_state["running"]:
            return jsonify({"error": "another job is running"}), 409
        _job_state.update(running=True, progress=0, total=0, message="identifying…", terapeak=False,
                          owner=db.owner())

    def worker():
        try:
            _identify_job()
            # Keep the identify outcome (including a failure) in the final
            # message; pricing would otherwise overwrite it.
            id_message = _job_state["message"]
            _job_state.update(progress=0, total=0, message="pricing…")
            _pricing_job(False, only_unpriced=True)
            _job_state["message"] = f"{id_message}; {_job_state['message']}"
        finally:
            _job_state["running"] = False

    threading.Thread(target=_as_owner(worker), daemon=True).start()
    return jsonify({"started": True})


def _identify_group(group: list[dict], grid: Optional[dict], tally: dict, tick=None) -> None:
    """Identify the unidentified cards of one page. `tally` counts
    done / by_image / by_ai and keeps the last error; `tick` is called after
    each card so progress can be shown."""
    from lib.vision_identify import identify_crop, identify_page

    def _apply(card: dict, res: dict, patch=None) -> dict:
        patch = patch if patch is not None else _identification_patch(res, card)
        with db.connect(DB_PATH) as conn:
            db.update_card(conn, int(card["id"]), patch)
        return patch

    def _done() -> None:
        tally["done"] += 1
        if tick:
            tick()

    # Pass 0 (free): learned cache + art-index hash match.
    unmatched: list[dict] = []
    for card in group:
        crop = data_path(card["crop_path"])
        res = _match_crop(crop) if ART_MATCH_ENABLED and runtime.ensure_local(crop) else None
        patch = _identification_patch(res, card) if res is not None else None
        if patch is not None and patch.get("_catalog_status") not in ("unverified", "probable"):
            _apply(card, res, patch)
            tally["by_image"] += 1
            _done()
        else:
            unmatched.append(card)
    if not unmatched:
        return

    # Pass 1 (AI): one page-level call when several crops on the page still
    # need it (~9x cheaper than per-crop); otherwise go straight to per-crop
    # calls for the stragglers.
    page_results: dict[int, dict] = {}
    if len(unmatched) >= 3:
        # Send only the unresolved cards. Limit each sheet to nine so large
        # photos do not shrink collector numbers below legibility or exhaust
        # the response budget. The same crops work after a recycled instance.
        from lib.card_finder import contact_sheet

        for start in range(0, len(unmatched), 9):
            batch = unmatched[start:start + 9]
            available = []
            images = []
            for card in batch:
                path = data_path(card["crop_path"])
                image = cv2.imread(str(path)) if runtime.ensure_local(path) else None
                if image is not None:
                    available.append(card)
                    images.append(cv2.resize(image, (480, 671), interpolation=cv2.INTER_AREA))
            if len(images) < 3:
                continue
            try:
                with tempfile.TemporaryDirectory() as folder:
                    page_path = Path(folder) / "identification.jpg"
                    cv2.imwrite(str(page_path), contact_sheet(images))
                    results = identify_page(str(page_path), rows=(len(images) + 2) // 3)
                positions = {(r.get("row"), r.get("col")): r for r in results}
                for i, card in enumerate(available):
                    if divmod(i, 3) in positions:
                        page_results[card["id"]] = positions[divmod(i, 3)]
            except Exception as exc:
                tally["last_error"] = str(exc)
                print(f"[identify] page: {exc}", file=sys.stderr)

    for card in unmatched:
        res = page_results.get(card["id"])
        patch = _identification_patch(res, card) if res else None
        crop = data_path(card["crop_path"])
        have_crop = runtime.ensure_local(crop)
        # An offline catalog cannot be repaired by another model call. Retry
        # illegible reads and actual catalog disagreements, once per card.
        if (patch is None or not patch.get("name") or
                float(res.get("confidence") or 0.0) < IDENTIFY_RETRY_CONF or
                patch.get("_catalog_status") in ("unverified", "probable")):
            if have_crop:
                try:
                    crop_res = identify_crop(str(crop))
                    crop_patch = _identification_patch(crop_res, card)
                    if patch is None or float(crop_patch.get("id_confidence") or 0.0) >= float(patch.get("id_confidence") or 0.0):
                        res, patch = crop_res, crop_patch
                except Exception as exc:
                    tally["last_error"] = str(exc)
                    print(f"[identify] crop id={card['id']}: {exc}", file=sys.stderr)
        if res and (res.get("name") or "").strip():
            _apply(card, res, patch)
            tally["by_ai"] += 1
            if have_crop:
                _remember_identification(crop, res, patch)
        _done()


def _identify_summary(tally: dict, total: int) -> str:
    message = (f"Named {tally['by_image'] + tally['by_ai']} of {total} "
               f"({tally['by_image']} from the photo, {tally['by_ai']} read)")
    missed = total - tally["by_image"] - tally["by_ai"]
    if missed and tally.get("last_error"):
        message += f", {missed} failed: {tally['last_error'][:160]}"
    return message


def _identify_job() -> Optional[int]:
    """Identify unidentified bench cards. Returns how many were identified,
    or None if the job failed. Never touches _job_state["running"]."""
    try:
        with db.connect(DB_PATH) as conn:
            cards = db.list_cards(conn, sort="oldest", unidentified_only=True)
            grids = {int(r["id"]): r for r in db.list_grids(conn)}
        _job_state["total"] = len(cards)
        if not cards:
            _job_state["message"] = "Nothing new to name"
            return 0

        by_grid: dict[Any, list[dict]] = {}
        for c in cards:
            by_grid.setdefault(c.get("grid_id"), []).append(c)

        # last_error is shown to the user; a missing API key fails every card the same way.
        tally = {"done": 0, "by_image": 0, "by_ai": 0, "last_error": ""}

        def _tick() -> None:
            _job_state["progress"] = tally["done"]
            _job_state["message"] = (
                f"Naming {tally['done']} of {len(cards)}"
            )

        for grid_id, group in by_grid.items():
            _identify_group(group, grids.get(grid_id) if grid_id is not None else None, tally, _tick)

        _job_state["message"] = _identify_summary(tally, len(cards))
        return tally["by_image"] + tally["by_ai"]
    except Exception as exc:
        traceback.print_exc()
        _job_state["message"] = f"Couldn’t name the cards: {exc}"
        return None


# ----------------------------------------------------------------------
# Jobs on a serverless host
#
# There is no process that outlives a request, so a job cannot run on a
# thread. Instead a job is a row in the database holding a queue of card ids,
# and each status request the open page makes (it polls every second or two)
# does a few seconds of that queue before answering. Closing the page pauses
# the job; opening it again carries on. Several requests can arrive at once
# (two tabs, two instances), so each slice is claimed first.
# ----------------------------------------------------------------------

MAIN_JOB = "job:main"
PUBLISH_JOB = "job:publish"
SLICE_SECONDS = float(os.getenv("JOB_SLICE_SECONDS", "12"))
LEASE_SECONDS = 150
PRICE_BATCH = 12
_IDLE = {"running": False, "progress": 0, "total": 0, "message": ""}


def _public(state: Optional[dict]) -> dict:
    state = state or _IDLE
    return {k: v for k, v in state.items() if k not in ("queue", "lease", "tally", "params")}


def _job_busy(key: str) -> bool:
    return bool((runtime.state_get(key) or {}).get("running"))


def _pump(key: str, do_slice) -> dict:
    """Do one slice of a job if it is running and nobody else holds it."""
    def claim(state: dict) -> Optional[dict]:
        if not state.get("running") or state.get("lease", 0) > time.time():
            return None
        state["lease"] = time.time() + LEASE_SECONDS
        return state

    state = runtime.state_claim(key, claim)
    if state is None:
        return _public(runtime.state_get(key))
    try:
        deadline = time.monotonic() + SLICE_SECONDS
        while True:                      # always at least one unit of work
            do_slice(state)
            # Saved after every unit of work, so a slice that dies loses one unit.
            state["lease"] = time.time() + LEASE_SECONDS
            runtime.state_set(key, state)
            if not state.get("running") or time.monotonic() >= deadline:
                break
    except Exception as exc:
        traceback.print_exc()
        state.update(running=False, message=f"failed: {exc}")
    finally:
        state["lease"] = 0
        runtime.state_set(key, state)
    return _public(state)


def _pricing_queue(only_unpriced: bool) -> list[int]:
    with db.connect(DB_PATH) as conn:
        cards = db.list_cards(conn, sort="newest")
    return [int(c["id"]) for c in cards
            if c.get("name") and not (only_unpriced and c.get("final_price") is not None)]


def _start_sliced_job(kind: str, only_unpriced: bool = False):
    if _job_busy(_jk(MAIN_JOB)):
        return jsonify({"error": "another job is running"}), 409
    state = {"running": True, "kind": kind, "progress": 0, "terapeak": False, "lease": 0,
             "tally": {"done": 0, "by_image": 0, "by_ai": 0, "last_error": ""}}
    if kind in ("identify", "scan"):
        with db.connect(DB_PATH) as conn:
            cards = db.list_cards(conn, sort="oldest", unidentified_only=True)
        # Kept in page order, each with its page, so a page is identified in one go.
        cards.sort(key=lambda c: (c.get("grid_id") is None, c.get("grid_id") or 0, c["id"]))
        state.update(phase="identify", queue=[[int(c["id"]), c.get("grid_id")] for c in cards],
                     total=len(cards), id_total=len(cards), message="identifying…")
    else:
        queue = _pricing_queue(only_unpriced)
        state.update(phase="pricing", queue=queue, total=len(queue), message="pricing…")
    runtime.state_set(_jk(MAIN_JOB), state)
    return jsonify({"started": True})


def _main_slice(state: dict) -> None:
    queue = state["queue"]
    if state["phase"] == "identify":
        tally = state["tally"]
        if queue:
            grid_id = queue[0][1]
            ids = [cid for cid, gid in queue if gid == grid_id][:9] if grid_id is not None else [queue[0][0]]
            with db.connect(DB_PATH) as conn:
                group = [c for c in (db.get_card(conn, cid) for cid in ids)
                         if c and not (c.get("name") or "").strip()]
                grid = None
                if grid_id is not None:
                    grid = db.get_grid(conn, grid_id)
            tally["done"] += len(ids) - len(group)       # removed or named by hand meanwhile
            if group:
                _identify_group(group, grid, tally)
            taken = set(ids)
            state["queue"] = [entry for entry in queue if entry[0] not in taken]
            state["progress"] = tally["done"]
            state["message"] = f"Naming {tally['done']} of {state['id_total']}"
            return
        summary = _identify_summary(tally, state["id_total"]) if state["id_total"] else "Nothing new to name"
        if state["kind"] != "scan":
            state.update(running=False, message=summary)
            return
        # Identification done; price what now has a name and no price.
        pending = _pricing_queue(only_unpriced=True)
        state.update(phase="pricing", queue=pending, progress=0, total=len(pending),
                     id_message=summary, message="pricing…")
        return

    if not queue:
        message = f"Priced {state['total']} cards" if state["total"] else "Nothing new to price"
        if state.get("id_message"):
            message = f"{state['id_message']}; {message}"
        state.update(running=False, message=message)
        return
    batch, state["queue"] = queue[:PRICE_BATCH], queue[PRICE_BATCH:]

    def price(card_id: int) -> None:
        try:
            with db.connect(DB_PATH) as conn:
                card = db.get_card(conn, card_id)
            if not card or not card.get("name"):
                return
            patch, _ = _price_card_pass1(card)
            with db.connect(DB_PATH) as conn:
                db.update_card(conn, card_id, patch)
        except Exception as exc:
            print(f"[pricing] id={card_id}: {exc}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=int(os.getenv("TCG_WORKERS", "6"))) as pool:
        list(pool.map(_as_owner(price), batch))
    state["progress"] += len(batch)
    state["message"] = f"Pricing {state['progress']} of {state['total']}"


# ----------------------------------------------------------------------
# Export + image upload
# ----------------------------------------------------------------------

def _sold_stems(strict: bool = True) -> set[str]:
    """Stems of cards that have sold. strict (the default, used wherever
    cards are about to be published or exported) lets a failed lookup raise:
    not knowing what has sold must stop a publish, not wave it through."""
    from webapp import sales_sync
    try:
        return sales_sync.sold_stems()
    except Exception as exc:
        if strict:
            raise RuntimeError(f"could not check which cards have already sold: {exc}") from exc
        print(f"[bench] sold lookup failed: {exc}", file=sys.stderr)
        return set()


def _unsold(cards: list[dict]) -> list[dict]:
    """Drop cards that have already sold somewhere. Publishing or exporting
    one again would offer a card that is no longer there."""
    sold = _sold_stems()
    return [c for c in cards
            if Path(c["crop_path"]).stem not in sold and c.get("ebay_listing_status") != "sold"]


TCG_CATALOG_PATH = OUTPUT_DIR / "cache" / "tcgplayer_catalog.csv"
_tcg_catalog_cache: dict[str, Any] = {"mtime": None, "catalog": None}


class _StoredTcgCatalog:
    """The TCGplayer export as rows in the database (serverless). Matching is
    lib.tcgplayer_catalog's, run over the few rows that share the card's
    name and number."""

    def __init__(self, count: int) -> None:
        self._count = count

    def __len__(self) -> int:
        return self._count

    def match(self, card: dict) -> Optional[str]:
        from lib.tcgplayer_catalog import TcgCatalog, _norm_name, _norm_number
        from webapp.models import TcgProduct, get_session

        name, number = _norm_name(card.get("name")), _norm_number(card.get("card_number"))
        if not name or not number:
            return None
        s = get_session()
        try:
            rows = [{"id": r.tcg_id, "name": r.name, "number": r.number, "set": r.set_name,
                     "condition": r.condition}
                    for r in s.query(TcgProduct).filter_by(name=name, number=number).all()]
        finally:
            s.close()
        return TcgCatalog(rows).match(card) if rows else None


def _store_tcg_rows(rows: list[dict], replace: bool) -> None:
    from webapp.models import TcgProduct, get_session

    s = get_session()
    try:
        if replace:
            s.query(TcgProduct).delete()
        s.bulk_insert_mappings(TcgProduct, [
            {"tcg_id": r["id"][:40], "name": r["name"][:300], "number": r["number"][:40],
             "set_name": r["set"][:300], "condition": r["condition"][:80]} for r in rows])
        s.commit()
    finally:
        s.close()


def get_tcg_catalog():
    """The seller's TCGplayer export (real TCGplayer Ids), or None if none
    has been uploaded. Re-read when the file changes."""
    from lib.tcgplayer_catalog import TcgCatalog
    if SERVERLESS:
        from webapp.models import TcgProduct, get_session
        s = get_session()
        try:
            count = s.query(TcgProduct).count()
        finally:
            s.close()
        return _StoredTcgCatalog(count) if count else None
    try:
        mtime = TCG_CATALOG_PATH.stat().st_mtime
    except OSError:
        _tcg_catalog_cache.update(mtime=None, catalog=None)
        return None
    if _tcg_catalog_cache["mtime"] != mtime:
        _tcg_catalog_cache.update(mtime=mtime, catalog=TcgCatalog.load(TCG_CATALOG_PATH))
    return _tcg_catalog_cache["catalog"]


def _tcg_rows(payload: list[dict]) -> list[dict]:
    return get_csv_gen().tcgplayer_rows(payload, get_tcg_catalog())


@app.route("/api/tcgplayer/catalog", methods=["GET"])
def api_tcg_catalog_status():
    catalog = get_tcg_catalog()
    with db.connect(DB_PATH) as conn:
        priced = [c for c in db.list_cards(conn, sort="newest") if c.get("final_price")]
    matched = sum(1 for c in priced if catalog.match(c)) if catalog else 0
    return jsonify({"loaded": catalog is not None, "rows": len(catalog) if catalog else 0,
                    "priced": len(priced), "matched": matched})


@app.route("/api/tcgplayer/catalog", methods=["POST"])
def api_tcg_catalog_upload():
    """multipart file=<the pricing CSV exported from the TCGplayer seller
    portal>. Kept and used to fill in real TCGplayer Ids.

    A host that caps request size gets the file in parts (part=N of
    parts=M, each a CSV with the header line): part 1 replaces what was
    stored, later parts add to it."""
    from lib.tcgplayer_catalog import TcgCatalog

    try:
        part, parts = int(request.form.get("part") or 1), int(request.form.get("parts") or 1)
    except ValueError:
        return jsonify({"error": "part and parts must be numbers"}), 400

    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "choose the CSV you exported from TCGplayer"}), 400
    raw = upload.read(40 * 1024 * 1024 + 1)
    if len(raw) > 40 * 1024 * 1024:
        return jsonify({"error": "that file is over 40 MB; export only the Pokemon product line"}), 400
    text = raw.decode("utf-8", errors="replace")
    try:
        catalog = TcgCatalog.parse(text)
    except ValueError as exc:
        # One part of a larger file can hold no Pokemon rows at all.
        if not (parts > 1 and "no Pokemon rows" in str(exc)):
            return jsonify({"error": str(exc)}), 400
        catalog = TcgCatalog([])
    if SERVERLESS:
        _store_tcg_rows(catalog.rows, replace=part == 1)
        if part < parts:
            return jsonify({"part": part, "parts": parts, "rows": len(catalog)})
        return api_tcg_catalog_status()
    TCG_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if part == 1:
        TCG_CATALOG_PATH.write_text(text, encoding="utf-8")
    else:
        with TCG_CATALOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(text.split("\n", 1)[1] if "\n" in text else "")
    _tcg_catalog_cache.update(mtime=None, catalog=None)
    if part < parts:
        return jsonify({"part": part, "parts": parts, "rows": len(catalog)})
    return api_tcg_catalog_status()


def _cards_payload(cards: list[dict]) -> list[dict]:
    """Coerce DB rows into the dict shape the CSV generator + listers expect."""
    payload = []
    for c in cards:
        payload.append({
            "id": c["id"],
            "crop_path": c["crop_path"],
            # A price a person set does not wait on a review of the estimate.
            "needs_review": bool(c["needs_review"]) and c.get("list_price") is None,
            "name": c["name"] or "",
            "set_name": c["set_name"] or "",
            "set_code": c["set_code"] or "",
            "card_number": c["card_number"] or "",
            "rarity": c["rarity"] or "",
            "is_holo": bool(c["is_holo"]),
            "printing": card_printing(c),
            "condition_guess": c["condition_guess"] or "",
            # The owner's own price, when they set one, is the listing price.
            "price": c.get("list_price") or c["final_price"],
            "final_price": c.get("list_price") or c["final_price"],
            "estimate": c["final_price"],
            "listing_notes": c.get("listing_notes") or "",
            "ai_description": c.get("ai_description") or "",
            "tcgplayer_product_id": c["tcgplayer_product_id"],
            "image_url": c["image_url"] or "",
            "confidence": c["pricing_confidence"],
        })
    return payload


@app.route("/api/export/csvs", methods=["POST"])
def api_export_csvs():
    gc = get_csv_gen()
    with db.connect(DB_PATH) as conn:
        cards = _unsold(db.list_cards(conn, sort="newest"))
    payload = _cards_payload(cards)

    _csv_dir().mkdir(parents=True, exist_ok=True)
    written = []
    for filename, rows_fn in (
        ("tcgplayer_bulk.csv", _tcg_rows),
        ("whatnot_seller_hub.csv", gc.whatnot_rows),
        ("ebay_bulk.csv", gc.ebay_rows),
    ):
        rows = rows_fn(payload)
        # Use pandas only when present; otherwise write naively.
        path = _csv_dir() / filename
        _write_csv(path, rows)
        written.append({"file": rel_path(path), "rows": len(rows)})
    return jsonify({"written": written})


def _write_csv(path: Path, rows: list[dict]) -> None:
    _write_csv_file(path, rows)
    if SERVERLESS:
        # The download may be served by an instance that never wrote the file.
        runtime.state_set(f"csv:{rel_path(path)}", {"text": path.read_text()})


def _write_csv_file(path: Path, rows: list[dict]) -> None:
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
    found = [c for c in cards if c]
    cards = _unsold(found)
    if len(cards) < len(found):
        return jsonify({"error": f"{len(found) - len(cards)} of the selected cards have already "
                                 "sold; untick them and try again"}), 400
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
            "printing": card_printing(c),
            "condition_guess": c.get("condition_guess") or "",
            "image_url": c.get("image_url") or "",
            "final_price": c.get("final_price"),
        })

    out_dir = _csv_dir() / "lots" / slug
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
        written.append({"file": rel_path(path), "rows": 1})

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
        crop = data_path(card["crop_path"])
        runtime.ensure_local(crop)
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
    """Readiness of each site, and what a publish would send right now, so
    the UI can say it in plain words before anything goes live."""
    from webapp import connections

    conn_status = {
        "ebay": connections._ebay_status(),
        "tcgplayer": connections._portal_status("tcgplayer"),
        "whatnot": connections._portal_status("whatnot"),
    }
    can_upload = connections.cloudinary_ready()

    with db.connect(DB_PATH) as conn:
        cards = _unsold(db.list_cards(conn, sort="newest"))
    # Each site's filter mirrors what its publish path really sends: eBay
    # needs a hosted photo (uploaded on the way when Cloudinary is set up),
    # the TCGplayer CSV drops rows with no TCGplayer product id, Whatnot
    # takes every priced card.
    priced = [c for c in cards if c.get("final_price")]

    def _group(rows: list[dict]) -> dict:
        flagged = [c for c in rows if c.get("needs_review")]
        return {
            "count": len(rows),
            "total": round(sum(c["final_price"] for c in rows), 2),
            "flagged": len(flagged),
            "flagged_total": round(sum(c["final_price"] for c in flagged), 2),
        }

    ebay_rows = [c for c in priced if c.get("image_url") or can_upload]
    catalog = get_tcg_catalog()
    tcg_rows = [c for c in priced if catalog and catalog.match(c)]
    counts = {
        "priced": len(priced),
        "ebay": {
            **_group(ebay_rows), "left_out": len(priced) - len(ebay_rows),
            "to_upload": sum(1 for c in ebay_rows if not c.get("image_url")),
            "to_upload_unflagged": sum(1 for c in ebay_rows
                                       if not c.get("image_url") and not c.get("needs_review")),
        },
        "tcgplayer": {**_group(tcg_rows), "left_out": len(priced) - len(tcg_rows),
                      "catalog": catalog is not None},
        "whatnot": {**_group(priced), "left_out": 0},
    }

    def _site(name: str) -> dict:
        st = conn_status[name]
        return {"ready": st["ready"], "reason": "" if st["ready"] else st.get("next", "")}

    return jsonify({
        "ebay": _site("ebay"),
        "tcgplayer": _site("tcgplayer"),
        "whatnot": _site("whatnot"),
        "job": _publish_state,
        "counts": counts,
    })


@app.route("/api/publish/job-status")
def api_publish_job_status():
    if SERVERLESS:
        return jsonify(_pump(_jk(PUBLISH_JOB), _ebay_publish_slice))
    return jsonify(_publish_state)


@app.route("/api/publish/ebay", methods=["POST"])
def api_publish_ebay():
    """Publish every eligible card as a live eBay listing (background job).

    Body: {"expected": N, "skip_flagged": bool}. `expected` is the count the
    user confirmed and is required; if the eligible set has changed size
    since (a pricing job finished in between), nothing is listed and the job
    says so. skip_flagged leaves out cards whose price needs review."""
    body = request.get_json(silent=True) or {}
    skip_flagged = bool(body.get("skip_flagged"))
    ai = bool(body.get("ai_descriptions"))
    # Required: a bare POST must never be enough to put listings live.
    try:
        expected = int(body["expected"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "expected (the number of cards you confirmed) is required; "
                                 "read it from /api/publish/status"}), 400
    if SERVERLESS:
        return _start_sliced_ebay_publish(expected, skip_flagged, ai)
    with _publish_lock:
        if _publish_state["running"]:
            return jsonify({"error": "a publish job is already running"}), 409
        _publish_state.update(running=True, progress=0, total=0, message="starting…", site="ebay")

    def worker():
        try:
            from lib.ebay_lister import EbayLister, EbayListingError
            from lib.ebay_oauth import EbayUserNotAuthorized
            from webapp import connections
            try:
                lister = EbayLister(auth=connections._ebay_auth())
            except Exception as exc:
                _publish_state["message"] = f"cannot start: {exc}"
                return

            can_upload = connections.cloudinary_ready()
            try:
                with db.connect(DB_PATH) as conn:
                    cards = _cards_payload(_unsold(db.list_cards(conn, sort="newest")))
            except RuntimeError as exc:
                _publish_state["message"] = f"nothing listed: {exc}"
                return
            eligible = [c for c in cards
                        if c.get("final_price") and (c.get("image_url") or can_upload)]
            if skip_flagged:
                eligible = [c for c in eligible if not c.get("needs_review")]
            if len(eligible) != expected:
                _publish_state["message"] = (
                    f"nothing listed: {len(eligible)} cards are eligible now, "
                    f"you confirmed {expected}. Check the list and publish again."
                )
                return
            _publish_state["total"] = len(eligible)

            done = listed = 0
            failures: list[str] = []
            for card in eligible:
                if not card.get("image_url"):
                    _publish_state["message"] = f"uploading photo {done + 1}/{len(eligible)}"
                try:
                    failure = _publish_one_ebay(lister, card)
                except EbayUserNotAuthorized as exc:
                    _publish_state["message"] = (
                        f"stopped after {listed} listed: eBay is not connected ({exc})")
                    return
                if failure:
                    failures.append(failure)
                else:
                    listed += 1
                    if ai and not card.get("ai_description"):
                        _descriptions.enqueue([card])
                done += 1
                _publish_state["progress"] = done
                _publish_state["message"] = f"{listed} listed, {len(failures)} failed, {done}/{len(eligible)} done"
            # Say plainly how it went; the first error is usually the one
            # that explains all of them (a missing policy, a bad category).
            message = f"done: {listed} of {len(eligible)} listed on eBay"
            if failures:
                message += f", {len(failures)} failed. First error: {failures[0][:300]}"
            _publish_state["message"] = message
        finally:
            _publish_state["running"] = False

    threading.Thread(target=_as_owner(worker), daemon=True).start()
    return jsonify({"started": True})


def _publish_one_ebay(lister, card: dict) -> Optional[str]:
    """List one card on eBay and record the outcome on its row. Returns None
    when it listed, else a short failure. A lost eBay connection is raised,
    since it stops the whole run."""
    from lib.ebay_lister import EbayListingError
    from lib.ebay_oauth import EbayUserNotAuthorized

    title = card.get("name") or Path(card.get("crop_path", "")).stem
    try:
        if not card.get("image_url"):
            # eBay needs the photo at a public URL; host it now instead of
            # making the operator do it card by card.
            from lib import cloudinary_client
            crop = data_path(card["crop_path"])
            runtime.ensure_local(crop)
            card["image_url"] = cloudinary_client.upload(str(crop))
            with db.connect(DB_PATH) as conn:
                db.update_card(conn, int(card["id"]), {"image_url": card["image_url"]})
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
        return None
    except EbayUserNotAuthorized:
        raise
    except EbayListingError as exc:
        print(f"[publish ebay] FAILED {title}: {exc}", file=sys.stderr)
        problem = str(exc)
    except Exception as exc:
        print(f"[publish ebay] error {title}: {exc}", file=sys.stderr)
        problem = str(exc)
    with db.connect(DB_PATH) as conn:
        db.update_card(conn, int(card["id"]), {"ebay_listing_status": f"error: {problem}"[:200]})
    return f"{title}: {problem}"


def _ebay_publish_set(skip_flagged: bool) -> list[dict]:
    """What a publish would list right now. Raises RuntimeError when sold
    cards cannot be told apart."""
    from webapp import connections

    can_upload = connections.cloudinary_ready()
    with db.connect(DB_PATH) as conn:
        cards = _cards_payload(_unsold(db.list_cards(conn, sort="newest")))
    eligible = [c for c in cards if c.get("final_price") and (c.get("image_url") or can_upload)]
    return [c for c in eligible if not (skip_flagged and c.get("needs_review"))]


def _start_sliced_ebay_publish(expected: int, skip_flagged: bool, ai: bool = False):
    if _job_busy(_jk(PUBLISH_JOB)):
        return jsonify({"error": "a publish job is already running"}), 409
    state = {"running": False, "progress": 0, "total": 0, "site": "ebay", "lease": 0, "ai": ai,
             "listed": 0, "failures": [], "queue": []}
    try:
        eligible = _ebay_publish_set(skip_flagged)
    except RuntimeError as exc:
        state["message"] = f"nothing listed: {exc}"
    else:
        if len(eligible) != expected:
            state["message"] = (f"nothing listed: {len(eligible)} cards are eligible now, "
                                f"you confirmed {expected}. Check the list and publish again.")
        else:
            state.update(running=True, queue=[int(c["id"]) for c in eligible], total=len(eligible),
                         message="starting…")
    runtime.state_set(_jk(PUBLISH_JOB), state)
    return jsonify({"started": True})


def _ebay_publish_slice(state: dict) -> None:
    from lib.ebay_lister import EbayLister
    from lib.ebay_oauth import EbayUserNotAuthorized
    from webapp import connections

    def finish() -> None:
        message = f"done: {state['listed']} of {state['total']} listed on eBay"
        if state["failures"]:
            message += f", {len(state['failures'])} failed. First error: {state['failures'][0][:300]}"
        state.update(running=False, message=message)

    if not state["queue"]:
        finish()
        return
    card_id = state["queue"].pop(0)
    try:
        lister = EbayLister(auth=connections._ebay_auth())   # noqa: SLF001
    except Exception as exc:
        state.update(running=False, message=f"cannot continue: {exc}")
        return
    with db.connect(DB_PATH) as conn:
        row = db.get_card(conn, card_id)
    # Listed or sold since the run started: leave it alone.
    card = _cards_payload(_unsold([row]))[0] if row and not row.get("ebay_listing_id") and _unsold([row]) else None
    if card is not None:
        try:
            failure = _publish_one_ebay(lister, card)
        except EbayUserNotAuthorized as exc:
            state.update(running=False,
                         message=f"stopped after {state['listed']} listed: eBay is not connected ({exc})")
            return
        if failure:
            state["failures"] = (state["failures"] + [failure])[:20]
            state["failed"] = state.get("failed", 0) + 1
        else:
            state["listed"] += 1
            if state.get("ai") and not card.get("ai_description"):
                _descriptions.enqueue([card])
    state["progress"] += 1
    state["message"] = (f"{state['listed']} listed, {state.get('failed', 0)} failed, "
                        f"{state['progress']}/{state['total']} done")
    if not state["queue"]:
        finish()


@app.route("/api/publish/portal", methods=["POST"])
def api_publish_portal():
    """Generate the site's CSV from the DB and upload it via headless browser."""
    if SERVERLESS:
        return jsonify({"error": "Uploading through the TCGplayer or Whatnot seller portal drives a browser "
                                 "on the machine the app runs on, which a hosted install does not have. "
                                 "Download the CSV and upload it in the portal yourself."}), 409
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
                payload = _cards_payload(_unsold(db.list_cards(conn, sort="newest")))
            _csv_dir().mkdir(parents=True, exist_ok=True)
            if site == "tcgplayer":
                filename, rows_fn = "tcgplayer_bulk.csv", _tcg_rows
                from lib.tcgplayer_lister import TCGPlayerLister as Lister
            else:
                filename, rows_fn = "whatnot_seller_hub.csv", gc.whatnot_rows
                from lib.whatnot_lister import WhatnotLister as Lister
            path = _csv_dir() / filename
            rows = rows_fn(payload)
            if not rows:
                _publish_state["message"] = (
                    "nothing uploaded: no cards to send" + (
                        " (upload your TCGplayer pricing export under Connections so cards "
                        "can be matched to TCGplayer Ids)" if site == "tcgplayer" else ""))
                return
            _write_csv(path, rows)

            _publish_state["message"] = f"uploading {filename} to {site}…"
            from lib.portal_uploader import portal_state_path
            with Lister(state_path=str(portal_state_path(site, ROOT))) as lister:
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

    threading.Thread(target=_as_owner(worker), daemon=True).start()
    return jsonify({"started": True})


# ----------------------------------------------------------------------
# Static crops + CSV downloads
# ----------------------------------------------------------------------

@app.route("/crops/<path:filename>")
@app.route("/output/crops/<path:filename>")
def serve_crop(filename: str):
    if not (CROPS_DIR / filename).exists():
        # Not on this instance's disk: the browser fetches it from storage.
        url = runtime.file_url(f"{rel_path(CROPS_DIR)}/{filename}") if ".." not in filename.split("/") else None
        if url:
            response = redirect(url, code=302)
            response.headers["Cache-Control"] = "public, max-age=86400"
            return response
    return send_from_directory(CROPS_DIR, filename)


@app.route("/csvs/<path:filename>")
@app.route("/output/csvs/<path:filename>")
def download_csv(filename: str):
    if ".." in filename.split("/"):
        abort(404)
    uid = db.owner()
    if uid is not None and filename.split("/")[0] != f"u{uid}":
        abort(404)     # another account's export
    path = CSVS_DIR / filename
    if not path.exists():
        saved = runtime.state_get(f"csv:{rel_path(path)}") if SERVERLESS else None
        if saved is None:
            abort(404)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(saved["text"])
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
# The card catalog's cache (sets, each set's cards) lives in the database, so
# every instance shares it and it survives a restart.
from lib import card_catalog as _card_catalog  # noqa: E402

_card_catalog.use_store(lambda key: runtime.state_get(f"catalog:{key}"),
                        lambda key, value: runtime.state_set(f"catalog:{key}", value))

from webapp import descriptions as _descriptions  # noqa: E402
_descriptions.use_bench(lambda: DB_PATH)


def _pump_descriptions(flush: bool = False, seconds: float = 20.0) -> dict:
    """Move this bench's AI descriptions along (webapp/descriptions.py), with
    the install's eBay connection when there is one."""
    from lib.ebay_lister import EbayLister
    from lib.ebay_mapping import EbayMapping
    from webapp import connections

    mapping = None
    if all(os.getenv(n) for n in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")):
        try:
            mapping = EbayMapping(connections._ebay_auth())   # noqa: SLF001
        except Exception as exc:
            print(f"[descriptions] eBay not usable: {exc}", file=sys.stderr)
    return _descriptions.pump(
        flush=flush, mapping=mapping, seconds=seconds,
        shape=lambda row: _cards_payload([row])[0],
        lister_factory=lambda: EbayLister(auth=connections._ebay_auth()))   # noqa: SLF001


@app.route("/api/publish/descriptions", methods=["GET", "POST"])
def api_descriptions():
    """GET: how this bench's AI descriptions stand. POST: move them along
    (send full eBay batches, collect finished ones, write the rest)."""
    if request.method == "GET":
        return jsonify(_descriptions.counts())
    return jsonify(_pump_descriptions())


@app.route("/api/cron/descriptions")
def api_cron_descriptions():
    """The second of the two daily runs (vercel.json), 12 hours after the
    first: sends any batch of fewer than 10 that is still waiting."""
    import hmac

    secret = os.getenv("CRON_SECRET") or ""
    sent = request.headers.get("Authorization") or ""
    if not secret or not hmac.compare_digest(sent, f"Bearer {secret}"):
        return jsonify({"error": "not authorized"}), 401
    return jsonify(_flush_all_descriptions())


def _flush_all_descriptions() -> dict:
    out: dict[str, Any] = {}
    for owner in _descriptions.owners_with_work():
        db.set_owner(owner)
        try:
            out[str(owner)] = _pump_descriptions(flush=True, seconds=8.0)
        except Exception as exc:
            traceback.print_exc()
            out[str(owner)] = {"error": str(exc)}
    return out

if SERVERLESS:
    # No process to run a timer in: the daily cron below does the refresh.
    price_scheduler.install(price_fn=_price_card)
else:
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

# Setup and health checks for eBay, Stripe, TCGplayer, Whatnot, Cloudinary.
from webapp.connections import connections_bp  # noqa: E402

app.register_blueprint(connections_bp)

# eBay marketplace account deletion notices (public, signature-checked).
from webapp.ebay_deletion import ebay_deletion_bp  # noqa: E402

app.register_blueprint(ebay_deletion_bp)

# Sales read back from eBay (API) and Whatnot / TCGplayer (CSV exports), and
# automatic seller payouts through Stripe Connect.
from webapp import sales_sync  # noqa: E402
from webapp.payouts import payouts_bp  # noqa: E402

from webapp.refunds import refunds_bp  # noqa: E402

app.register_blueprint(sales_sync.sales_bp)
app.register_blueprint(payouts_bp)
app.register_blueprint(refunds_bp)

# Cards already listed on eBay, TCGplayer or Whatnot, read into the library.
from webapp.listing_import import listings_bp  # noqa: E402

app.register_blueprint(listings_bp)

# Google Drive: connect an account, import photos and spreadsheets from it.
from webapp.drive import drive_bp  # noqa: E402

app.register_blueprint(drive_bp)
if not SERVERLESS:
    sales_sync.start_background()


@app.route("/api/cron/daily")
def api_cron_daily():
    """Called once a day by the host's scheduler (vercel.json) in place of
    the timers a long-running process would keep: refresh due prices, read
    eBay sales back. Vercel sends CRON_SECRET as a bearer token."""
    import hmac

    secret = os.getenv("CRON_SECRET") or ""
    sent = request.headers.get("Authorization") or ""
    if not secret or not hmac.compare_digest(sent, f"Bearer {secret}"):
        return jsonify({"error": "not authorized"}), 401
    out: dict[str, Any] = {}
    try:
        out["prices_refreshed"] = price_scheduler.refresh_cards(
            batch_size=int(os.getenv("CRON_PRICE_BATCH", "150")))
    except Exception as exc:
        traceback.print_exc()
        out["price_error"] = str(exc)
    try:
        from lib import card_catalog
        out["sets"] = card_catalog.refresh_sets()      # new sets reach identification within a day
    except Exception as exc:
        out["sets_error"] = str(exc)
    try:
        out["ebay_sales"] = sales_sync.sync_ebay()
    except Exception as exc:
        traceback.print_exc()
        out["ebay_error"] = str(exc)
    try:
        out["descriptions"] = _flush_all_descriptions()
    except Exception as exc:
        traceback.print_exc()
        out["descriptions_error"] = str(exc)
    return jsonify(out)


@app.route("/api/session")
def api_session():
    """Lets the open bench page know whether library actions need a login."""
    from webapp.auth import current_user_id
    return jsonify({
        "logged_in": bool(current_user_id()),
        # Publishing, Connections and sales sync are the operator's.
        "operator": _is_operator(),
        # A serverless host refuses request bodies over 4.5 MB. The page
        # shrinks photos and splits CSVs to stay under this.
        "max_upload_bytes": 4_000_000 if SERVERLESS else None,
        "hosted": SERVERLESS,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=os.getenv("FLASK_DEBUG") == "1")
