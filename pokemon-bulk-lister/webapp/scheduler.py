"""Background price-refresh scheduler for the catalog.

Cost model: prices attach to catalog cards (shared across users), and refresh
cadence is tiered by card value — a $200 Charizard moves in hours, a $0.25
common doesn't move in a week. Each cycle refreshes at most
PRICE_REFRESH_BATCH cards (most valuable, most stale first), so external API
spend has a hard per-cycle ceiling regardless of catalog size.

The actual pricing function is injected from app.py (`start(price_fn=...)`)
so this module never duplicates the source-aggregation logic and there is no
circular import.

Env knobs:
  PRICE_REFRESH_ENABLED   default 1; 0 disables the loop entirely
  PRICE_REFRESH_INTERVAL  seconds between cycles (default 900)
  PRICE_REFRESH_BATCH     max cards refreshed per cycle (default 25)
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime, timedelta
from typing import Callable, Optional

from webapp import alerts_engine
from webapp.models import CatalogCard, get_session, record_price, utcnow

# (min final_price, refresh interval) — first match wins, ordered high→low.
# Unpriced cards (final_price None) are treated as hot: they need a first price.
TIERS = [
    (20.0, timedelta(hours=6)),
    (5.0, timedelta(hours=24)),
    (0.0, timedelta(days=7)),
]

_state: dict = {
    "enabled": True,
    "running_cycle": False,
    "last_cycle_at": None,
    "last_refreshed": 0,
    "last_error": None,
    "total_refreshed": 0,
}
_price_fn: Optional[Callable[[dict], dict]] = None
_cycle_lock = threading.Lock()


def _interval_for(price: Optional[float]) -> timedelta:
    if price is None:
        return timedelta(0)  # never priced — always due
    for floor, interval in TIERS:
        if price >= floor:
            return interval
    return TIERS[-1][1]


def _is_due(cc: CatalogCard, now: datetime) -> bool:
    if cc.last_priced_at is None:
        return True
    return now - cc.last_priced_at >= _interval_for(cc.final_price)


def _card_dict(cc: CatalogCard) -> dict:
    return {
        "name": cc.name,
        "set_name": cc.set_name,
        "set_code": cc.set_code,
        "card_number": cc.card_number,
        "is_holo": cc.is_holo,
    }


def refresh_cards(card_ids: Optional[list[int]] = None, batch_size: Optional[int] = None) -> int:
    """Refresh due catalog cards (or the given ids unconditionally). Serialized."""
    if _price_fn is None:
        return 0
    batch = batch_size or int(os.getenv("PRICE_REFRESH_BATCH", "25"))

    with _cycle_lock:
        _state["running_cycle"] = True
        s = get_session()
        refreshed = 0
        try:
            now = utcnow()
            q = s.query(CatalogCard)
            if card_ids is not None:
                targets = q.filter(CatalogCard.id.in_(card_ids)).all()
            else:
                targets = [c for c in q.all() if _is_due(c, now)]
                # Most valuable + most stale first; cap the batch.
                targets.sort(
                    key=lambda c: (-(c.final_price or 1e9),
                                   c.last_priced_at or datetime(1970, 1, 1)),
                )
            for cc in targets[:batch]:
                try:
                    patch = _price_fn(_card_dict(cc))
                except Exception as exc:
                    print(f"[scheduler] pricing {cc.name}: {exc}", file=sys.stderr)
                    continue
                if patch.get("final_price") is None and cc.final_price is not None:
                    # Nothing found this time. Keep the price on file (it may
                    # be the owner's own, from their spreadsheet) instead of
                    # replacing it with nothing.
                    continue
                record_price(s, cc, patch)
                s.commit()
                refreshed += 1

            try:
                alerts_engine.evaluate_all(s)
                s.commit()
            except Exception:
                s.rollback()
                traceback.print_exc()

            _state.update(
                last_cycle_at=utcnow().isoformat() + "Z",
                last_refreshed=refreshed,
                last_error=None,
            )
            _state["total_refreshed"] += refreshed
        except Exception as exc:
            _state["last_error"] = str(exc)
            traceback.print_exc()
        finally:
            s.close()
            _state["running_cycle"] = False
        return refreshed


def install(price_fn: Callable[[dict], dict]) -> None:
    """Install the pricing callback without a timer. For hosts where a
    scheduled request calls refresh_cards instead."""
    global _price_fn
    _price_fn = price_fn


def start(price_fn: Callable[[dict], dict]) -> None:
    """Install the pricing callback and launch the refresh loop (daemon)."""
    install(price_fn)

    if os.getenv("PRICE_REFRESH_ENABLED", "1") == "0":
        _state["enabled"] = False
        print("[scheduler] price refresh disabled (PRICE_REFRESH_ENABLED=0)", file=sys.stderr)
        return

    interval = int(os.getenv("PRICE_REFRESH_INTERVAL", "900"))

    def loop():
        import time
        while True:
            time.sleep(interval)
            try:
                refresh_cards()
            except Exception:
                traceback.print_exc()

    threading.Thread(target=loop, daemon=True, name="price-refresh").start()
    print(f"[scheduler] price refresh loop started (every {interval}s)", file=sys.stderr)


def status() -> dict:
    return dict(_state)
