"""Step 5: generate marketplace upload CSVs from cards_priced.json.

Outputs:
    output/csvs/tcgplayer_bulk.csv     TCGPlayer staged-inventory bulk format
    output/csvs/whatnot_seller_hub.csv Whatnot US Seller Hub inventory import
    output/csvs/ebay_bulk.csv          eBay Seller Hub bulk listing CSV

Column orders match each platform's published template at time of writing
(2026-05). Verify against the latest template before uploading, these are
documented as moving targets.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


# Listing text follows the "marketplace listing" row in the df-writing skill
# (.claude/skills/df-writing): searchable words first, condition from the data,
# no hype, no emoji, no em-dashes. These strings go on a live marketplace, so a
# condition or rarity claim here is a factual claim about the item.


def _condition_full(short: str) -> str:
    return {
        "NM": "Near Mint",
        "LP": "Lightly Played",
        "MP": "Moderately Played",
        "HP": "Heavily Played",
        "DMG": "Damaged",
    }.get(short.upper(), "Near Mint")


def _title(card: dict) -> str:
    parts = [card.get("name", "")]
    number = card.get("card_number")
    set_name = card.get("set_name")
    if number:
        parts.append(f"#{number}")
    if set_name:
        parts.append(set_name)
    if card.get("is_holo"):
        parts.append("Holo")
    parts.append("Pokemon TCG")
    return " ".join(p for p in parts if p)


def tcgplayer_rows(cards: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for c in cards:
        if not c.get("price") or not c.get("tcgplayer_product_id"):
            continue
        rows.append(
            {
                "TCGplayer Id": c["tcgplayer_product_id"],
                "Product Line": "Pokemon",
                "Set Name": c.get("set_name", ""),
                "Product Name": c.get("name", ""),
                "Number": c.get("card_number", ""),
                "Rarity": c.get("rarity", ""),
                "Condition": _condition_full(c.get("condition_guess", "NM")),
                "TCG Marketplace Price": f"{c['price']:.2f}",
                "Add to Quantity": 1,
            }
        )
    return rows


def whatnot_rows(cards: list[dict]) -> list[dict]:
    """Whatnot Seller Hub inventory import template (US).

    Reference columns based on Whatnot's current bulk-inventory CSV template.
    """
    rows: list[dict] = []
    for c in cards:
        if not c.get("price"):
            continue
        rows.append(
            {
                "Category": "Trading Cards",
                "Sub Category": "Pokemon",
                "Title": _title(c),
                "Description": _description(c),
                "Quantity": 1,
                "Type": "Buy it Now",
                "Price": f"{c['price']:.2f}",
                "Shipping Profile": "0-1 oz",
                "Offerable": "TRUE",
                "Hazmat": "Not Hazmat",
                "Condition": _condition_full(c.get("condition_guess", "NM")),
                "Cost Per Item": "",
                "SKU": Path(c.get("crop_path", "")).stem,
                "Image URL 1": c.get("image_url", ""),
            }
        )
    return rows


def _ebay_package(c: dict) -> dict:
    if c.get("is_bulk"):
        oz = int(c.get("bulk_weight_oz", 8))
        return {
            "Weight major": str(oz // 16),
            "Weight minor": str(oz % 16),
            "Package type": "Package",
        }
    return {
        "Weight major": "0",
        "Weight minor": "1",
        "Package type": "PackageThickEnvelope",
    }


def ebay_rows(cards: list[dict]) -> list[dict]:
    """eBay Seller Hub bulk CSV (Action / Custom label / Category ID / ...).

    Category 183454 = Collectible Card Games > Pokémon TCG > Individual Cards.
    """
    rows: list[dict] = []
    for c in cards:
        if not c.get("price"):
            continue
        rows.append(
            {
                "Action": "Add",
                "Custom label (SKU)": Path(c.get("crop_path", "")).stem,
                "Category ID": "183454",
                "Title": _title(c)[:80],
                "Condition ID": "1000",  # New / NM ungraded
                "Format": "FixedPrice",
                "Duration": "GTC",
                "Start price": f"{c['price']:.2f}",
                "Quantity": 1,
                "Item photo URL": c.get("image_url", ""),
                "Description": _description(c),
                "C:Game": "Pokémon TCG",
                "C:Card Name": c.get("name", ""),
                "C:Set": c.get("set_name", ""),
                "C:Card Number": c.get("card_number", ""),
                "C:Rarity": c.get("rarity", ""),
                "C:Features": "Holo" if c.get("is_holo") else "",
                "C:Language": "English",
                "C:Country/Region of Manufacture": "United States",
                "Shipping type": "Calculated",
                "Shipping service 1 option": "USPSGroundAdvantage",
                **_ebay_package(c),
                "Dispatch time max": "1",
                "Returns accepted option": "ReturnsAccepted",
                "Returns within option": "Days_30",
                "Refund option": "MoneyBack",
                "Return shipping cost paid by": "Buyer",
            }
        )
    return rows


def _lot_title(cards: list[dict], bundle: dict) -> str:
    if bundle.get("title"):
        return str(bundle["title"])
    n = len(cards)
    sets = sorted({c.get("set_name") for c in cards if c.get("set_name")})
    if len(sets) == 1:
        return f"Pokemon TCG Lot: {n} cards from {sets[0]}"
    return f"Pokemon TCG Lot: {n} cards (mixed sets)"


def _lot_description(cards: list[dict], bundle: dict) -> str:
    n = len(cards)
    sets = sorted({c.get("set_name") for c in cards if c.get("set_name") if c.get("set_name")})
    holo_count = sum(1 for c in cards if c.get("is_holo"))
    rarities = sorted({c.get("rarity") for c in cards if c.get("rarity")})

    lines = [f"Bulk lot of {n} Pokemon TCG cards."]
    if sets:
        lines.append(
            "Sets: " + ", ".join(sets[:8])
            + (f", and {len(sets) - 8} more" if len(sets) > 8 else "")
        )
    if rarities:
        lines.append("Rarities included: " + ", ".join(rarities))
    if holo_count:
        lines.append(f"Includes {holo_count} holo card(s).")
    if bundle.get("note"):
        lines.append(str(bundle["note"]))

    lines.append("")
    lines.append("Contents:")
    for c in cards:
        bits = [c.get("name") or "Unidentified"]
        if c.get("card_number"):
            bits.append(f"#{c['card_number']}")
        if c.get("set_name"):
            bits.append(c["set_name"])
        if c.get("is_holo"):
            bits.append("Holo")
        cond = c.get("condition_guess")
        if cond:
            bits.append(f"({_condition_full(cond)})")
        lines.append("• " + " ".join(bits))

    lines.append("")
    lines.append("English. Ships boxed with cards sleeved together to keep shipping low.")
    return "\n".join(lines)


def _lot_condition(cards: list[dict]) -> str:
    """Worst condition wins so we don't oversell the lot."""
    order = ["NM", "LP", "MP", "HP", "DMG"]
    worst_idx = 0
    for c in cards:
        cg = (c.get("condition_guess") or "NM").upper()
        if cg in order:
            worst_idx = max(worst_idx, order.index(cg))
    return _condition_full(order[worst_idx])


def _lot_weight_oz(cards: list[dict]) -> int:
    """Rough lot weight: ~0.06 oz per card + 4 oz of box/padding, min 6 oz."""
    return max(6, int(round(len(cards) * 0.06 + 4)))


def whatnot_lot_row(cards: list[dict], bundle: dict) -> dict:
    price = float(bundle.get("price") or 0.0)
    quantity = int(bundle.get("quantity") or 1)
    sku = bundle.get("sku") or f"lot-{len(cards)}"
    image_url = bundle.get("image_url") or next((c.get("image_url") for c in cards if c.get("image_url")), "")
    return {
        "Category": "Trading Cards",
        "Sub Category": "Pokemon",
        "Title": _lot_title(cards, bundle),
        "Description": _lot_description(cards, bundle),
        "Quantity": quantity,
        "Type": "Buy it Now",
        "Price": f"{price:.2f}",
        "Shipping Profile": "4-8 oz" if len(cards) <= 30 else "8-16 oz",
        "Offerable": "TRUE",
        "Hazmat": "Not Hazmat",
        "Condition": _lot_condition(cards),
        "Cost Per Item": "",
        "SKU": sku,
        "Image URL 1": image_url,
    }


def ebay_lot_row(cards: list[dict], bundle: dict) -> dict:
    price = float(bundle.get("price") or 0.0)
    quantity = int(bundle.get("quantity") or 1)
    sku = bundle.get("sku") or f"lot-{len(cards)}"
    image_url = bundle.get("image_url") or next((c.get("image_url") for c in cards if c.get("image_url")), "")
    oz = _lot_weight_oz(cards)
    # eBay category 183469 = Collectible Card Games > Pokémon TCG > Mixed Card Lots.
    return {
        "Action": "Add",
        "Custom label (SKU)": sku,
        "Category ID": "183469",
        "Title": _lot_title(cards, bundle)[:80],
        "Condition ID": "3000",  # Used / mixed lots default to Used
        "Format": "FixedPrice",
        "Duration": "GTC",
        "Start price": f"{price:.2f}",
        "Quantity": quantity,
        "Item photo URL": image_url,
        "Description": _lot_description(cards, bundle),
        "C:Game": "Pokémon TCG",
        "C:Set": ", ".join(sorted({c.get("set_name", "") for c in cards if c.get("set_name")}))[:65],
        "C:Features": "Holo Included" if any(c.get("is_holo") for c in cards) else "",
        "C:Language": "English",
        "C:Country/Region of Manufacture": "United States",
        "Shipping type": "Calculated",
        "Shipping service 1 option": "USPSGroundAdvantage",
        "Weight major": str(oz // 16),
        "Weight minor": str(oz % 16),
        "Package type": "Package",
        "Dispatch time max": "1",
        "Returns accepted option": "ReturnsAccepted",
        "Returns within option": "Days_30",
        "Refund option": "MoneyBack",
        "Return shipping cost paid by": "Buyer",
    }


def tcgplayer_lot_row(cards: list[dict], bundle: dict) -> dict:
    """TCGPlayer's bulk staged-inventory CSV is single-product only.

    There's no native lot format in that template, so we emit a single
    informational row the user can hand-edit before uploading (or skip).
    """
    price = float(bundle.get("price") or 0.0)
    return {
        "TCGplayer Id": "",
        "Product Line": "Pokemon",
        "Set Name": "Mixed Lot",
        "Product Name": _lot_title(cards, bundle),
        "Number": "",
        "Rarity": "",
        "Condition": _lot_condition(cards),
        "TCG Marketplace Price": f"{price:.2f}",
        "Add to Quantity": int(bundle.get("quantity") or 1),
        "Notes": "Lot: TCGplayer Id required; this row needs manual entry before upload.",
    }


def _description(c: dict) -> str:
    bits: list[str] = []
    name = c.get("name", "")
    if name:
        bits.append(name)
    if c.get("set_name"):
        bits.append(f"from {c['set_name']}")
    if c.get("card_number"):
        bits.append(f"(#{c['card_number']})")
    if c.get("rarity"):
        bits.append(str(c["rarity"]))
    if c.get("is_holo"):
        bits.append("Holo")
    bits.append(f"in {_condition_full(c.get('condition_guess', 'NM'))} condition.")
    if c.get("is_bulk"):
        bits.append("English. Bulk lot, ships boxed, no sleeves.")
    else:
        bits.append("English. Ships in a hard case.")
    return " ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default="output/cards_priced.json")
    parser.add_argument("--out-dir", default="output/csvs")
    args = parser.parse_args()

    cards_path = Path(args.cards)
    if not cards_path.exists():
        print(f"{cards_path} not found", file=sys.stderr)
        return 1

    with cards_path.open() as f:
        cards: list[dict[str, Any]] = json.load(f)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        ("tcgplayer_bulk.csv", tcgplayer_rows(cards)),
        ("whatnot_seller_hub.csv", whatnot_rows(cards)),
        ("ebay_bulk.csv", ebay_rows(cards)),
    ]
    for filename, rows in targets:
        df = pd.DataFrame(rows)
        path = out_dir / filename
        df.to_csv(path, index=False)
        print(f"{path}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
