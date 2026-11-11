"""Real TCGplayer Ids, taken from the seller's own TCGplayer export.

TCGplayer's bulk upload identifies each row by its "TCGplayer Id", a number
TCGplayer assigns to one card in one condition and printing. Nothing this app
talks to hands those out: pokemontcg.io's card id (sv6-130), which earlier
versions wrote into that column, is not one, and TCGplayer has no public API
for sellers.

The place a seller can get them is TCGplayer itself: the pricing / inventory
export in the seller portal lists every product with its TCGplayer Id, set,
name, number and condition. This module reads that file and finds the row for
a scanned card, so the upload this app writes carries Ids TCGplayer will
recognise.

Matching is deliberately strict. A row is used only when name, collector
number, set and condition all agree and exactly one printing fits; anything
ambiguous is left out and counted, because a wrong Id lists the wrong card.

The export's column names are the ones TCGplayer's upload template uses
(TCGplayer Id, Product Line, Set Name, Product Name, Number, Condition). They
have not been checked against a real export in this environment; if a file
is missing one, `parse` says which.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from pathlib import Path
from typing import Optional

CONDITIONS = {
    "NM": "near mint", "LP": "lightly played", "MP": "moderately played",
    "HP": "heavily played", "DMG": "damaged",
}
_REQUIRED = {"id": ("tcgplayerid",), "name": ("productname", "name"),
             "number": ("number", "cardnumber"), "condition": ("condition",)}
_OPTIONAL = {"set": ("setname", "set"), "line": ("productline",)}


def _norm(text: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _norm_name(text: Optional[str]) -> str:
    """Card name without the decorations exports add: "Dragapult ex - 130/167",
    "Pikachu (Reverse Holo)"."""
    s = re.sub(r"\s*\(.*?\)", "", text or "")
    s = re.sub(r"\s+-\s+[\w/]+\s*$", "", s)
    return _norm(s)


def _norm_number(text: Optional[str]) -> str:
    s = str(text or "").strip().lower().split("/")[0]
    return s.lstrip("0") or s


def _header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class TcgCatalog:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self._index: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            self._index.setdefault((row["name"], row["number"]), []).append(row)

    def __len__(self) -> int:
        return len(self.rows)

    @classmethod
    def parse(cls, text: str) -> "TcgCatalog":
        reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
        headers = reader.fieldnames or []
        by_norm = {_header(h): h for h in headers}
        cols: dict[str, Optional[str]] = {}
        for field, names in {**_REQUIRED, **_OPTIONAL}.items():
            cols[field] = next((by_norm[n] for n in names if n in by_norm), None)
        missing = [f for f in _REQUIRED if not cols[f]]
        if missing:
            wanted = {"id": "TCGplayer Id", "name": "Product Name", "number": "Number",
                      "condition": "Condition"}
            raise ValueError(
                "this does not look like a TCGplayer pricing export: no "
                + ", ".join(wanted[m] for m in missing) + " column. Columns found: "
                + (", ".join(headers) or "none"))
        rows = []
        for raw in reader:
            tcg_id = (raw.get(cols["id"]) or "").strip()
            if not tcg_id:
                continue
            if cols["line"] and "pokemon" not in _norm(raw.get(cols["line"])):
                continue
            rows.append({
                "id": tcg_id,
                "name": _norm_name(raw.get(cols["name"])),
                "number": _norm_number(raw.get(cols["number"])),
                "set": _norm(raw.get(cols["set"])) if cols["set"] else "",
                "condition": _norm(raw.get(cols["condition"])),
            })
        if not rows:
            raise ValueError("no Pokemon rows with a TCGplayer Id were found in that file")
        return cls(rows)

    @classmethod
    def load(cls, path) -> Optional["TcgCatalog"]:
        try:
            return cls.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None

    def match(self, card: dict) -> Optional[str]:
        """The TCGplayer Id for a scanned card, or None when no row, or more
        than one, fits."""
        name, number = _norm_name(card.get("name")), _norm_number(card.get("card_number"))
        if not name or not number:
            return None
        rows = self._index.get((name, number), [])
        card_set = _norm(card.get("set_name"))
        if card_set:
            # Exports prefix the set ("SV06: Twilight Masquerade"), so compare loosely.
            rows = [r for r in rows if not r["set"] or card_set in r["set"] or r["set"] in card_set]
        base = CONDITIONS.get(str(card.get("condition_guess") or "NM").upper(), "near mint")
        rows = [r for r in rows if r["condition"].startswith(base)]
        if not rows:
            return None

        def printing(row: dict) -> str:
            return row["condition"][len(base):].strip()

        want_holo = bool(card.get("is_holo"))
        exact = [r for r in rows if printing(r) == ("holofoil" if want_holo else "")]
        if len(exact) == 1:
            return exact[0]["id"]
        if len(exact) > 1:
            return None
        # The card's printing is not on offer under that exact name. One
        # remaining candidate is unambiguous (many cards exist only as
        # holofoil); several are not.
        return rows[0]["id"] if len(rows) == 1 else None
