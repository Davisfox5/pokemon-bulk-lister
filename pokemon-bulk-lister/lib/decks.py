"""Deck logic: decklist parsing, library matching, popular-deck aggregation.

Pure functions plus one small HTTP client. No Flask, no database, so the whole
module is unit-testable.

Decklists use the Pokemon TCG Live export shape, which is also what Limitless
serves: a count, a card name, a set code (e.g. TWM) and a collector number.
The library stores pokemontcg.io set ids (e.g. sv6), so `SetCodes` translates
between the two using data/set_codes.json.

Popular decks come from Limitless (play.limitlesstcg.com/api): the largest
recent tournaments in a format, grouped by archetype. The representative list
for an archetype is the best-placing player's actual 60 cards, and the source
(tournament, date, player, placing) travels with it so the UI can show where
every list came from.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote_plus

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SET_CODES_PATH = DATA_DIR / "set_codes.json"
BUNDLED_POPULAR_PATH = DATA_DIR / "popular_decks.json"

LIMITLESS_API = "https://play.limitlesstcg.com/api"

SECTIONS = ("pokemon", "trainer", "energy")
BASIC_ENERGY_TYPES = (
    "grass", "fire", "water", "lightning", "psychic",
    "fighting", "darkness", "metal", "fairy",
)
# Pokemon TCG Live writes basic energy as "Basic {R} Energy".
_ENERGY_SYMBOLS = {
    "g": "grass", "r": "fire", "w": "water", "l": "lightning", "p": "psychic",
    "f": "fighting", "d": "darkness", "m": "metal", "y": "fairy",
}


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------

def norm_name(name: Optional[str]) -> str:
    """Comparable card name: accents, case, apostrophes and the three ways of
    writing a basic energy all collapse to one form."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("’", "'").replace("‘", "'").lower()
    s = re.sub(r"\s+", " ", s).strip()
    m = re.fullmatch(r"basic \{(\w)\} energy", s)
    if m and m.group(1) in _ENERGY_SYMBOLS:
        return f"{_ENERGY_SYMBOLS[m.group(1)]} energy"
    m = re.fullmatch(r"basic (\w+) energy", s)
    if m and m.group(1) in BASIC_ENERGY_TYPES:
        return f"{m.group(1)} energy"
    return s


def is_basic_energy(name: Optional[str]) -> bool:
    n = norm_name(name)
    return n.endswith(" energy") and n[: -len(" energy")] in BASIC_ENERGY_TYPES


def norm_number(number: Optional[str]) -> str:
    """Collector number without leading zeros or a '/total' suffix."""
    s = str(number or "").strip().lower().split("/")[0]
    return s.lstrip("0") or s


# ----------------------------------------------------------------------
# Set codes
# ----------------------------------------------------------------------

class SetCodes:
    """Pokemon TCG Live set code (TWM) <-> pokemontcg.io set id (sv6)."""

    def __init__(self, mapping: Optional[dict[str, dict]] = None) -> None:
        self._by_code = {k.upper(): v for k, v in (mapping or {}).items()}
        self._by_id: dict[str, str] = {}
        for code, entry in self._by_code.items():
            for set_id in entry.get("ids") or []:
                self._by_id.setdefault(set_id.lower(), code)

    @classmethod
    def load(cls, path: Path = SET_CODES_PATH) -> "SetCodes":
        try:
            return cls(json.loads(Path(path).read_text()).get("sets") or {})
        except (OSError, ValueError):
            return cls({})

    def ids(self, code: Optional[str]) -> list[str]:
        return list((self._by_code.get((code or "").upper()) or {}).get("ids") or [])

    def name(self, code: Optional[str]) -> Optional[str]:
        return (self._by_code.get((code or "").upper()) or {}).get("name")

    def code_for(self, set_id: Optional[str]) -> Optional[str]:
        return self._by_id.get((set_id or "").lower())

    def same_set(self, deck_code: Optional[str], set_code: Optional[str],
                 set_name: Optional[str] = None) -> bool:
        """Does a library card's set (pokemontcg.io id and/or name) match a
        decklist set code?"""
        code = (deck_code or "").upper()
        if not code:
            return False
        sc = (set_code or "").strip().lower()
        if sc and (sc == code.lower() or sc in self.ids(code)):
            return True
        known = self.name(code)
        return bool(known and set_name and norm_name(known) == norm_name(set_name))


# ----------------------------------------------------------------------
# Decklist parsing
# ----------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^(pok[eé]mon|trainers?|energy|energies)\b\s*[:\-]?\s*\(?\d*\)?\s*$", re.I
)
_SECTION_KEY = {"pok": "pokemon", "tra": "trainer", "ene": "energy"}
# "4 Dragapult ex TWM 130" (set code is upper-case, 2-6 chars, may hold digits
# or a dash: 30C, PR-SV). An optional trailing "PH" marks a parallel foil.
_CARD_RE = re.compile(
    r"^(\d+)x?\s+(.+?)\s+([A-Z0-9][A-Z0-9-]{1,5})\s+([A-Za-z]{0,3}\d+[a-z]?)(?:\s+PH)?$"
)
_NAME_ONLY_RE = re.compile(r"^(\d+)x?\s+(.+)$")


def parse_decklist(text: str) -> dict[str, Any]:
    """Parse a pasted decklist. Returns {"cards": [...], "total": int,
    "warnings": [...]}; each card is {section, count, name, set, number}."""
    cards: list[dict] = []
    warnings: list[str] = []
    section: Optional[str] = None

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("total cards"):
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = _SECTION_KEY[unicodedata.normalize("NFKD", m.group(1))[:3].lower()]
            continue
        m = _CARD_RE.match(line)
        if m and any(ch.isalpha() for ch in m.group(3)):
            count, name, set_code, number = m.groups()
        else:
            m = _NAME_ONLY_RE.match(line)
            if not m:
                warnings.append(f"skipped line: {line[:60]}")
                continue
            count, name = m.groups()
            set_code = number = None
        count_i = int(count)
        if count_i <= 0 or count_i > 60:
            warnings.append(f"skipped line (count {count_i}): {line[:60]}")
            continue
        name = name.strip()
        sec = section
        if sec is None:
            sec = "energy" if is_basic_energy(name) else "pokemon"
        cards.append({
            "section": sec,
            "count": count_i,
            "name": name,
            "set": set_code,
            "number": number,
        })

    total = sum(c["count"] for c in cards)
    if cards and total != 60:
        warnings.append(f"list has {total} cards, a legal deck has 60")
    return {"cards": cards, "total": total, "warnings": warnings}


def format_decklist(cards: Iterable[dict], only_missing: bool = False) -> str:
    """Render cards back to pasteable text. With only_missing, counts are the
    `missing` field of a matched deck (a shopping list)."""
    lines: list[str] = []
    for c in cards:
        n = c.get("missing", 0) if only_missing else c.get("count", 0)
        if not n:
            continue
        bits = [str(n), c["name"]]
        if c.get("set") and c.get("number"):
            bits += [c["set"], str(c["number"])]
        lines.append(" ".join(bits))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Matching a deck against a library
# ----------------------------------------------------------------------

def match_deck(cards: list[dict], owned: list[dict],
               set_codes: Optional[SetCodes] = None) -> dict[str, Any]:
    """Work out which deck cards the library covers.

    owned: [{name, set_code, set_name, card_number, quantity}, ...]

    A library copy is spent once, so two deck lines for the same card cannot
    both claim it. Per deck line:
      have_exact  copies of this exact printing
      have_other  copies of another printing that is safe to play instead:
                  any printing of a Trainer or Energy, or the same Pokemon
                  name from the same set (an alternate art)
      maybe       copies of a same-named Pokemon from a different set. Same
                  name does not mean same card for Pokemon, so these do not
                  count toward the deck; the UI asks the user to check.
      missing     count - have_exact - have_other
    """
    sc = set_codes or SetCodes()
    pool: dict[str, list[dict]] = defaultdict(list)
    for o in owned:
        qty = int(o.get("quantity") or 0)
        if qty > 0 and o.get("name"):
            pool[norm_name(o["name"])].append({**o, "_left": qty})

    def is_exact(card: dict, o: dict) -> bool:
        return bool(
            card.get("set") and card.get("number")
            and norm_number(o.get("card_number")) == norm_number(card["number"])
            and sc.same_set(card["set"], o.get("set_code"), o.get("set_name"))
        )

    def take(entries: list[dict], need: int) -> int:
        got = 0
        for o in entries:
            if need - got <= 0:
                break
            n = min(o["_left"], need - got)
            o["_left"] -= n
            got += n
        return got

    out = [dict(c, have_exact=0, have_other=0, maybe=0) for c in cards]

    # Pass 1: exact printings claim their copies first.
    for c in out:
        entries = [o for o in pool.get(norm_name(c["name"]), ()) if is_exact(c, o)]
        c["have_exact"] = take(entries, c["count"])

    # Pass 2: substitutes for whatever is still short.
    for c in out:
        need = c["count"] - c["have_exact"]
        if need <= 0:
            continue
        entries = [o for o in pool.get(norm_name(c["name"]), ()) if o["_left"] > 0]
        any_printing = c.get("section") in ("trainer", "energy") or is_basic_energy(c["name"])
        if any_printing or not c.get("set"):
            c["have_other"] = take(entries, need)
        else:
            same_set = [o for o in entries
                        if sc.same_set(c["set"], o.get("set_code"), o.get("set_name"))]
            c["have_other"] = take(same_set, need)
            need -= c["have_other"]
            if need > 0:
                c["maybe"] = min(need, sum(o["_left"] for o in entries))

    total = have = 0
    for c in out:
        c["have"] = c["have_exact"] + c["have_other"]
        c["missing"] = c["count"] - c["have"]
        total += c["count"]
        have += c["have"]
    return {
        "cards": out,
        "total": total,
        "have": have,
        "missing": total - have,
        "maybe": sum(c["maybe"] for c in out),
        "completion_pct": round(have / total * 100, 1) if total else 0.0,
    }


# ----------------------------------------------------------------------
# Outside listings
# ----------------------------------------------------------------------

def search_links(name: str, set_code: Optional[str] = None,
                 number: Optional[str] = None) -> dict[str, str]:
    """Search links for buying a card off-platform. Plain search URLs, so no
    API key and nothing to go stale."""
    q = " ".join(b for b in (name, set_code or "", str(number or "")) if b)
    return {
        "tcgplayer": "https://www.tcgplayer.com/search/pokemon/product?q=" + quote_plus(q),
        "ebay": "https://www.ebay.com/sch/i.html?_sacat=183454&_nkw=" + quote_plus(q + " pokemon"),
    }


# ----------------------------------------------------------------------
# Popular decks (Limitless)
# ----------------------------------------------------------------------

class LimitlessClient:
    """Minimal client for the public Limitless tournament API."""

    def __init__(self, session=None, timeout: int = 30) -> None:
        if session is None:
            import requests
            session = requests.Session()
        self._session = session
        self.timeout = timeout

    def _get(self, path: str, params: Optional[dict] = None):
        resp = self._session.get(f"{LIMITLESS_API}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def tournaments(self, fmt: str = "STANDARD", limit: int = 50) -> list[dict]:
        return self._get("/tournaments", {"game": "PTCG", "format": fmt, "limit": limit})

    def standings(self, tournament_id: str) -> list[dict]:
        return self._get(f"/tournaments/{tournament_id}/standings")


def _deck_cards(decklist: dict) -> list[dict]:
    cards = []
    for section in SECTIONS:
        for c in decklist.get(section) or []:
            try:
                count = int(c.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if count <= 0 or not c.get("name"):
                continue
            cards.append({
                "section": section,
                "count": count,
                "name": c["name"],
                "set": c.get("set") or None,
                "number": str(c["number"]) if c.get("number") else None,
            })
    return cards


def aggregate_popular(events: list[tuple[dict, list[dict]]], top_n: int = 12) -> list[dict]:
    """Group tournament standings by archetype.

    events: [(tournament, standings), ...]. Returns the top_n archetypes by
    share of entered decks. The list shown for an archetype is the best
    placing among its players (ties go to the bigger tournament).
    """
    counts: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    best: dict[str, tuple] = {}
    entered = 0

    for tournament, standings in events:
        for row in standings or []:
            deck = row.get("deck") or {}
            deck_id = deck.get("id")
            if not deck_id or deck_id == "other":
                continue
            cards = _deck_cards(row.get("decklist") or {})
            if not cards:
                continue
            entered += 1
            counts[deck_id] += 1
            names[deck_id] = deck.get("name") or deck_id
            placing = row.get("placing") or 10 ** 6
            rank = (placing, -(tournament.get("players") or 0))
            if deck_id not in best or rank < best[deck_id][0]:
                best[deck_id] = (rank, tournament, row, cards)

    ranked = sorted(counts, key=lambda d: (-counts[d], names[d]))[:top_n]
    out = []
    for deck_id in ranked:
        _, tournament, row, cards = best[deck_id]
        record = row.get("record") or {}
        out.append({
            "id": deck_id,
            "name": names[deck_id],
            "decks_entered": counts[deck_id],
            "share_pct": round(counts[deck_id] / entered * 100, 1) if entered else 0.0,
            "cards": cards,
            "total": sum(c["count"] for c in cards),
            "sample": {
                "player": row.get("name") or row.get("player"),
                "placing": row.get("placing"),
                "record": "-".join(str(record.get(k, 0)) for k in ("wins", "losses", "ties")),
                "tournament_id": tournament.get("id"),
                "tournament": tournament.get("name"),
                "tournament_date": (tournament.get("date") or "")[:10],
                "tournament_players": tournament.get("players"),
                "url": f"https://play.limitlesstcg.com/tournament/{tournament.get('id')}/standings",
            },
        })
    return out


def fetch_popular(client: Optional[LimitlessClient] = None, fmt: str = "STANDARD",
                  n_tournaments: int = 3, top_n: int = 12, pause: float = 0.5) -> dict:
    """Pull the largest recent tournaments in a format and aggregate them.
    1 + n_tournaments HTTP requests."""
    client = client or LimitlessClient()
    recent = client.tournaments(fmt=fmt, limit=50)
    biggest = sorted(recent, key=lambda t: -(t.get("players") or 0))[:n_tournaments]
    events = []
    for t in biggest:
        time.sleep(pause)
        events.append((t, client.standings(t["id"])))
    decks = aggregate_popular(events, top_n=top_n)
    if not decks:
        raise ValueError("Limitless returned no decklists for the recent tournaments")
    return {
        "source": "Limitless TCG (play.limitlesstcg.com)",
        "format": fmt,
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tournaments": [
            {"id": t.get("id"), "name": t.get("name"),
             "date": (t.get("date") or "")[:10], "players": t.get("players")}
            for t, _ in events
        ],
        "decks": decks,
    }


def load_popular(cache_path: Optional[Path] = None,
                 bundled_path: Path = BUNDLED_POPULAR_PATH) -> dict:
    """Newest popular-deck data available: the refreshed cache if there is
    one, else the snapshot shipped with the repo. `origin` says which."""
    for path, origin in ((cache_path, "refreshed"), (bundled_path, "bundled")):
        if path is None:
            continue
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        if data.get("decks"):
            data["origin"] = origin
            return data
    return {"decks": [], "tournaments": [], "origin": "none", "fetched_at": None,
            "source": None, "format": None}


def save_popular(data: dict, cache_path: Path) -> None:
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(cache_path)
