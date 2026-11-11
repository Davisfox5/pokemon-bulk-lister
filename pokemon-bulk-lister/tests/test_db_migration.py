"""SQLite listing-column migration + write/read/stats."""
from __future__ import annotations

import os
import sqlite3

import pytest

from webapp import db

LISTING_COLUMNS = (
    "ebay_listing_id", "ebay_offer_id", "ebay_listing_url",
    "ebay_listing_status", "listed_at",
)


def _columns(path: str) -> set[str]:
    return {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(cards)").fetchall()}


@pytest.mark.skipif(bool(os.getenv("TEST_DATABASE_URL")),
                    reason="builds a legacy SQLite file; a Postgres bench starts with every column")
def test_migration_adds_listing_columns_to_legacy_db(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    # Pre-listing schema: grids + cards.grid_id, no listing columns, has updated_at.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE grids (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT UNIQUE NOT NULL,
          original_path TEXT NOT NULL, uploaded_at TEXT, crop_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE cards (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          grid_id INTEGER REFERENCES grids(id) ON DELETE CASCADE,
          crop_path TEXT UNIQUE NOT NULL,
          name TEXT, final_price REAL, image_url TEXT,
          is_holo INTEGER NOT NULL DEFAULT 0,
          outlier_flag INTEGER NOT NULL DEFAULT 0,
          needs_review INTEGER NOT NULL DEFAULT 0,
          pricing_confidence REAL NOT NULL DEFAULT 0.0,
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    assert not (set(LISTING_COLUMNS) & _columns(path))
    db.init_db(path)
    assert set(LISTING_COLUMNS) <= _columns(path)


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "fresh.sqlite")
    db.init_db(path)
    cols_first = _columns(path)
    db.init_db(path)  # second run must not error or change schema
    assert _columns(path) == cols_first


def test_listing_write_and_stat(tmp_path):
    path = str(tmp_path / "fresh.sqlite")
    db.init_db(path)
    with db.connect(path) as conn:
        gid = db.get_or_create_grid(conn, "page01", "input/grids/page01.jpg")
        cid = db.insert_card_stub(conn, gid, "output/crops/page01_r0c0.jpg", 0, 0)
        db.update_card(conn, cid, {"name": "Pikachu", "final_price": 12.5, "image_url": "http://img"})
        db.update_card(conn, cid, {
            "ebay_listing_id": "v1|123|0",
            "ebay_offer_id": "of_1",
            "ebay_listing_url": "https://www.ebay.com/itm/123",
            "ebay_listing_status": "listed",
            "listed_at": "2026-05-28T00:00:00Z",
        })
        card = db.get_card(conn, cid)
        stats = db.card_stats(conn)

    assert card["ebay_listing_id"] == "v1|123|0"
    assert card["ebay_listing_status"] == "listed"
    assert stats["ebay_listed"] == 1
