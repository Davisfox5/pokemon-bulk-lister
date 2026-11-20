"""Cardmarket free daily price-guide ingest.

Cardmarket publishes anonymous, TOS-blessed JSON exports ("Anyone can import
and incorporate Cardmarket's product and price data into their own
applications with no extra permission"):

  price guide:   .../productCatalog/priceGuide/price_guide_6.json
  singles list:  .../productCatalog/productList/products_singles_6.json
  (idGame=6 = Pokémon; prices in EUR; updated daily)

This replaces per-card Cardmarket lookups with one cached daily download and,
more importantly, provides a price signal when pokemontcg.io fails (its data
lags new sets and its CDN 404s under load).

Matching caveat: the product list carries no collector numbers and no
expansion names, so lookups are NAME-based across all printings of a name.
Measured on real data, every popular name spans a huge price range (bulk
commons + secret-rare printings), so ratio guards refuse everything. The
rule that works instead:

  - one printing            -> trust it (no ambiguity)
  - all printings agree 3x  -> median (safe at any price level)
  - p25 <= EUR 1 (bulk)     -> return p25 as a deliberately conservative
                               estimate; under-listing a common by cents is
                               harmless, and a single-source fallback price
                               scores 0.5 confidence -> auto-flagged for
                               review by the aggregator anyway
  - otherwise               -> refuse (print-sensitive; needs a real match)

Prices are used internally for aggregation only — publicly *displaying* raw
Cardmarket prices requires written agreement per their TOS.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from lib import price_notes

PRICE_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_6.json"
PRODUCTS_URL = "https://downloads.s3.cardmarket.com/productCatalog/productList/products_singles_6.json"

DEFAULT_CACHE_DIR = "output/cache/cardmarket"
CACHE_TTL_HOURS = float(os.getenv("CARDMARKET_TTL_HOURS", "24"))

# Same-name candidates that all agree within this ratio are trusted outright.
MAX_CANDIDATE_SPREAD = 3.0

# If the cheapest quartile of a name's printings sits at or below this (EUR),
# the card in hand is overwhelmingly likely a bulk printing — use p25 as a
# conservative estimate. Above it, a name-only match is refused.
BULK_CEILING_EUR = float(os.getenv("CARDMARKET_BULK_CEILING_EUR", "1.0"))

_BRACKETS_RE = re.compile(r"\s*[\[(].*?[\])]")
_WS_RE = re.compile(r"\s+")


def _normalize(name: str) -> str:
    """'Kakuna [Bug Bite | Primal Clash]' -> 'kakuna'."""
    name = _BRACKETS_RE.sub("", name or "")
    return _WS_RE.sub(" ", name).strip().lower()


class CardmarketPriceGuide:
    """Lazily-built, thread-safe index over the daily Cardmarket exports."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, timeout: int = 120) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._index: Optional[dict[str, list[tuple[Optional[float], Optional[float]]]]] = None

    # -- data loading ---------------------------------------------------

    def _fetch_cached(self, url: str, filename: str) -> dict:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / filename
        fresh = path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_HOURS * 3600
        if not fresh:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            # Validate before overwriting a good cache with an error page.
            data = resp.json()
            path.write_bytes(resp.content)
            return data
        return json.loads(path.read_text())

    def _build_index(self) -> dict[str, list[tuple[Optional[float], Optional[float]]]]:
        guide = self._fetch_cached(PRICE_GUIDE_URL, "price_guide_6.json")
        products = self._fetch_cached(PRODUCTS_URL, "products_singles_6.json")

        prices: dict[int, tuple[Optional[float], Optional[float]]] = {}
        for entry in guide.get("priceGuides") or []:
            pid = entry.get("idProduct")
            if pid is None:
                continue
            prices[int(pid)] = (entry.get("trend"), entry.get("trend-holo"))

        index: dict[str, list[tuple[Optional[float], Optional[float]]]] = {}
        for product in products.get("products") or []:
            pid = product.get("idProduct")
            if pid is None or int(pid) not in prices:
                continue
            key = _normalize(product.get("name") or "")
            if not key:
                continue
            index.setdefault(key, []).append(prices[int(pid)])
        return index

    def _ensure_index(self) -> dict:
        if self._index is None:
            with self._lock:
                if self._index is None:
                    self._index = self._build_index()
        return self._index

    # For tests: inject data without network.
    def load_from_data(self, guide: dict, products: dict) -> None:
        self._fetch_cached = lambda url, filename: guide if "priceGuide" in url else products  # type: ignore
        self._index = None
        self._ensure_index()

    # -- lookup ---------------------------------------------------------

    def lookup_trend_eur(self, name: str, is_holo: bool = False) -> tuple[Optional[float], str]:
        """Median trend price (EUR) across same-name printings, or (None, reason).

        Prefers the holo trend for holo cards, falling back to non-holo.
        """
        index = self._ensure_index()
        candidates = index.get(_normalize(name))
        if not candidates:
            return None, "no cardmarket name match"

        values: list[float] = []
        for trend, trend_holo in candidates:
            v = (trend_holo if is_holo else trend) or (trend if is_holo else None)
            if v is not None and v > 0:
                values.append(float(v))
        if not values:
            return None, "cardmarket match has no trend prices"

        values.sort()
        n = len(values)
        if n == 1:
            return round(values[0], 2), "cardmarket name-match (single printing)"
        if values[-1] <= MAX_CANDIDATE_SPREAD * values[0]:
            return round(statistics.median(values), 2), f"cardmarket name-match (n={n}, printings agree)"
        p25 = values[n // 4]
        if p25 <= BULK_CEILING_EUR:
            # p25 of the printings: a conservative bulk estimate.
            return round(p25, 2), price_notes.CM_ESTIMATE
        # The printings disagree above bulk range: too ambiguous to use.
        return None, f"cardmarket printings disagree across {n} candidates; name match too ambiguous"
