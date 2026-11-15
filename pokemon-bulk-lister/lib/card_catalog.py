"""The real card catalog, and checking an identification against it.

No model is involved. A card's printed name and collector number ("023/128")
pin down which card it is: the number after the slash is how many cards the
set prints, which names the set, and the set's own card list then says what
card number 23 is. `resolve` does that lookup and says whether what was read
off a photo is a card that exists.

Two things are cached, because the source (pokemontcg.io) is slow and often
down:
  sets            every set: id, name, printed code, printed total, release date.
                  Refreshed daily; a copy ships in data/sets.json.
  cards of a set  fetched the first time a set is needed, then kept.

`store` is where the cache lives: a pair of functions get(key) / set(key, value)
holding JSON-able values. The web app puts it in the database; by default it
is a folder of files.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
BUNDLED_SETS = ROOT / "data" / "sets.json"
SETS_MAX_AGE = 24 * 3600
CARDS_MAX_AGE = 14 * 24 * 3600          # a new set gains cards for a while after release
API = "https://api.pokemontcg.io/v2"


# ---------------------------------------------------------------- cache

class _FileStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _path(self, key: str) -> Path:
        return self.folder / (re.sub(r"[^a-z0-9._-]", "_", key.lower()) + ".json")

    def get(self, key: str) -> Any:
        try:
            return json.loads(self._path(key).read_text())
        except (OSError, ValueError):
            return None

    def set(self, key: str, value: Any) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps(value))


_store: Any = _FileStore(Path(os.getenv("DATA_ROOT") or ROOT) / "output" / "cache" / "catalog")


def use_store(get: Callable[[str], Any], set_: Callable[[str, Any], None]) -> None:
    """Keep the cache somewhere else (the web app: its database)."""
    global _store

    class _Store:
        pass

    s = _Store()
    s.get, s.set = get, set_      # type: ignore[attr-defined]
    _store = s


# ---------------------------------------------------------------- fetching

_down_until = 0.0      # after a failed fetch, do not try again for a minute


def _fetch(path: str, params: dict) -> dict:
    """One page from pokemontcg.io, retried: it answers 5xx about as often
    as not. Once it has failed, calls fail at once for a minute, so a batch
    of cards does not wait on a source that is down. CARD_CATALOG_OFFLINE=1
    never calls out (tests)."""
    global _down_until
    if os.getenv("CARD_CATALOG_OFFLINE") == "1" or time.monotonic() < _down_until:
        raise RuntimeError("card catalog unreachable")
    import requests

    headers = {"X-Api-Key": os.environ["POKEMONTCG_API_KEY"]} if os.getenv("POKEMONTCG_API_KEY") else {}
    last: Any = None
    for attempt in range(3):
        try:
            r = requests.get(f"{API}{path}", params=params, headers=headers, timeout=12)
            if r.status_code == 200:
                return r.json()
            last = f"{r.status_code}"
        except Exception as exc:        # timeouts, resets
            last = exc
        time.sleep(0.5 * (attempt + 1))
    _down_until = time.monotonic() + 60
    raise RuntimeError(f"card catalog unreachable ({last})")


def _all_pages(path: str, params: dict) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        data = _fetch(path, {**params, "page": page, "pageSize": 250})
        rows = data.get("data") or []
        out.extend(rows)
        if not rows or len(out) >= int(data.get("totalCount") or 0):
            return out
        page += 1


# ---------------------------------------------------------------- sets

def _bundled_sets() -> list[dict]:
    try:
        return json.loads(BUNDLED_SETS.read_text())["sets"]
    except (OSError, ValueError, KeyError):
        return []


def refresh_sets() -> int:
    """Fetch the full set list now. Returns how many sets there are."""
    rows = _all_pages("/sets", {"select": "id,name,series,printedTotal,total,ptcgoCode,releaseDate"})
    if rows:
        _store.set("sets", {"at": time.time(), "sets": rows})
    return len(rows)


def sets() -> list[dict]:
    """Every set, oldest first. Never fails: a stale or bundled list is used
    when the source cannot be reached."""
    saved = _store.get("sets") or {}
    rows = saved.get("sets") or []
    if not rows or time.time() - float(saved.get("at") or 0) > SETS_MAX_AGE:
        try:
            refresh_sets()
            rows = (_store.get("sets") or {}).get("sets") or rows
        except Exception:
            if rows:        # stale beats nothing; do not retry on every call
                _store.set("sets", {"at": time.time() - SETS_MAX_AGE + 3600, "sets": rows})
    known = {s["id"] for s in rows}
    rows = rows + [s for s in _bundled_sets() if s["id"] not in known]
    return sorted(rows, key=lambda s: (s.get("releaseDate") or "", s["id"]))


def recent_sets(n: int = 60) -> list[dict]:
    """The newest n sets, newest first."""
    return list(reversed(sets()))[:n]


def set_cards(set_id: str) -> list[dict]:
    """Every card in a set: id, name, number, rarity. Raises when the set has
    never been fetched and the source cannot be reached."""
    key = f"set-cards:{set_id}"
    saved = _store.get(key) or {}
    cards = saved.get("cards")
    if cards is not None and time.time() - float(saved.get("at") or 0) <= CARDS_MAX_AGE:
        return cards
    try:
        fresh = [{"id": c.get("id"), "name": c.get("name") or "", "number": str(c.get("number") or ""),
                  "rarity": c.get("rarity")}
                 for c in _all_pages("/cards", {"q": f"set.id:{set_id}", "select": "id,name,number,rarity"})]
    except Exception:
        try:
            fresh = _mirror_cards(set_id)
        except Exception:
            if cards is not None:
                return cards
            raise
    _store.set(key, {"at": time.time(), "cards": fresh})
    return fresh


MIRROR = "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master/cards/en"


def _mirror_cards(set_id: str) -> list[dict]:
    """A set's card list from the catalog's own data repository, which stays
    up when its API does not. No prices there, which this does not need."""
    if os.getenv("CARD_CATALOG_OFFLINE") == "1" or not re.fullmatch(r"[a-z0-9]+", set_id or ""):
        raise RuntimeError("card catalog unreachable")
    import requests

    r = requests.get(f"{MIRROR}/{set_id}.json", timeout=15)
    r.raise_for_status()
    return [{"id": c.get("id"), "name": c.get("name") or "", "number": str(c.get("number") or ""),
             "rarity": c.get("rarity")} for c in r.json()]


# ---------------------------------------------------------------- Japanese cards
#
# The catalog is English. A Japanese card is named from a table (the printed
# name -> the English one, no model), and checked by number only where an
# English set prints the same cards under the same numbers.

JA_NAMES = ROOT / "data" / "pokemon_names_ja.json"
JA_SETS = ROOT / "data" / "ja_sets.json"
JAPANESE = "(Japanese)"

_OWNERS = {"ロケット団": "Team Rocket's", "N": "N's", "ヒビキ": "Ethan's", "リーリエ": "Lillie's", "マリィ": "Marnie's",
           "ナンジャモ": "Iono's", "ホップ": "Hop's", "ダイゴ": "Steven's", "シロナ": "Cynthia's", "カスミ": "Misty's"}
_REGIONS = {"アローラ": "Alolan", "ガラル": "Galarian", "ヒスイ": "Hisuian", "パルデア": "Paldean"}
_FORMS = {"ヒートロトム": "Heat Rotom", "ウォッシュロトム": "Wash Rotom", "フロストロトム": "Frost Rotom",
          "スピンロトム": "Fan Rotom", "カットロトム": "Mow Rotom"}
_ja_names: Optional[dict] = None
_ja_sets: Optional[dict] = None


def is_japanese(text: Optional[str]) -> bool:
    """Has kana or kanji in it."""
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text or ""))


def _ja_table() -> dict:
    global _ja_names
    if _ja_names is None:
        try:
            _ja_names = json.loads(JA_NAMES.read_text(encoding="utf-8"))["names"]
        except (OSError, ValueError, KeyError):
            _ja_names = {}
    return _ja_names


def japanese_set(printed_code: Optional[str]) -> Optional[dict]:
    """What is known of a Japanese set from the code printed on the card
    ("sv11W"): name, total, and the English twin set if there is one."""
    global _ja_sets
    if _ja_sets is None:
        try:
            _ja_sets = json.loads(JA_SETS.read_text(encoding="utf-8"))["sets"]
        except (OSError, ValueError, KeyError):
            _ja_sets = {}
    return _ja_sets.get((printed_code or "").strip().lower())


def japanese_set_named(set_name: Optional[str]) -> Optional[dict]:
    """The same, from a stored set name such as "White Flare (Japanese)"."""
    japanese_set("")
    plain = _norm((set_name or "").replace(JAPANESE, ""))
    for row in (_ja_sets or {}).values():
        if _norm(row.get("name")) == plain:
            return row
    return None


def english_name(printed: Optional[str]) -> Optional[str]:
    """The English name of a card from its printed Japanese name, or None
    when the table does not have it (trainers, items)."""
    text = unicodedata.normalize("NFKC", printed or "").strip()
    text = re.sub(r"\s+", "", text)
    suffix = ""
    m = re.search(r"(ex|EX|GX|VMAX|VSTAR|V)$", text)
    if m and is_japanese(text[:m.start()]):
        suffix, text = " " + m.group(1), text[:m.start()]
    owner = ""
    for ja, en in _OWNERS.items():
        if text.startswith(ja + "の"):
            owner, text = en + " ", text[len(ja) + 1:]
            break
    region = ""
    for ja, en in _REGIONS.items():
        if text.startswith(ja) and text[len(ja):] in _ja_table():
            region, text = en + " ", text[len(ja):]
            break
    base = _FORMS.get(text) or _ja_table().get(text)
    return f"{owner}{region}{base}{suffix}" if base else None


def resolve_japanese(name_printed: str, name_read: Optional[str] = None, number: Optional[str] = None,
                     total: Any = None, printed_code: Optional[str] = None,
                     set_name_read: Optional[str] = None) -> dict:
    """Check a Japanese card. Same shape as resolve():
      verified  the printed name translates to a card that is, once, in the
                English set that reprints this one (its numbers differ, so
                the number stays as read).
      probable  the printed name is in the name table, so the name is right;
                the set and number are as read.
      unknown   neither: what the reader made of it stands.
    The set name carries "(Japanese)" and there is no set_code: the catalog
    ids are English sets."""
    num = _number(number)
    known = japanese_set(printed_code)
    if known is None and _total(total):
        fits = [s for s in (japanese_set("") or _ja_sets or {}).values() if s.get("total") == _total(total)]
        known = fits[0] if len(fits) == 1 else None
    set_name = (known or {}).get("name") or (set_name_read or "").replace(JAPANESE, "").strip()
    set_name = f"{set_name} {JAPANESE}".strip()
    translated = english_name(name_printed)

    def record(name: str, rarity: Optional[str] = None) -> dict:
        return {"name": name, "set_name": set_name, "set_code": "", "card_number": num or (number or ""),
                "rarity": rarity}

    twin = (known or {}).get("twin")
    if twin and translated:
        try:
            cards = set_cards(twin)
        except Exception:
            cards = []
        named = ([c for c in cards if c["name"].casefold() == translated.casefold()]       # "Nidoran♂" is not "Nidoran♀"
                 or [c for c in cards if _norm(c["name"]) == _norm(translated)])
        if named:
            def _ord(c: dict) -> tuple:
                raw = str(c.get("number") or "")
                return (0, int(raw)) if raw.isdigit() else (1, raw)
            named = sorted(named, key=_ord)
            total_n = int((known or {}).get("total") or 0)
            ordinary_cards = [c for c in named if str(c.get("number") or "").isdigit() and int(c["number"]) <= total_n]
            asked_ordinary = len(named) == 1 or (num.isdigit() and total_n and int(num) <= total_n)
            pick = ordinary_cards[0] if asked_ordinary and ordinary_cards else named[0]
            return {"status": "verified", "card": record(pick["name"], pick.get("rarity") if asked_ordinary else None),
                    "why": ""}
    if translated:
        return {"status": "probable", "card": record(translated),
                "why": "named from the printed Japanese name; the English catalog has no list for this set"}
    return {"status": "unknown", "card": record((name_read or "").strip()) if name_read else None,
            "why": "a Japanese card the name table does not have"}


# ---------------------------------------------------------------- matching

def _norm(text: Optional[str]) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _number(text: Optional[str]) -> str:
    """'023' -> '23', 'TG05' -> 'tg5', '023/128' -> '23'."""
    text = (text or "").split("/")[0].strip().lower()
    m = re.match(r"^([a-z]*)0*(\d+)([a-z]*)$", text)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else text


def _total(text: Any) -> Optional[int]:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group(0)) if m else None


def _same_name(read: str, real: str) -> bool:
    """The name read off a card against the catalog's. Exact after
    normalising, or one a short slip from the other ("Goldengo" for
    "Gholdengo"): the number already pins the card, the name confirms it."""
    a, b = _norm(read), _norm(real)
    if not a or not b:
        return False
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return True
    if abs(len(a) - len(b)) > 2 or min(len(a), len(b)) < 5:
        return False
    # edit distance <= 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 2


def _candidate_sets(total: Optional[int], printed_code: Optional[str], set_code: Optional[str],
                    set_name: Optional[str]) -> list[dict]:
    """Sets the card could be from, most likely first: named by what is
    printed on the card (set code, total), then by what the reader thought."""
    every = sets()
    code = (printed_code or "").strip().upper()
    ranked: list[dict] = []

    def add(rows: list[dict]) -> None:
        for s in sorted(rows, key=lambda s: s.get("releaseDate") or "", reverse=True):
            if s not in ranked:
                ranked.append(s)

    pinned: list[dict] = []
    if code and total:
        pinned = [s for s in every if (s.get("ptcgoCode") or "").upper() == code and s.get("printedTotal") == total]
        add(pinned)
    if code:
        add([s for s in every if (s.get("ptcgoCode") or "").upper() == code])
    # A printed total is shared by many sets. Once the printed code already
    # named a set of that size, another set of the same size must not win.
    if total and not pinned:
        add([s for s in every if s.get("printedTotal") == total])
    if set_code:
        add([s for s in every if s["id"].lower() == set_code.strip().lower()])
    if set_name:
        add([s for s in every if _norm(s.get("name")) == _norm(set_name)])
    return ranked


def resolve(name: str, number: Optional[str] = None, total: Any = None, printed_code: Optional[str] = None,
            set_code: Optional[str] = None, set_name: Optional[str] = None, max_sets: int = 6) -> dict:
    """Check what was read off a card against the catalog.

    Returns {"status", "why", "card"}:
      verified    a card with this name and number exists in a set that fits
                  what is printed on it. "card" is the catalog's record:
                  name, set_name, set_code, card_number, rarity.
      probable    the name is in a fitting set, once, under another number
                  (the number was misread). "card" as above.
      unverified  nothing fits. "card" is None; "why" says what was tried.
      unknown     the catalog could not be reached, so nothing was checked.
    """
    num = _number(number)
    total_n = _total(total) if total not in (None, "") else (_total(str(number).split("/")[1]) if number and "/" in str(number) else None)
    candidates = _candidate_sets(total_n, printed_code, set_code, set_name)
    if not candidates:
        return {"status": "unverified", "card": None,
                "why": "no set fits what was read (no printed total or set code, and the set named is not a real one)"}

    def record(s: dict, c: dict) -> dict:
        return {"name": c["name"], "set_name": s.get("name"), "set_code": s["id"],
                "card_number": c["number"], "rarity": c.get("rarity")}

    by_number: list[dict] = []
    by_name: list[dict] = []
    reached = False
    for s in candidates[:max_sets]:
        try:
            cards = set_cards(s["id"])
        except Exception:
            continue
        reached = True
        for c in cards:
            if num and _number(c["number"]) == num and _same_name(name, c["name"]):
                by_number.append(record(s, c))
        named = [c for c in cards if _same_name(name, c["name"])]
        if len(named) == 1:
            by_name.append(record(s, named[0]))
        if by_number:
            break               # candidates are in order of likelihood
    if not reached:
        return {"status": "unknown", "card": None, "why": "the card catalog could not be reached"}
    if by_number:
        return {"status": "verified", "card": by_number[0], "why": ""}
    if len(by_name) == 1:
        return {"status": "probable", "card": by_name[0],
                "why": f"the name is in {by_name[0]['set_name']} as number {by_name[0]['card_number']}, not {number or 'the number read'}"}
    tried = ", ".join(s.get("name") or s["id"] for s in candidates[:max_sets])
    return {"status": "unverified", "card": None, "why": f"no card named {name!r} numbered {number or '?'} in: {tried}"}


def prompt_reference(n: int = 60) -> str:
    """The newest sets as text for the identification prompt: a reader that
    has never seen a set can still name it from the code and total printed on
    the card."""
    lines = [f"{s.get('name')} | id {s['id']} | printed code {s.get('ptcgoCode') or '-'} | "
             f"{s.get('printedTotal') or s.get('total') or '?'} cards | {s.get('releaseDate') or ''}"
             for s in recent_sets(n)]
    return "\n".join(lines)
