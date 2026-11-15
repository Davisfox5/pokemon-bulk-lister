"""Step 3 — enrich cards.json with prices from pokemontcg.io (TCGPlayer + Cardmarket),
eBay 30-day sold stats, and (optionally) Terapeak 365-day sold stats via headless browser.

Reads output/cards.json, looks up each card, applies the aggregation rule, and
writes output/cards_priced.json.

Skips entries whose `name` is empty (i.e. step 2 hasn't filled them in yet).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.ebay_client import EbayAuthError, EbayClient  # noqa: E402
from lib.pricing import aggregate  # noqa: E402
from lib.tcgplayer_client import TCGPlayerClient  # noqa: E402


def enrich_one(
    entry: dict,
    tcg: TCGPlayerClient,
    ebay: EbayClient,
    eur_usd_rate: float,
    terapeak: Optional["TerapeakClient"] = None,
) -> dict:
    name = entry.get("name", "").strip()
    if not name:
        return {**entry, "_skipped": "no name (run step 2 first)"}

    set_name = entry.get("set_name") or None
    set_code = entry.get("set_code") or None
    card_number = entry.get("card_number") or None
    from lib.printing import of as card_printing
    printing = card_printing(entry)
    is_holo = printing == "holofoil"

    # One pokemontcg.io call -> TCGPlayer (USD) + Cardmarket (EUR).
    tcg_price: Optional[float] = None
    cm_eur: Optional[float] = None
    tcg_card: Optional[dict] = None
    try:
        prices = tcg.lookup_prices(
            name=name,
            set_name=set_name,
            set_code=set_code,
            card_number=card_number,
            is_holo=is_holo,
            printing=printing,
        )
        tcg_price = prices["tcgplayer_market_usd"]
        cm_eur = prices["cardmarket_trend_eur"]
        tcg_card = prices["card"]
    except Exception as exc:
        print(f"  [tcg/cm error] {name}: {exc}", file=sys.stderr)

    cm_usd: Optional[float] = round(cm_eur * eur_usd_rate, 2) if cm_eur else None

    ebay_stats: dict[str, Any] = {"median": None, "max": None, "count": 0}
    try:
        query = ebay.build_query(name=name, set_name=set_name, card_number=card_number)
        if printing == "normal":
            ebay_stats = ebay.sold_stats(query=query, days=30, condition="NEW")
    except EbayAuthError as exc:
        print(f"  [ebay auth] {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"  [ebay error] {name}: {exc}", file=sys.stderr)

    terapeak_stats: dict[str, Any] = {"median": None, "count": 0}
    if terapeak is not None and printing == "normal":
        try:
            tp_query = " ".join(p for p in [name, set_name, card_number, "pokemon"] if p)
            terapeak_stats = terapeak.search(tp_query, days=365)
        except Exception as exc:
            print(f"  [terapeak error] {name}: {exc}", file=sys.stderr)

    result = aggregate(
        tcgplayer_market=tcg_price,
        ebay_median_30d=ebay_stats.get("median"),
        ebay_max_30d=ebay_stats.get("max"),
        cardmarket_trend_usd=cm_usd,
        terapeak_median_usd=terapeak_stats.get("median"),
    )

    enriched = dict(entry)
    enriched["price"] = result.price
    enriched["sources"] = result.sources
    enriched["confidence"] = result.confidence
    enriched["outlier_flag"] = result.outlier_flag
    enriched["needs_review"] = result.needs_review
    enriched["pricing_notes"] = result.notes
    enriched["ebay_sold_count_30d"] = ebay_stats.get("count", 0)
    enriched["terapeak_sold_count_365d"] = terapeak_stats.get("count", 0)
    enriched["cardmarket_trend_eur"] = cm_eur
    if tcg_card is not None:
        enriched["tcgplayer_product_id"] = tcg_card.get("id")
        enriched["tcgplayer_url"] = (tcg_card.get("tcgplayer") or {}).get("url")
        enriched["cardmarket_url"] = (tcg_card.get("cardmarket") or {}).get("url")
    return enriched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default="output/cards.json")
    parser.add_argument("--out", default="output/cards_priced.json")
    parser.add_argument(
        "--terapeak",
        action="store_true",
        help="enable Terapeak headless scraping for 365-day sold history",
    )
    args = parser.parse_args()

    load_dotenv()

    eur_usd_rate = float(os.getenv("EUR_USD_RATE", "1.08"))

    cards_path = Path(args.cards)
    out_path = Path(args.out)
    if not cards_path.exists():
        print(f"{cards_path} not found — run step 2 first", file=sys.stderr)
        return 1

    with cards_path.open() as f:
        entries = json.load(f)

    tcg = TCGPlayerClient()
    ebay = EbayClient()

    terapeak = None
    if args.terapeak or os.getenv("ENABLE_TERAPEAK") == "1":
        from lib.terapeak_client import TerapeakClient  # local import: optional dep
        terapeak = TerapeakClient()
        print(f"Terapeak headless scraping enabled (EUR_USD_RATE={eur_usd_rate})")

    enriched: list[dict] = []
    try:
        for i, entry in enumerate(entries, 1):
            name = entry.get("name", "<unidentified>")
            print(f"[{i}/{len(entries)}] {name}")
            enriched.append(enrich_one(entry, tcg, ebay, eur_usd_rate, terapeak))
    finally:
        if terapeak is not None:
            terapeak.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(enriched, f, indent=2)

    review = sum(1 for e in enriched if e.get("needs_review"))
    print(f"\nWrote {out_path}: {len(enriched)} cards, {review} flagged for review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
