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
number, set, condition and printing all agree exactly and one row is left;
everything else is left out and counted, because a wrong Id lists the wrong
card. Expect some real cards to go unmatched (a set this app names
differently from TCGplayer, a holo flag the scan got wrong): those are for a
person to add in TCGplayer, not for this code to guess.

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
    """Card name without a trailing collector number ("Dragapult ex -
    130/167"). Parentheticals stay: "Pikachu (Staff Prerelease)" is a
    different product from "Pikachu"."""
    s = re.sub(r"\s+-\s+\d[\w/]*\s*$", "", text or "")
    return _norm(s)


def _norm_set(text: Optional[str]) -> str:
    """Set name without the code TCGplayer puts in front ("SV06: Twilight
    Masquerade")."""
    return _norm(re.sub(r"^\s*[A-Za-z0-9.]+:\s*", "", text or ""))


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
                "set": _norm_set(raw.get(cols["set"])) if cols["set"] else "",
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
        """The TCGplayer Id for a scanned card, or None.

        Every one of these must agree, exactly, and exactly one row must be
        left: name, collector number, set name, condition, printing (a holo
        card takes the Holofoil row, any other the plain one). A card with
        no set, an export row with no set, a condition this app does not
        know, or a printing the export does not list under that exact name
        all return None. A wrong Id lists the wrong product, so there is no
        "closest" match.
        """
        name, number = _norm_name(card.get("name")), _norm_number(card.get("card_number"))
        card_set = _norm_set(card.get("set_name"))
        base = CONDITIONS.get(str(card.get("condition_guess") or "NM").upper())
        if not name or not number or not card_set or base is None:
            return None
        wanted = f"{base} holofoil" if card.get("is_holo") else base
        rows = [r for r in self._index.get((name, number), [])
                if r["set"] == card_set and r["condition"] == wanted]
        return rows[0]["id"] if len(rows) == 1 else None
