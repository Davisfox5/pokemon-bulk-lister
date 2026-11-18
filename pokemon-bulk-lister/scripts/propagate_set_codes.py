"""Cross-sheet set-code propagation — UNANIMITY rule.

Same name does NOT mean same printing. A Charizard from Base Set ($1000)
and a Charizard from Pokémon GO ($1) share a name but are functionally
different cards. Naive majority-vote propagation can move cards from a
correct match to a wrong one (real example: this very script previously
moved three correctly-priced common Fuecocos onto a sv8 set_code with
a Cardmarket variant mismatch, inflating their price 100x).

Safe rule for this pass:
  - For each name that appears multiple times, collect set_codes from
    instances where id_confidence ≥ 0.5.
  - If ALL of those confident instances share the SAME set_code,
    propagate it to instances missing one.
  - If there's ANY disagreement (two different set_codes), refuse to
    propagate and flag the unknowns for manual review.

Run:
    python scripts/propagate_set_codes.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter


def propagate(db_path: str = "output/db.sqlite", dry_run: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    cards_by_name: dict[str, list[dict]] = {}
    for cid, name, sc, num, conf in conn.execute(
        "SELECT id, name, set_code, card_number, id_confidence FROM cards "
        "WHERE name IS NOT NULL AND name <> '' ORDER BY id"
    ):
        cards_by_name.setdefault(name, []).append(
            {"id": cid, "set_code": sc, "card_number": num, "id_confidence": conf or 0}
        )

    propagated = 0
    refused = 0
    for name, cards in cards_by_name.items():
        if len(cards) < 2:
            continue
        confident = [c for c in cards if c["set_code"] and c["id_confidence"] >= 0.5]
        if not confident:
            continue

        codes = {c["set_code"] for c in confident}
        if len(codes) > 1:
            # Multiple printings exist across confident matches — silently skip.
            # The unknowns get re-examined in the identification pass, not
            # punted to a "needs_review" pile.
            missing = [c for c in cards if not c["set_code"]]
            if missing:
                refused += len(missing)
                code_list = ", ".join(sorted(codes))
                print(f"  skip    {name:<25} (sets seen: {code_list}); "
                      f"{len(missing)} entries need re-examination, not propagating")
            continue

        # Unanimous — safe to propagate.
        only_code = codes.pop()
        missing = [c for c in cards if not c["set_code"]]
        for c in missing:
            print(f"  PROPAGATE {name:<23} id={c['id']:<4} <- set_code={only_code}")
            if not dry_run:
                conn.execute(
                    "UPDATE cards SET set_code = ?, "
                    "id_confidence = MAX(?, COALESCE(id_confidence, 0)), "
                    "pricing_notes = COALESCE(NULLIF(pricing_notes,''),'') || "
                    "'; Set filled in from matching cards.', "
                    "updated_at = datetime('now') WHERE id = ?",
                    (only_code, 0.6, c["id"]),
                )
            propagated += 1

    if not dry_run:
        conn.commit()
    print(f"\npropagated {propagated} card(s), refused {refused} ambiguous case(s)")
    return propagated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, don't write")
    args = parser.parse_args()
    propagate(dry_run=args.dry_run)
