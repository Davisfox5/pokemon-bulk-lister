"""The words a person sees about a price.

Every note is one of the short sentences below, so the screens never show
source codes, set ids, ratios or match levels. A warning that holds a card
back for review is stored with a leading "⚠ " (the code looks for it);
`plain()` strips it before display.

A price disagreement always reads the same way, built by one function from
the card's own source prices, whatever the stored note says:

    Prices vary: TCGplayer $1.06, Cardmarket $7.06. Using $4.06.

Sources in a fixed order (TCGplayer, Cardmarket, eBay), each price to the
cent, then the price the card is listed at. Older notes ("TCG/CM disagree
6.7x on swsh45 — variant mismatch?", "dropped from aggregate" and the like)
are recognised and shown in this same format, so a card priced months ago
reads the same as one priced today. Prices within the minimum gap
(lib/pricing.MIN_DISAGREEMENT_USD) never show as a disagreement.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

WARN = "⚠ "

FINISH = "Choose the finish (non-holo, holo or reverse holo) to price this card."
JAPANESE = "Japanese card, priced from the English version."
CHECK_ID = "Check the card against the photo."
SET_NEEDED = "Choose the set to price this card."
SET_UNCONFIRMED = "Set not confirmed. Check the set before listing."
NUMBER_UNCONFIRMED = "Card number not confirmed. Check the printing."
DISAGREE = "Prices vary."                # stored marker when no prices are known
SINGLE = "Single pricing source."
NO_PRICE = "No price found."
LOOKUP_FAILED = "The price lookup failed. Try pricing again."
CM_ESTIMATE = "Cardmarket price estimated from similar printings."
SET_FILLED = "Set filled in from matching cards."
FALLBACK = "Check the price before listing."
CLOSE = "The prices are close together."

SOURCE_ORDER = ("TCGplayer", "Cardmarket", "eBay")
_KIND_DISAGREE = "disagree"
# Most important first: the first one or two are what a person sees.
_ORDER = [FINISH, SET_NEEDED, JAPANESE, CHECK_ID, _KIND_DISAGREE, SET_UNCONFIRMED, NUMBER_UNCONFIRMED,
          NO_PRICE, LOOKUP_FAILED, CM_ESTIMATE, SET_FILLED, SINGLE, CLOSE]
SHOWN = 2

Prices = Iterable[tuple[str, Optional[float]]]


def money(value: float) -> str:
    return f"${value:,.2f}"


def _ordered(prices: Prices) -> list[tuple[str, float]]:
    """Positive prices, one per source, in SOURCE_ORDER (others after)."""
    seen: dict[str, float] = {}
    for name, value in prices:
        if value and value > 0 and name not in seen:
            seen[name] = float(value)
    rank = {n: i for i, n in enumerate(SOURCE_ORDER)}
    return sorted(seen.items(), key=lambda kv: rank.get(kv[0], len(rank)))


def disagreement(prices: Prices, using: Optional[float] = None) -> str:
    """The one way a price disagreement is shown:
    'Prices vary: TCGplayer $1.06, Cardmarket $7.06. Using $4.06.'"""
    listed = _ordered(prices)
    head = (f"Prices vary: {', '.join(f'{n} {money(v)}' for n, v in listed)}."
            if len(listed) >= 2 else DISAGREE)
    return f"{head} Using {money(using)}." if using else head


def disagree(prices: Prices) -> str:
    """What the pricing steps store when sources disagree (no 'Using': the
    price is shown next to it, and the display adds it)."""
    return disagreement(prices)


def warn(text: str) -> str:
    return WARN + text


# ------------------------------------------------------------ reading notes

_TCG_VS_CM = re.compile(r"TCG \$([\d.,]+?)\.? vs Cardmarket \$([\d.,]+?)\.?(?:\s|\)|$)")
_CM_VS_TCG = re.compile(r"Cardmarket \$([\d.,]+?)\.? vs TCG \$([\d.,]+?)\.?(?:\s|\)|$)")
_NAMED = re.compile(r"(TCGplayer|Cardmarket|eBay|Terapeak) \$(\d[\d,]*(?:\.\d+)?)")
_TWO = re.compile(r"two sources disagree\D*[\d.]+x \(([\d.]+) vs ([\d.]+)\)")
_LEGACY = [
    (re.compile(r"finish not checked|choose non-holo|choose the finish", re.I), FINISH),
    (re.compile(r"choose the set", re.I), SET_NEEDED),
    (re.compile(r"japanese", re.I), JAPANESE),
    (re.compile(r"identification needs review|compare the card to its photo|matches its photo|against the photo", re.I), CHECK_ID),
    (re.compile(r"name-only match|matched on the name only|name only|set not confirmed", re.I), SET_UNCONFIRMED),
    (re.compile(r"card number was not used|card number not confirmed", re.I), NUMBER_UNCONFIRMED),
    (re.compile(r"no price sources|no price found", re.I), NO_PRICE),
    (re.compile(r"tcg lookup:|price lookup failed", re.I), LOOKUP_FAILED),
    (re.compile(r"bulk-floor estimate|estimated from similar printings", re.I), CM_ESTIMATE),
    (re.compile(r"set_code propagated|set filled in", re.I), SET_FILLED),
    (re.compile(r"close together", re.I), CLOSE),
    (re.compile(r"single pricing source", re.I), SINGLE),
    # Notes saved in older wording, shown without pronouns.
    (re.compile(r"from your spreadsheet", re.I), "From the imported spreadsheet."),
    (re.compile(r"unattributed spreadsheet estimate removed", re.I),
     "Unattributed spreadsheet estimate removed. Re-import the sheet to restore the estimate."),
]
# Any of these, in any wording, is a price disagreement.
_DISAGREE_WORDS = re.compile(r"disagree|prices vary|variant mismatch|dropped from aggregate|far above the others", re.I)
_TECHNICAL = re.compile(r"[_=<>|]|https?:|pokemontcg|\bmatch:|\b\d+(\.\d+)?x\b|×")


def _num(text: str) -> float:
    return float(text.replace(",", "").rstrip("."))


def _stated_prices(frag: str) -> list[tuple[str, float]]:
    """Source prices an older note spelled out, if any."""
    m = _TCG_VS_CM.search(frag)
    if m:
        return [("TCGplayer", _num(m.group(1))), ("Cardmarket", _num(m.group(2)))]
    m = _CM_VS_TCG.search(frag)
    if m:
        return [("TCGplayer", _num(m.group(2))), ("Cardmarket", _num(m.group(1)))]
    named = [(("eBay" if n == "Terapeak" else n), _num(v)) for n, v in _NAMED.findall(frag)]
    if len(named) >= 2:
        return named
    m = _TWO.search(frag)
    if m:
        return [("One source", _num(m.group(1))), ("Another", _num(m.group(2)))]
    return []


def _kind(fragment: str) -> tuple[Optional[str], list[tuple[str, float]]]:
    """What one stored fragment says: a sentence, or _KIND_DISAGREE with any
    prices it named. (None, []) to drop it."""
    frag = fragment.strip().lstrip("⚠").strip()
    if not frag:
        return None, []
    if _DISAGREE_WORDS.search(frag):
        return _KIND_DISAGREE, _stated_prices(frag)
    for pattern, sentence in _LEGACY:
        if pattern.search(frag):
            return sentence, []
    # Anything else is shown only when it already reads as a plain sentence
    # (a spreadsheet note, say). Codes, ratios and lower-case fragments of
    # older notes are dropped.
    if frag[0].isupper() and not _TECHNICAL.search(frag) and "—" not in frag:
        return (frag if frag.endswith(".") else frag + "."), []
    return None, []


def _close(prices: list[tuple[str, float]]) -> bool:
    from lib.pricing import MIN_DISAGREEMENT_USD

    values = [v for _, v in prices]
    return len(values) >= 2 and max(values) - min(values) <= MIN_DISAGREEMENT_USD


def plain(notes: Optional[str], shown: int = SHOWN, prices: Optional[Prices] = None,
          using: Optional[float] = None, outlier: bool = False) -> str:
    """What a person sees: at most `shown` short sentences, most important
    first.

    prices:  the card's own source prices, as (name, value), which a
             disagreement is always built from when there are two or more.
    using:   the price the card is listed at, for 'Using $X.'
    outlier: the card was flagged for disagreeing prices; it is shown even
             when the stored note does not say so.

    Empty only when there are no notes and nothing flagged."""
    card_prices = _ordered(prices or [])
    found: list[str] = []
    disagreed, stated = outlier, []
    for fragment in re.split(r";\s*", notes or ""):
        kind, named = _kind(fragment)
        if kind == _KIND_DISAGREE:
            disagreed = True
            stated = stated or named
        elif kind and kind not in found:
            found.append(kind)
    if disagreed:
        basis = card_prices if len(card_prices) >= 2 else _ordered(stated)
        if basis and _close(basis):
            found.append(CLOSE)           # a few cents apart: not a disagreement
        else:
            found.append(disagreement(basis, using))
    if not found:
        return FALLBACK if (notes or "").strip() else ""

    def rank(s: str) -> int:
        key = _KIND_DISAGREE if s.startswith(DISAGREE[:-1]) else s
        return _ORDER.index(key) if key in _ORDER else len(_ORDER)

    if len(found) > 1:
        found = [s for s in found if s != CLOSE]
    if SET_NEEDED in found:
        found = [s for s in found if s != NO_PRICE]     # the missing set is the reason
    if LOOKUP_FAILED in found:
        found = [s for s in found if s != NO_PRICE]     # the failure is the reason
    found.sort(key=rank)
    return " ".join(found[:shown])
