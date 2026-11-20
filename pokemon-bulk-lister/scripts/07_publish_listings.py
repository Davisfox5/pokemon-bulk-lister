"""Step 7 — publish listings to the three marketplaces.

Two very different mechanisms, because the platforms differ:

  eBay      → official Sell Inventory API. One live listing per card
              (inventory_item → offer → publish). Listing IDs are written back
              into cards_priced.json. Requires a one-time
              `python -m webapp.setup_ebay`.

  TCGPlayer → no public listing API. Uploads the staged-inventory CSV through
  Whatnot     the Seller Portal via a headless browser. Requires a one-time
              `python -m webapp.setup_portal --site <name>`. ⚠️ TOS-grey, fragile.

Examples:
    python scripts/07_publish_listings.py --site ebay
    python scripts/07_publish_listings.py --site tcgplayer --site whatnot
    python scripts/07_publish_listings.py --all --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402


def _publish_ebay(cards: list[dict], cards_path: Path, dry_run: bool) -> int:
    from lib.ebay_lister import EbayLister, EbayListingError
    from lib.ebay_oauth import EbayUserNotAuthorized

    try:
        lister = EbayLister()
    except Exception as exc:
        print(f"[ebay] cannot start: {exc}", file=sys.stderr)
        return 1

    eligible = [c for c in cards if (c.get("price") or c.get("final_price")) and c.get("image_url")]
    skipped = len(cards) - len(eligible)
    print(f"[ebay] {len(eligible)} eligible card(s); {skipped} skipped (no price or image)")

    ok = 0
    for card in eligible:
        title = card.get("name") or Path(card.get("crop_path", "")).stem
        try:
            result = lister.publish_card(card, dry_run=dry_run)
        except EbayUserNotAuthorized as exc:
            print(f"[ebay] {exc}", file=sys.stderr)
            return 1
        except EbayListingError as exc:
            print(f"[ebay] FAILED {title}: {exc}", file=sys.stderr)
            continue
        if dry_run:
            print(f"[ebay] DRY-RUN would list {title} @ ${result['price']:.2f}")
        else:
            card["ebay_listing_id"] = result.get("listing_id")
            card["ebay_offer_id"] = result.get("offer_id")
            card["ebay_listing_url"] = result.get("url")
            print(f"[ebay] listed {title} → {result.get('listing_id')}")
        ok += 1

    if not dry_run and ok:
        cards_path.write_text(json.dumps(cards, indent=2))
        print(f"[ebay] wrote {ok} listing id(s) back to {cards_path}")
    return 0


def _ensure_csv(name: str, rows_fn, cards: list[dict], out_dir: Path) -> Path:
    import csv

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    rows = rows_fn(cards)
    if not rows:
        path.write_text("")
        return path
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[{name}] regenerated with {len(rows)} row(s)")
    return path


def _publish_portal(site: str, csv_path: Path, dry_run: bool) -> int:
    if dry_run:
        print(f"[{site}] DRY-RUN would upload {csv_path}")
        return 0
    if site == "tcgplayer":
        from lib.tcgplayer_lister import TCGPlayerLister as Lister
    else:
        from lib.whatnot_lister import WhatnotLister as Lister
    try:
        with Lister() as lister:
            result = lister.upload_csv(str(csv_path))
    except Exception as exc:
        print(f"[{site}] FAILED: {exc}", file=sys.stderr)
        return 1
    status = "ok" if result.get("ok") else "uncertain"
    print(f"[{site}] upload {status}: {result.get('detail')}")
    return 0 if result.get("ok") else 2


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cards", default="output/cards_priced.json")
    parser.add_argument("--out-dir", default="output/csvs")
    parser.add_argument("--site", action="append", choices=["ebay", "tcgplayer", "whatnot"], default=[])
    parser.add_argument("--all", action="store_true", help="publish to all three sites")
    parser.add_argument("--dry-run", action="store_true", help="build payloads/CSVs but don't push")
    args = parser.parse_args()

    sites = ["ebay", "tcgplayer", "whatnot"] if args.all else args.site
    if not sites:
        parser.error("specify --site SITE (repeatable) or --all")

    cards_path = Path(args.cards)
    if not cards_path.exists():
        print(f"{cards_path} not found — run the pipeline through pricing first", file=sys.stderr)
        return 1
    cards: list[dict[str, Any]] = json.loads(cards_path.read_text())

    # CSV generators for the portal sites (imported from the numbered script).
    import importlib.util

    spec = importlib.util.spec_from_file_location("generate_csvs", ROOT / "scripts" / "05_generate_csvs.py")
    gen = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(gen)

    out_dir = Path(args.out_dir)
    rc = 0
    if "ebay" in sites:
        rc |= _publish_ebay(cards, cards_path, args.dry_run)
    if "tcgplayer" in sites:
        catalog = gen._load_tcg_catalog()
        path = _ensure_csv("tcgplayer_bulk.csv", lambda cs: gen.tcgplayer_rows(cs, catalog), cards, out_dir)
        rc |= _publish_portal("tcgplayer", path, args.dry_run)
    if "whatnot" in sites:
        path = _ensure_csv("whatnot_seller_hub.csv", gen.whatnot_rows, cards, out_dir)
        rc |= _publish_portal("whatnot", path, args.dry_run)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
