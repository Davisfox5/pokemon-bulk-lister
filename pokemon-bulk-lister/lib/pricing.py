"""Price aggregation, outlier detection, and confidence scoring."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional

from lib import price_notes


OUTLIER_MULTIPLIER = 2.5
LOW_CONFIDENCE_THRESHOLD = 0.6

# Confidence considers BOTH relative spread (%) and absolute spread ($) between
# sources. Either signal can trigger high confidence (e.g. a $0.20 disagreement
# on a $0.10 card is 200% but is just noise — full confidence). Either signal
# can also reduce confidence (e.g. a 5% disagreement on a $1000 card is $50,
# which is real money — partial confidence).
TIGHT_SPREAD_RATIO = 0.20         # ≤ this % spread = "tight" on the relative axis
WIDE_SPREAD_RATIO = 1.20          # ≥ this % spread = "max uncertain" on the relative axis
ABSOLUTE_NOISE_FLOOR_USD = 2.00   # ≤ this $ spread = noise, ignore percentage entirely
# Prices this close are never a disagreement, whatever their ratio: $0.07 vs
# $0.30 is 4x but only 23 cents. Below it the price is the middle of the
# sources and the screens show the range. Every disagreement check uses it.
MIN_DISAGREEMENT_USD = ABSOLUTE_NOISE_FLOOR_USD


def far_apart(a: float, b: float, ratio: float) -> bool:
    """True when two prices disagree: more than `ratio` times apart AND more
    than MIN_DISAGREEMENT_USD apart in dollars."""
    lo, hi = min(a, b), max(a, b)
    return lo > 0 and hi > ratio * lo and (hi - lo) > MIN_DISAGREEMENT_USD
ABSOLUTE_REVIEW_USD = 25.00       # ≤ this $ spread = no penalty from the absolute axis
ABSOLUTE_HIGH_USD = 100.00        # ≥ this $ spread = "max uncertain" on the absolute axis


@dataclass
class PricingResult:
    price: Optional[float]
    sources: dict[str, Optional[float]]
    confidence: float
    outlier_flag: bool
    needs_review: bool
    notes: str


def _non_null(values: list[Optional[float]]) -> list[float]:
    return [v for v in values if v is not None and v > 0]


def aggregate(
    tcgplayer_market: Optional[float],
    ebay_median_30d: Optional[float],
    ebay_max_30d: Optional[float],
    cardmarket_trend_usd: Optional[float] = None,
    terapeak_median_usd: Optional[float] = None,
) -> PricingResult:
    """One price from the sources there are.

    The price is the MEDIAN of the non-null sources (the average of two).
    ebay_max_30d is recorded in `sources` for display but does not enter the
    price: it comes from the same 30-day comp set as ebay_median_30d.

    Nothing is dropped and nothing here holds a card for review; a person
    accepts the price or sets their own. Two things are noted beside it:
      * Sources more than OUTLIER_MULTIPLIER times apart AND more than
        MIN_DISAGREEMENT_USD ($2) apart set outlier_flag, with the note
        "Prices vary: TCGplayer $1.06, Cardmarket $7.06." Prices within $2
        never vary, whatever their ratio; the screens show their range.
      * One source alone is noted "Single pricing source."
    Only a card with no price at all needs review.

    Cardmarket trend price should already be FX-converted to USD by caller.
    """
    sources: dict[str, Optional[float]] = {
        "tcgplayer_market": tcgplayer_market,
        "ebay_median_30d": ebay_median_30d,
        "ebay_max_30d": ebay_max_30d,
        "cardmarket_trend_usd": cardmarket_trend_usd,
        "terapeak_median_usd": terapeak_median_usd,
    }
    valid = _non_null([tcgplayer_market, ebay_median_30d, cardmarket_trend_usd, terapeak_median_usd])
    # The sources as a note names them (lib/price_notes.py), the way the
    # screens do: eBay is the year of sales when there is one.
    named = [("TCGplayer", tcgplayer_market), ("Cardmarket", cardmarket_trend_usd),
             ("eBay", terapeak_median_usd or ebay_median_30d)]

    if not valid:
        return PricingResult(price=None, sources=sources, confidence=0.0, outlier_flag=False,
                             needs_review=True, notes=price_notes.NO_PRICE)

    outlier_flag = len(valid) >= 2 and far_apart(min(valid), max(valid), OUTLIER_MULTIPLIER)
    notes = (price_notes.disagree(named) if outlier_flag else
             price_notes.SINGLE if len(valid) == 1 else "")
    return PricingResult(
        price=round(statistics.median(valid), 2),
        sources=sources,
        confidence=round(_confidence(valid), 3),
        outlier_flag=outlier_flag,
        needs_review=False,
        notes=notes,
    )


def _confidence(values: list[float]) -> float:
    """Confidence considers both relative spread (%) and absolute spread ($).

    Decision flow:
      * 0 sources → 0.0
      * 1 source  → 0.5 (we have a number but nothing to corroborate)
      * Absolute spread ≤ $ABSOLUTE_NOISE_FLOOR_USD → 1.0 (cheap-card noise)
      * Otherwise: take the LOWER of the relative-axis confidence and the
        absolute-axis confidence — i.e. both axes have to look OK for us to
        report high confidence on an actionable price.

    Examples ($/conf):
      0.10 vs 0.30 (rel 200%, abs $0.20)  → noise floor → 1.00
      9.71 vs 14.83 (rel 53%, abs $5)     → rel 0.67, abs 1.00 → 0.67
      240   vs 280   (rel 17%, abs $40)   → rel 1.00, abs 0.80 → 0.80
      1200  vs 1260  (rel 5%,  abs $60)   → rel 1.00, abs 0.53 → 0.53
    """
    if len(values) == 0:
        return 0.0
    if len(values) == 1:
        return 0.5

    lo, hi = min(values), max(values)
    if lo <= 0:
        return 0.0

    abs_spread = hi - lo
    if abs_spread <= ABSOLUTE_NOISE_FLOOR_USD:
        return 1.0

    rel_spread = abs_spread / lo

    # Relative-axis confidence: 1.0 ≤ tight ratio, linearly decays to 0.0 by wide ratio.
    if rel_spread <= TIGHT_SPREAD_RATIO:
        rel_conf = 1.0
    else:
        rel_conf = 1.0 - (rel_spread - TIGHT_SPREAD_RATIO) / (WIDE_SPREAD_RATIO - TIGHT_SPREAD_RATIO)

    # Absolute-axis confidence: 1.0 ≤ review threshold, decays to 0.0 by high threshold.
    if abs_spread <= ABSOLUTE_REVIEW_USD:
        abs_conf = 1.0
    else:
        abs_conf = 1.0 - (abs_spread - ABSOLUTE_REVIEW_USD) / (ABSOLUTE_HIGH_USD - ABSOLUTE_REVIEW_USD)

    return max(0.0, min(1.0, min(rel_conf, abs_conf)))
