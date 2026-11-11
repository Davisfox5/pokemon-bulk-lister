"""Multi-user data layer: users, canonical card catalog, per-user inventory,
price history, alerts, and eBay account links.

Design: the legacy `cards` table (webapp/db.py) stays as the single-user
"processing bench" — photos in, identifications and prices out. This module
adds the productized layer on top:

  catalog_cards   one row per (name, set, number, holo) variant — prices live
                  HERE, so N users owning the same card share one price fetch.
  inventory_items a user's claim on a catalog card (qty, condition, cost basis).
  price_history   append-only snapshots per catalog card → portfolio charts
                  and pct-change alerts.

Engine is SQLAlchemy against DATABASE_URL (defaults to the existing SQLite
file), so pointing DATABASE_URL at Postgres is a config change, not a rewrite.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = f"sqlite:///{ROOT / 'output' / 'db.sqlite'}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_URL)


def utcnow() -> datetime:
    # Naive UTC: SQLite round-trips naive datetimes; mixing aware/naive breaks
    # scheduler age comparisons.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    inventory: Mapped[list["InventoryItem"]] = relationship(back_populates="user")


class CatalogCard(Base):
    """Canonical card variant. Prices attach here, never to inventory."""

    __tablename__ = "catalog_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    variant_key: Mapped[str] = mapped_column(String(400), unique=True, nullable=False)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    set_name: Mapped[Optional[str]] = mapped_column(String(200))
    set_code: Mapped[Optional[str]] = mapped_column(String(40))
    card_number: Mapped[Optional[str]] = mapped_column(String(40))
    rarity: Mapped[Optional[str]] = mapped_column(String(80))
    is_holo: Mapped[bool] = mapped_column(Boolean, default=False)

    tcgplayer_product_id: Mapped[Optional[str]] = mapped_column(String(80))
    tcgplayer_url: Mapped[Optional[str]] = mapped_column(Text)
    cardmarket_url: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(Text)

    # Latest aggregated price (mirrors the newest price_history row).
    final_price: Mapped[Optional[float]] = mapped_column(Float)
    pricing_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    outlier_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    pricing_notes: Mapped[Optional[str]] = mapped_column(Text)
    tcgplayer_market: Mapped[Optional[float]] = mapped_column(Float)
    cardmarket_trend_usd: Mapped[Optional[float]] = mapped_column(Float)
    ebay_median_30d: Mapped[Optional[float]] = mapped_column(Float)
    ebay_max_30d: Mapped[Optional[float]] = mapped_column(Float)
    last_priced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    history: Mapped[list["PriceSnapshot"]] = relationship(back_populates="card")


class PriceSnapshot(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_card_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_cards.id", ondelete="CASCADE"), nullable=False
    )
    priced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    final_price: Mapped[Optional[float]] = mapped_column(Float)
    pricing_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    tcgplayer_market: Mapped[Optional[float]] = mapped_column(Float)
    cardmarket_trend_usd: Mapped[Optional[float]] = mapped_column(Float)
    ebay_median_30d: Mapped[Optional[float]] = mapped_column(Float)

    card: Mapped[CatalogCard] = relationship(back_populates="history")


Index("idx_price_history_card_time", PriceSnapshot.catalog_card_id, PriceSnapshot.priced_at)


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    catalog_card_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_cards.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # Default NM: house rule — cards are Near Mint unless explicitly noted.
    condition: Mapped[str] = mapped_column(String(20), default="NM")
    acquired_price: Mapped[Optional[float]] = mapped_column(Float)
    acquired_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    source_crop_path: Mapped[Optional[str]] = mapped_column(Text)
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="inventory")
    catalog_card: Mapped[CatalogCard] = relationship()


Index("idx_inventory_user", InventoryItem.user_id)


class AlertRule(Base):
    """kind: 'price_above' | 'price_below' | 'pct_change' | 'sell_signal'.

    catalog_card_id NULL = applies to every card in the user's inventory.
    threshold: dollars for above/below; percent for pct_change/sell_signal.
    """

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    catalog_card_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("catalog_cards.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("alert_rules.id", ondelete="SET NULL"))
    catalog_card_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("catalog_cards.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("idx_alerts_user_unread", Alert.user_id, Alert.is_read)


class MarketListing(Base):
    """A card offered for sale on the platform. status: active | sold | cancelled."""

    __tablename__ = "market_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    inventory_item_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False
    )
    catalog_card_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_cards.id", ondelete="RESTRICT"), nullable=False
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    seller: Mapped[User] = relationship()
    inventory_item: Mapped[InventoryItem] = relationship()
    catalog_card: Mapped[CatalogCard] = relationship()


Index("idx_market_listings_status", MarketListing.status)


class Order(Base):
    """A purchase of a MarketListing. The platform-fee ledger lives here:
    amount = what the buyer pays, platform_fee = our cut, seller_proceeds =
    amount - platform_fee (owed to the seller once paid).

    status: pending_payment | paid | completed | cancelled | refund_due
            (refund_due: a card payment has to be sent back and Stripe
            refused the automatic refund; the operator must do it by hand)
    payment_provider: 'stripe' | 'offline'
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("market_listings.id", ondelete="RESTRICT"), nullable=False
    )
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    platform_fee: Mapped[float] = mapped_column(Float, nullable=False)
    seller_proceeds: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending_payment")
    payment_provider: Mapped[str] = mapped_column(String(20), default="offline")
    payment_ref: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    listing: Mapped[MarketListing] = relationship()


Index("idx_orders_buyer", Order.buyer_id)
Index("idx_orders_seller", Order.seller_id)


class EbayAccount(Base):
    __tablename__ = "ebay_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text)
    access_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    inventory_item_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="CASCADE"), nullable=False
    )
    marketplace: Mapped[str] = mapped_column(String(20), default="ebay")
    sku: Mapped[Optional[str]] = mapped_column(String(80))
    offer_id: Mapped[Optional[str]] = mapped_column(String(80))
    listing_id: Mapped[Optional[str]] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    price: Mapped[Optional[float]] = mapped_column(Float)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LibraryEvent(Base):
    """Append-only ledger of what entered and left a user's library.

    Inventory rows only say what a user holds now. This table is the record of
    every card they added, bought or sold, and it outlives the inventory row
    (a sold card moves to the buyer; the seller keeps the event).

    kind:    'added' (scanned or entered, no purchase) | 'purchased' | 'sold'
             | 'removed' (taken out by hand with no sale: a correction, a
             gift, a lost card)
    channel: where it happened: 'scan', 'manual', 'market' (this platform),
             'ebay', 'tcgplayer', 'whatnot', 'in_person', 'other'
    unit_price: per-card price paid or received. unit_cost: per-card cost
    basis at the time of a sale, so realized gain survives later edits.
    """

    __tablename__ = "library_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    catalog_card_id: Mapped[int] = mapped_column(
        ForeignKey("catalog_cards.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), default="manual")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Optional[float]] = mapped_column(Float)
    unit_cost: Mapped[Optional[float]] = mapped_column(Float)
    fees: Mapped[Optional[float]] = mapped_column(Float)
    condition: Mapped[Optional[str]] = mapped_column(String(20))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    image_url: Mapped[Optional[str]] = mapped_column(Text)
    crop_path: Mapped[Optional[str]] = mapped_column(Text)
    note: Mapped[Optional[str]] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    catalog_card: Mapped[CatalogCard] = relationship()


Index("idx_library_events_user_kind", LibraryEvent.user_id, LibraryEvent.kind)

EVENT_KINDS = ("added", "purchased", "sold", "removed")
EVENT_CHANNELS = (
    "scan", "manual", "market", "ebay", "tcgplayer", "whatnot", "in_person", "other",
)


class Payout(Base):
    """Money the platform operator has paid out to a seller.

    Card payments land in the operator's Stripe account, so each sale leaves
    the operator owing the seller their proceeds. A row here settles part of
    that. Seller balance = card-payment proceeds - fees owed on sales settled
    directly - payouts.
    """

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[Optional[str]] = mapped_column(String(40))
    note: Mapped[Optional[str]] = mapped_column(Text)
    recorded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("idx_payouts_seller", Payout.seller_id)


class EmailVerification(Base):
    """Proof that a user controls their email address.

    One row per user. The link mailed out carries a random token; only its
    SHA-256 is stored, so reading the database does not yield a usable link.
    verified_at set = verified. (A separate table rather than a column on
    users, because this module cannot add columns to an existing table.)
    """

    __tablename__ = "email_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    token_hash: Mapped[Optional[str]] = mapped_column(String(64))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ExternalSale(Base):
    """A sale read back from outside the app: an eBay order line, or a row of
    a Whatnot / TCGplayer sales export.

    (source, external_id) is unique, so reading the same order twice records
    it once. status:
      recorded   matched to a library card; a 'sold' ledger row was written
      unmatched  no library card with that SKU; shown for a person to handle
      dismissed  a person said to ignore it
      void       cancelled or fully refunded at the source; nothing recorded
    """

    __tablename__ = "external_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(200))
    title: Mapped[Optional[str]] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[Optional[float]] = mapped_column(Float)
    fees: Mapped[Optional[float]] = mapped_column(Float)
    sold_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default="unmatched")
    detail: Mapped[Optional[str]] = mapped_column(Text)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    library_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("library_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


Index("uq_external_sale", ExternalSale.source, ExternalSale.external_id, unique=True)


class SellerPayoutAccount(Base):
    """A seller's Stripe connected account, for automatic payouts."""

    __tablename__ = "seller_payout_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    stripe_account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    # Cached from Stripe; refreshed whenever the account is looked at.
    transfers_active: Mapped[bool] = mapped_column(Boolean, default=False)
    payouts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    details_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OrderTransfer(Base):
    """The Stripe transfer that paid a seller for one order. order_id is
    unique: an order's proceeds can be transferred once."""

    __tablename__ = "order_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    transfer_id: Mapped[str] = mapped_column(String(80), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Deck(Base):
    """A saved decklist. source: 'paste' (user import) | 'popular' (copied
    from a tournament list; source_meta holds the JSON provenance)."""

    __tablename__ = "decks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="paste")
    source_meta: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    cards: Mapped[list["DeckCard"]] = relationship(
        back_populates="deck", cascade="all, delete-orphan", order_by="DeckCard.position"
    )


class DeckCard(Base):
    """One line of a decklist. set_code is the Pokemon TCG Live code (TWM),
    not the pokemontcg.io id; lib.decks.SetCodes translates."""

    __tablename__ = "deck_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deck_id: Mapped[int] = mapped_column(ForeignKey("decks.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(20), default="pokemon")
    count: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    set_code: Mapped[Optional[str]] = mapped_column(String(40))
    card_number: Mapped[Optional[str]] = mapped_column(String(40))

    deck: Mapped[Deck] = relationship(back_populates="cards")


Index("idx_decks_user", Deck.user_id)
Index("idx_deck_cards_deck", DeckCard.deck_id)


# ----------------------------------------------------------------------
# Engine / session
# ----------------------------------------------------------------------

_engine = None
_SessionLocal = None


def init_models(url: Optional[str] = None) -> None:
    """Create the engine and any missing tables. Passing `url` rebinds to a
    different database (the test suite points this at a temp file)."""
    global _engine, _SessionLocal
    if _engine is not None and url is None:
        return
    if _engine is not None:
        _engine.dispose()
    db_url = url or DATABASE_URL
    kwargs: dict[str, Any] = {"future": True}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(db_url, **kwargs)
    if db_url.startswith("sqlite"):
        # The legacy bench (raw sqlite3) and the scheduler write concurrently;
        # without WAL + busy_timeout, cross-connection writes raise
        # "database is locked".
        from sqlalchemy import event

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    Base.metadata.create_all(_engine)
    _ensure_indexes(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _ensure_indexes(engine) -> None:
    """Indexes on tables that already exist in older databases.

    `create_all` only creates missing tables, so an index added to an
    existing table has to be applied here. Each step is idempotent and runs
    on every launch.

    uq_active_listing: a card can have one active marketplace listing. The
    check-then-insert in the listing endpoints cannot guarantee that under
    concurrent requests; this partial unique index can. Duplicates left by an
    older build are cancelled first (the oldest listing is kept), or the
    index could not be created.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        # Keep one active listing per card: the one a buyer is waiting on if
        # there is one, else the oldest.
        rows = conn.execute(text(
            "SELECT l.id, l.inventory_item_id, "
            "  EXISTS (SELECT 1 FROM orders o WHERE o.listing_id = l.id "
            "          AND o.status = 'pending_payment') AS has_pending "
            "FROM market_listings l WHERE l.status = 'active' ORDER BY l.id"
        )).fetchall()
        keep: dict[int, tuple] = {}
        for listing_id, item_id, has_pending in rows:
            best = keep.get(item_id)
            if best is None or (has_pending and not best[1]):
                keep[item_id] = (listing_id, bool(has_pending))
        kept = {v[0] for v in keep.values()}
        for listing_id, _item_id, _pending in rows:
            if listing_id not in kept:
                conn.execute(text("UPDATE market_listings SET status = 'cancelled' WHERE id = :i"),
                             {"i": listing_id})
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_active_listing "
            "ON market_listings (inventory_item_id) WHERE status = 'active'"
        ))

        # uq_pending_order: one buyer at a time per listing. buy() checks
        # first, but two buyers clicking together could both pass the check.
        conn.execute(text(
            "UPDATE orders SET status = 'cancelled' "
            "WHERE status = 'pending_payment' AND id NOT IN ("
            "  SELECT MIN(id) FROM orders WHERE status = 'pending_payment' "
            "  GROUP BY listing_id)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_order "
            "ON orders (listing_id) WHERE status = 'pending_payment'"
        ))


def get_session() -> Session:
    if _SessionLocal is None:
        init_models()
    return _SessionLocal()


# ----------------------------------------------------------------------
# Catalog helpers
# ----------------------------------------------------------------------

def variant_key(
    name: str,
    set_code: Optional[str],
    set_name: Optional[str],
    card_number: Optional[str],
    is_holo: bool,
) -> str:
    def norm(s: Optional[str]) -> str:
        return (s or "").strip().lower()

    set_part = norm(set_code) or norm(set_name)
    return "|".join([norm(name), set_part, norm(card_number), "holo" if is_holo else "norm"])


PRICE_FIELDS = (
    "final_price", "pricing_confidence", "outlier_flag", "pricing_notes",
    "tcgplayer_market", "cardmarket_trend_usd", "ebay_median_30d", "ebay_max_30d",
)
ENRICH_FIELDS = ("tcgplayer_product_id", "tcgplayer_url", "cardmarket_url", "image_url")


def get_or_create_catalog_card(session: Session, card: dict) -> CatalogCard:
    """Find-or-create the canonical variant for a bench-card-shaped dict."""
    key = variant_key(
        card.get("name") or "",
        card.get("set_code"),
        card.get("set_name"),
        card.get("card_number"),
        bool(card.get("is_holo")),
    )
    cc = session.query(CatalogCard).filter_by(variant_key=key).one_or_none()
    if cc is None:
        cc = CatalogCard(
            variant_key=key,
            name=(card.get("name") or "").strip(),
            set_name=card.get("set_name"),
            set_code=card.get("set_code"),
            card_number=card.get("card_number"),
            rarity=card.get("rarity"),
            is_holo=bool(card.get("is_holo")),
        )
        session.add(cc)
        session.flush()
    for f in ENRICH_FIELDS:
        if card.get(f) and not getattr(cc, f):
            setattr(cc, f, card[f])
    return cc


def record_price(session: Session, cc: CatalogCard, patch: dict) -> None:
    """Apply a pricing patch to the catalog card and append a history snapshot."""
    for f in PRICE_FIELDS:
        if f in patch:
            setattr(cc, f, patch[f])
    cc.outlier_flag = bool(patch.get("outlier_flag", cc.outlier_flag))
    cc.last_priced_at = utcnow()
    session.add(PriceSnapshot(
        catalog_card_id=cc.id,
        final_price=patch.get("final_price", cc.final_price),
        pricing_confidence=float(patch.get("pricing_confidence") or 0.0),
        tcgplayer_market=patch.get("tcgplayer_market"),
        cardmarket_trend_usd=patch.get("cardmarket_trend_usd"),
        ebay_median_30d=patch.get("ebay_median_30d"),
    ))


def price_at(session: Session, catalog_card_id: int, at: datetime) -> Optional[float]:
    """Last known price at or before `at` (carried forward)."""
    snap = (
        session.query(PriceSnapshot)
        .filter(
            PriceSnapshot.catalog_card_id == catalog_card_id,
            PriceSnapshot.priced_at <= at,
            PriceSnapshot.final_price.isnot(None),
        )
        .order_by(PriceSnapshot.priced_at.desc())
        .first()
    )
    return snap.final_price if snap else None


def log_event(
    session: Session,
    user_id: int,
    catalog_card_id: int,
    kind: str,
    *,
    channel: str = "manual",
    quantity: int = 1,
    unit_price: Optional[float] = None,
    unit_cost: Optional[float] = None,
    fees: Optional[float] = None,
    condition: Optional[str] = None,
    order_id: Optional[int] = None,
    image_url: Optional[str] = None,
    crop_path: Optional[str] = None,
    note: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> LibraryEvent:
    """Append one row to the library ledger. Caller commits."""
    ev = LibraryEvent(
        user_id=user_id,
        catalog_card_id=catalog_card_id,
        kind=kind,
        channel=channel,
        quantity=max(1, int(quantity)),
        unit_price=unit_price,
        unit_cost=unit_cost,
        fees=fees,
        condition=condition,
        order_id=order_id,
        image_url=image_url,
        crop_path=crop_path,
        note=(note or None),
        occurred_at=occurred_at or utcnow(),
    )
    session.add(ev)
    return ev
