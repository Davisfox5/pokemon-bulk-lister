"""The words a person sees about a price.

Every note the pricing steps store is one of the short sentences below, so
the screens never show source codes, set ids, ratios or match levels. A
warning that should hold a card back for review is stored with a leading
"⚠ " (the code looks for it); `plain()` strips it before display.

`plain()` also turns notes stored by older versions ("TCG/CM disagree 6.7x
on swsh45 — variant mismatch?", "match: no_set" and the like) into the same
sentences, so a card priced months ago reads the same as one priced today.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

WARN = "⚠ "

FINISH = "Choose the finish (non-holo, holo or reverse holo) to price this card."
JAPANESE = "Japanese card, priced from the English version."
CHECK_ID = "Check that the card matches its photo."
SET_UNCONFIRMED = "Set not confirmed. Check it before listing."
NUMBER_UNCONFIRMED = "Card number not confirmed. Check the printing."
DISAGREE = "Prices disagree. Check the printing."
HIGH_LEFT_OUT = "One price was far above the others and was left out."
NO_PRICE = "No price found."
LOOKUP_FAILED = "The price lookup failed. Try pricing again."
CM_ESTIMATE = "Cardmarket price estimated from similar printings."
SET_FILLED = "Set filled in from matching cards."
FALLBACK = "Check the price before listing."
CLOSE = "The prices are close together."

# Most important first: the first one or two are what a person sees.
_ORDER = [FINISH, JAPANESE, CHECK_ID, "disagree", SET_UNCONFIRMED, NUMBER_UNCONFIRMED,
          HIGH_LEFT_OUT, NO_PRICE, LOOKUP_FAILED, CM_ESTIMATE, SET_FILLED]
SHOWN = 2


def money(value: float) -> str:
    return f"${value:,.2f}"


def disagree(prices: Iterable[tuple[str, Optional[float]]]) -> str:
    """'Prices disagree: TCGplayer $1.06, Cardmarket $7.06.'"""
    shown = [f"{name} {money(v)}" for name, v in prices if v]
    return f"Prices disagree: {', '.join(shown)}." if len(shown) >= 2 else DISAGREE


def warn(text: str) -> str:
    return WARN + text


# Older wordings, matched loosely. Each maps a stored fragment to a sentence.
_TCG_VS_CM = re.compile(r"TCG \$([\d.,]+) vs Cardmarket \$([\d.,]+)")
_CM_VS_TCG = re.compile(r"Cardmarket \$([\d.,]+) vs TCG \$([\d.,]+)")
_TWO = re.compile(r"two sources disagree\D*[\d.]+x \(([\d.]+) vs ([\d.]+)\)")
_LEGACY = [
    (re.compile(r"finish not checked|choose non-holo", re.I), FINISH),
    (re.compile(r"japanese", re.I), JAPANESE),
    (re.compile(r"identification needs review|compare the card to its photo", re.I), CHECK_ID),
    (re.compile(r"name-only match|matched on the name only|name only", re.I), SET_UNCONFIRMED),
    (re.compile(r"card number was not used", re.I), NUMBER_UNCONFIRMED),
    (re.compile(r"dropped from aggregate", re.I), HIGH_LEFT_OUT),
    (re.compile(r"no price sources", re.I), NO_PRICE),
    (re.compile(r"tcg lookup:", re.I), LOOKUP_FAILED),
    (re.compile(r"bulk-floor estimate", re.I), CM_ESTIMATE),
    (re.compile(r"set_code propagated", re.I), SET_FILLED),
    (re.compile(r"disagree|variant mismatch", re.I), DISAGREE),
]
_TECHNICAL = re.compile(r"[_=<>|]|https?:|pokemontcg|\bmatch:|\b\d+(\.\d+)?x\b|×")


def _close(sentence: str, prices) -> bool:
    """The prices named in a disagreement (or the card's own) are within the
    minimum gap that counts as one (lib/pricing.MIN_DISAGREEMENT_USD)."""
    from lib.pricing import MIN_DISAGREEMENT_USD

    values = [_num(v) for v in re.findall(r"\$(\d[\d,]*(?:\.\d+)?)", sentence)]
    if len(values) < 2 and prices is not None:
        values = [v for _, v in prices if v]
    return len(values) >= 2 and max(values) - min(values) <= MIN_DISAGREEMENT_USD


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _sentence(fragment: str) -> Optional[str]:
    """One stored fragment as a sentence a person can read, or None to drop it."""
    frag = fragment.strip().lstrip("⚠").strip()
    if not frag:
        return None
    m = _TCG_VS_CM.search(frag)
    if m:
        return disagree([("TCGplayer", _num(m.group(1))), ("Cardmarket", _num(m.group(2)))])
    m = _CM_VS_TCG.search(frag)
    if m:
        return disagree([("TCGplayer", _num(m.group(2))), ("Cardmarket", _num(m.group(1)))])
    m = _TWO.search(frag)
    if m:
        return disagree([("One source", _num(m.group(1))), ("another", _num(m.group(2)))])
    if frag.startswith("Prices disagree"):
        return frag
    for pattern, sentence in _LEGACY:
        if pattern.search(frag):
            return sentence
    # Anything else is shown only when it already reads as a plain sentence
    # (a spreadsheet note, say). Codes, ratios and lower-case fragments of
    # older notes are dropped.
    if frag[0].isupper() and not _TECHNICAL.search(frag) and "—" not in frag:
        return frag if frag.endswith(".") else frag + "."
    return None


def plain(notes: Optional[str], shown: int = SHOWN,
          prices: Optional[Iterable[tuple[str, Optional[float]]]] = None) -> str:
    """What a person sees: at most `shown` short sentences, most important
    first. Empty only when there were no notes at all. `prices` (the card's
    own source prices) fill in a disagreement an older note did not price."""
    if not notes or not notes.strip():
        return ""
    found: list[str] = []
    for fragment in re.split(r";\s*", notes):
        s = _sentence(fragment)
        if s == DISAGREE and prices is not None:
            s = disagree(prices)
        if s and s.startswith("Prices disagree") and _close(s, prices):
            s = CLOSE      # an older note about a few cents: not a disagreement
        if s and s not in found:
            found.append(s)
    # One disagreement sentence is enough; keep the one with the prices.
    with_prices = [s for s in found if s.startswith("Prices disagree:")]
    if with_prices:
        found = [s for s in found if s != DISAGREE and (not s.startswith("Prices disagree:") or s == with_prices[0])]

    def rank(s: str) -> int:
        key = "disagree" if s.startswith("Prices disagree") else s
        return _ORDER.index(key) if key in _ORDER else len(_ORDER)

    if len(found) > 1:
        found = [s for s in found if s != CLOSE]
    found.sort(key=rank)
    return " ".join(found[:shown]) or FALLBACK
