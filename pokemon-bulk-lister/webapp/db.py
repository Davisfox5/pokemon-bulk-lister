"""Storage for the scan bench: one row per crop (cards table) plus a small
grids table for the upload provenance. All fields nullable; the pipeline
fills them in across multiple steps.

Two backends behind the same functions:

  SQLite    the default. A file on this machine (output/db.sqlite).
  Postgres  when DATABASE_URL (or BENCH_DATABASE_URL) is a postgres URL. For
            hosts with no disk that persists, such as Vercel. The db_path
            argument is then ignored.

Callers use `with connect(path) as conn:` and `conn.execute(sql, params)`
with `?` placeholders either way; rows read like dicts.

Whose bench. On your own machine there is one bench and nobody signs in:
rows have no owner. Served to several accounts, each account has its own
bench: every row carries `owner_id`, and every function here reads and
writes only the rows of the owner set for the current request with
`set_owner` (a context variable, so it follows the request into the helpers
without being passed around). `owner_id IS NULL` is the single local bench.
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


DEFAULT_DB_PATH = "output/db.sqlite"

_owner: ContextVar[Optional[int]] = ContextVar("bench_owner", default=None)


def set_owner(user_id: Optional[int]) -> None:
    """Whose bench the current request works on. None is the local bench."""
    _owner.set(int(user_id) if user_id else None)


def owner() -> Optional[int]:
    return _owner.get()


def _mine(prefix: str = "") -> tuple[str, list]:
    """SQL condition and parameters selecting the current owner's rows."""
    uid = _owner.get()
    return (f"{prefix}owner_id IS NULL", []) if uid is None else (f"{prefix}owner_id = ?", [uid])


SCHEMA = """
CREATE TABLE IF NOT EXISTS grids (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT UNIQUE NOT NULL,
    original_path TEXT NOT NULL,
    uploaded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    crop_count    INTEGER NOT NULL DEFAULT 0,
    owner_id      INTEGER
);

CREATE TABLE IF NOT EXISTS cards (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id                 INTEGER REFERENCES grids(id) ON DELETE CASCADE,
    crop_path               TEXT UNIQUE NOT NULL,
    row                     INTEGER,
    col                     INTEGER,

    -- identification (filled by you / Claude vision)
    name                    TEXT,
    set_name                TEXT,
    set_code                TEXT,
    card_number             TEXT,
    rarity                  TEXT,
    is_holo                 INTEGER NOT NULL DEFAULT 0,
    condition_guess         TEXT,
    id_confidence           REAL NOT NULL DEFAULT 0.0,

    -- prices
    tcgplayer_market        REAL,
    cardmarket_trend_eur    REAL,
    cardmarket_trend_usd    REAL,
    ebay_median_30d         REAL,
    ebay_max_30d            REAL,
    ebay_sold_count_30d     INTEGER NOT NULL DEFAULT 0,
    terapeak_median_usd     REAL,
    terapeak_sold_count_365d INTEGER NOT NULL DEFAULT 0,
    final_price             REAL,
    pricing_confidence      REAL NOT NULL DEFAULT 0.0,
    outlier_flag            INTEGER NOT NULL DEFAULT 0,
    needs_review            INTEGER NOT NULL DEFAULT 0,
    pricing_notes           TEXT,

    -- enrichment
    tcgplayer_product_id    TEXT,
    tcgplayer_url           TEXT,
    cardmarket_url          TEXT,
    image_url               TEXT,

    -- listing (eBay Sell API is per-card; TCGPlayer/Whatnot are batch CSV uploads)
    ebay_listing_id         TEXT,
    ebay_offer_id           TEXT,
    ebay_listing_url        TEXT,
    ebay_listing_status     TEXT,
    listed_at               TEXT,

    -- whose bench this row is on (NULL: the single local bench)
    owner_id                INTEGER,

    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cards_grid         ON cards(grid_id);
CREATE INDEX IF NOT EXISTS idx_cards_needs_review ON cards(needs_review);
CREATE INDEX IF NOT EXISTS idx_cards_name         ON cards(name);
"""


CARD_FIELDS = (
    "id", "grid_id", "crop_path", "row", "col",
    "name", "set_name", "set_code", "card_number", "rarity",
    "is_holo", "condition_guess", "id_confidence",
    "tcgplayer_market", "cardmarket_trend_eur", "cardmarket_trend_usd",
    "ebay_median_30d", "ebay_max_30d", "ebay_sold_count_30d",
    "terapeak_median_usd", "terapeak_sold_count_365d",
    "final_price", "pricing_confidence", "outlier_flag",
    "needs_review", "pricing_notes",
    "tcgplayer_product_id", "tcgplayer_url", "cardmarket_url", "image_url",
    "ebay_listing_id", "ebay_offer_id", "ebay_listing_url", "ebay_listing_status", "listed_at",
    "created_at", "updated_at",
)

# Columns added after the original schema shipped; applied as idempotent
# ALTER TABLEs so existing DBs migrate forward on launch.
_MIGRATIONS = (
    ("ebay_listing_id", "TEXT"),
    ("ebay_offer_id", "TEXT"),
    ("ebay_listing_url", "TEXT"),
    ("ebay_listing_status", "TEXT"),
    ("listed_at", "TEXT"),
    ("owner_id", "INTEGER"),
)
_GRID_MIGRATIONS = (
    ("owner_id", "INTEGER"),
)
# After the columns exist (an older database gets them from the migrations).
_OWNER_INDEX = "CREATE INDEX IF NOT EXISTS idx_cards_owner ON cards(owner_id)"

EDITABLE_ID_FIELDS = (
    "name", "set_name", "set_code", "card_number", "rarity",
    "is_holo", "condition_guess", "id_confidence",
)


def pg_url() -> Optional[str]:
    """The Postgres URL the bench should use, in the plain form the driver
    takes, or None to use the SQLite file."""
    url = (os.getenv("BENCH_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return None


def _now() -> str:
    """UTC, in the same text form SQLite's datetime('now') writes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pg_schema() -> list[str]:
    """SCHEMA in Postgres's spelling, one statement per entry."""
    sql = (SCHEMA
           .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
           # SQLite's REAL is 8 bytes; Postgres's is 4 and would blur prices.
           .replace(" REAL", " DOUBLE PRECISION")
           .replace("(datetime('now'))", "(to_char(timezone('utc', now()), 'YYYY-MM-DD HH24:MI:SS'))"))
    lines = [line.split("--")[0] for line in sql.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


class _PgConnection:
    """The slice of sqlite3.Connection this app uses, over psycopg: `?`
    placeholders, rows that read like dicts, every statement committed."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def execute(self, sql: str, params=()):
        return self.raw.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))


# Opening a connection to a hosted Postgres costs a TLS handshake, so a few
# are kept open between requests.
_pg_idle: "queue.LifoQueue" = queue.LifoQueue(maxsize=4)


@contextmanager
def _pg_connect(url: str) -> Iterator[_PgConnection]:
    import psycopg
    from psycopg.rows import dict_row

    raw = None
    while raw is None:
        try:
            kept_url, candidate = _pg_idle.get_nowait()
        except queue.Empty:
            break
        if kept_url == url and not candidate.closed and not candidate.broken:
            raw = candidate
        else:
            candidate.close()
    if raw is None:
        # prepare_threshold=None: a hosted Postgres is usually reached through
        # a pooler that hands each statement to a different server connection,
        # where a statement prepared earlier does not exist.
        raw = psycopg.connect(url, autocommit=True, row_factory=dict_row, prepare_threshold=None)
    try:
        yield _PgConnection(raw)
    except BaseException:
        raw.close()
        raise
    else:
        try:
            _pg_idle.put_nowait((url, raw))
        except queue.Full:
            raw.close()


# Any 64-bit number; it only has to be the same everywhere this app runs.
_PG_SCHEMA_LOCK = 7_203_114_905


@contextmanager
def connect(db_path: str = DEFAULT_DB_PATH) -> Iterator[Any]:
    url = pg_url()
    if url:
        with _pg_connect(url) as conn:
            yield conn
        return
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    # WAL allows one writer at a time; without a busy_timeout a second concurrent
    # writer raises "database is locked" immediately, and the parallel pricing
    # job then silently drops that card's update. Wait instead.
    conn.execute("PRAGMA busy_timeout = 10000;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        if isinstance(conn, _PgConnection):
            # Two instances starting at once must not both create the tables.
            # The schema above already has every column, so the SQLite
            # migrations below have nothing to do here.
            # The lock lasts exactly as long as this one transaction. (A
            # session lock would not survive a pooler, which may run the
            # unlock on a different server connection than the lock.)
            with conn.raw.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(?)", (_PG_SCHEMA_LOCK,))
                for statement in _pg_schema():
                    conn.execute(statement)
                # A database made before benches had owners.
                conn.execute("ALTER TABLE grids ADD COLUMN IF NOT EXISTS owner_id BIGINT")
                conn.execute("ALTER TABLE cards ADD COLUMN IF NOT EXISTS owner_id BIGINT")
                conn.execute(_OWNER_INDEX)
            return
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
    for column, coltype in _MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {column} {coltype}")
    grid_columns = {row["name"] for row in conn.execute("PRAGMA table_info(grids)").fetchall()}
    for column, coltype in _GRID_MIGRATIONS:
        if column not in grid_columns:
            conn.execute(f"ALTER TABLE grids ADD COLUMN {column} {coltype}")
    conn.execute(_OWNER_INDEX)


def get_or_create_grid(conn: sqlite3.Connection, filename: str, original_path: str) -> int:
    mine, args = _mine()
    row = conn.execute(f"SELECT id FROM grids WHERE filename = ? AND {mine}", [filename] + args).fetchone()
    if row:
        return int(row["id"])
    # File names are unique across every bench (the caller picks an unused
    # one), so a new row never collides with another owner's.
    if isinstance(conn, _PgConnection):
        made = conn.execute(
            "INSERT INTO grids (filename, original_path, owner_id) VALUES (?, ?, ?) RETURNING id",
            (filename, original_path, owner()),
        ).fetchone()
        return int(made["id"])
    cur = conn.execute(
        "INSERT INTO grids (filename, original_path, owner_id) VALUES (?, ?, ?)",
        (filename, original_path, owner()),
    )
    return int(cur.lastrowid)


def insert_card_stub(
    conn: sqlite3.Connection,
    grid_id: int,
    crop_path: str,
    row: int,
    col: int,
) -> int:
    if isinstance(conn, _PgConnection):
        made = conn.execute(
            "INSERT INTO cards (grid_id, crop_path, row, col, owner_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (crop_path) DO NOTHING RETURNING id",
            (grid_id, crop_path, row, col, owner()),
        ).fetchone()
        if made:
            return int(made["id"])
        existing = conn.execute("SELECT id FROM cards WHERE crop_path = ?", (crop_path,)).fetchone()
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO cards (grid_id, crop_path, row, col, owner_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (grid_id, crop_path, row, col, owner()),
    )
    if cur.lastrowid:
        return int(cur.lastrowid)
    existing = conn.execute(
        "SELECT id FROM cards WHERE crop_path = ?", (crop_path,)
    ).fetchone()
    return int(existing["id"])


def update_grid_count(conn: sqlite3.Connection, grid_id: int) -> None:
    conn.execute(
        "UPDATE grids SET crop_count = (SELECT COUNT(*) FROM cards WHERE grid_id = ?) WHERE id = ?",
        (grid_id, grid_id),
    )


def list_cards(
    conn: sqlite3.Connection,
    sort: str = "confidence_asc",
    needs_review_only: bool = False,
    unidentified_only: bool = False,
) -> list[dict]:
    mine, args = _mine()
    where = [mine]
    if needs_review_only:
        where.append("needs_review = 1")
    if unidentified_only:
        where.append("(name IS NULL OR name = '')")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    order = {
        "confidence_asc": "pricing_confidence ASC, needs_review DESC, id ASC",
        "confidence_desc": "pricing_confidence DESC, id ASC",
        "price_desc": "final_price DESC NULLS LAST, id ASC",
        "price_asc": "final_price ASC NULLS LAST, id ASC",
        "newest": "created_at DESC, id DESC",
        "oldest": "created_at ASC, id ASC",
    }.get(sort, "pricing_confidence ASC, id ASC")

    rows = conn.execute(f"SELECT * FROM cards {where_sql} ORDER BY {order}", args).fetchall()
    return [dict(r) for r in rows]


def list_grids(conn: sqlite3.Connection) -> list[dict]:
    mine, args = _mine()
    return [dict(r) for r in conn.execute(f"SELECT * FROM grids WHERE {mine}", args).fetchall()]


def get_grid(conn: sqlite3.Connection, grid_id: int) -> Optional[dict]:
    mine, args = _mine()
    row = conn.execute(f"SELECT * FROM grids WHERE id = ? AND {mine}", [grid_id] + args).fetchone()
    return dict(row) if row else None


def card_id_by_crop(conn: sqlite3.Connection, crop_path: str) -> Optional[int]:
    mine, args = _mine()
    row = conn.execute(f"SELECT id FROM cards WHERE crop_path = ? AND {mine}", [crop_path] + args).fetchone()
    return int(row["id"]) if row else None


def adopt_unowned(conn: sqlite3.Connection, user_id: int) -> None:
    """Give the rows of the old single bench (no owner) to one account. Used
    once, for the operator, when an install moves under a public host."""
    for table in ("grids", "cards"):
        conn.execute(f"UPDATE {table} SET owner_id = ? WHERE owner_id IS NULL", (int(user_id),))
    conn.commit()


def get_card(conn: sqlite3.Connection, card_id: int, any_owner: bool = False) -> Optional[dict]:
    """One card of the current owner's bench. any_owner is for the few jobs
    that act for the whole install by a card's globally unique id (marking a
    card sold after a sale is read back), never for a request's own data."""
    if any_owner:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    else:
        mine, args = _mine()
        row = conn.execute(f"SELECT * FROM cards WHERE id = ? AND {mine}", [card_id] + args).fetchone()
    return dict(row) if row else None


def update_card(conn: sqlite3.Connection, card_id: int, patch: dict[str, Any],
                any_owner: bool = False) -> Optional[dict]:
    # A card on someone else's bench does not exist as far as this one goes.
    if get_card(conn, card_id, any_owner) is None:
        return None
    safe = {k: v for k, v in patch.items()
            if k in CARD_FIELDS and k not in ("id", "crop_path", "created_at", "owner_id")}
    if not safe:
        return get_card(conn, card_id, any_owner)
    # Coerce booleans.
    for bool_field in ("is_holo", "outlier_flag", "needs_review"):
        if bool_field in safe:
            safe[bool_field] = 1 if safe[bool_field] else 0
    cols = ", ".join(f"{k} = ?" for k in safe.keys())
    values = list(safe.values()) + [_now(), card_id]
    conn.execute(
        f"UPDATE cards SET {cols}, updated_at = ? WHERE id = ?",
        values,
    )
    return get_card(conn, card_id, any_owner)


def card_stats(conn: sqlite3.Connection) -> dict:
    mine, args = _mine()
    rows = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN name IS NOT NULL AND name <> '' THEN 1 ELSE 0 END) AS identified,
            SUM(CASE WHEN final_price IS NOT NULL THEN 1 ELSE 0 END) AS priced,
            SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS flagged,
            SUM(CASE WHEN image_url IS NOT NULL AND image_url <> '' THEN 1 ELSE 0 END) AS uploaded,
            SUM(CASE WHEN ebay_listing_id IS NOT NULL AND ebay_listing_id <> '' THEN 1 ELSE 0 END) AS ebay_listed,
            COALESCE(SUM(final_price), 0) AS total_value
        FROM cards
        WHERE {mine}
        """,
        args,
    ).fetchone()
    return dict(rows or {})


def maybe_import_legacy_json(
    conn: sqlite3.Connection,
    cards_json: str = "output/cards.json",
    cards_priced_json: str = "output/cards_priced.json",
) -> int:
    """If the DB is empty and legacy JSON files exist, import them."""
    count = conn.execute("SELECT COUNT(*) AS n FROM cards WHERE owner_id IS NULL").fetchone()
    if count and int(count["n"]) > 0:
        return 0

    legacy_path = Path(cards_priced_json) if Path(cards_priced_json).exists() else Path(cards_json)
    if not legacy_path.exists():
        return 0
    try:
        entries = json.loads(legacy_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    imported = 0
    for entry in entries:
        crop_path = entry.get("crop_path")
        if not crop_path:
            continue
        # Fake grid record from the filename stem.
        grid_filename = Path(crop_path).stem.split("_r")[0] or Path(crop_path).stem
        grid_id = get_or_create_grid(conn, grid_filename, crop_path)
        row, col = _parse_row_col(crop_path)
        card_id = insert_card_stub(conn, grid_id, crop_path, row, col)

        patch: dict[str, Any] = {
            k: entry.get(k)
            for k in (
                "name", "set_name", "set_code", "card_number", "rarity",
                "is_holo", "condition_guess",
                "tcgplayer_market", "cardmarket_trend_eur",
                "ebay_median_30d", "ebay_max_30d", "ebay_sold_count_30d",
                "terapeak_median_usd", "terapeak_sold_count_365d",
                "outlier_flag", "needs_review", "pricing_notes",
                "tcgplayer_product_id", "tcgplayer_url", "cardmarket_url", "image_url",
            )
            if k in entry
        }
        patch["id_confidence"] = float(entry.get("confidence", 0.0) or 0.0)
        patch["final_price"] = entry.get("price")
        patch["pricing_confidence"] = float(entry.get("confidence", 0.0) or 0.0)
        sources = entry.get("sources") or {}
        if isinstance(sources, dict):
            patch.setdefault("tcgplayer_market", sources.get("tcgplayer_market"))
            patch.setdefault("ebay_median_30d", sources.get("ebay_median_30d"))
            patch.setdefault("ebay_max_30d", sources.get("ebay_max_30d"))
            patch.setdefault("cardmarket_trend_usd", sources.get("cardmarket_trend_usd"))
            patch.setdefault("terapeak_median_usd", sources.get("terapeak_median_usd"))

        update_card(conn, card_id, patch)
        imported += 1

    for grid_id_row in conn.execute("SELECT id FROM grids WHERE owner_id IS NULL").fetchall():
        update_grid_count(conn, int(grid_id_row["id"]))

    return imported


def _parse_row_col(crop_path: str) -> tuple[Optional[int], Optional[int]]:
    """Pull row/col from a path like '..._r0c2.jpg'."""
    stem = Path(crop_path).stem
    if "_r" not in stem or "c" not in stem:
        return None, None
    try:
        tail = stem.split("_r")[-1]
        r, c = tail.split("c")
        return int(r), int(c)
    except (ValueError, IndexError):
        return None, None
