"""Price aggregation, outlier detection, and confidence scoring."""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional

from lib import price_notes


OUTLIER_MULTIPLIER = 2.5
# The order of the price list in aggregate(), for naming sources in notes.
_SOURCE_NAMES = ("TCGplayer", "eBay", "Cardmarket", "Terapeak")
LOW_CONFIDENCE_THRESHOLD = 0.6

# Confidence considers BOTH relative spread (%) and absolute spread ($) between
# sources. Either signal can trigger high confidence (e.g. a $0.20 disagreement
# on a $0.10 card is 200% but is just noise — full confidence). Either signal
# can also reduce confidence (e.g. a 5% disagreement on a $1000 card is $50,
# which is real money — partial confidence).
TIGHT_SPREAD_RATIO = 0.20         # ≤ this % spread = "tight" on the relative axis
WIDE_SPREAD_RATIO = 1.20          # ≥ this % spread = "max uncertain" on the relative axis
ABSOLUTE_NOISE_FLOOR_USD = 2.00   # ≤ this $ spread = noise, ignore percentage entirely
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
    """Aggregate prices from multiple sources.

    Rule:
      1) If TCGplayer and Cardmarket on the same matched card disagree by
         >5x, treat the outlier as bad upstream data (pokemontcg.io often
         reflects a different printing's price under one of the two fields).
         Drop the smaller source and use the other — TCGplayer market is
         usually the more trustworthy daily-updated source for US pricing.
      2) Price = MEDIAN of the remaining non-null sources.
         ebay_max_30d is recorded in `sources` for display but does NOT
         enter the aggregate: it comes from the same 30-day comp set as
         ebay_median_30d, so counting both double-weighted eBay and biased
         the result high.
         - With >=3 sources, a max > 2.5x median is dropped as an outlier
           and the median re-taken over the rest.
         - With exactly 2 sources disagreeing >2.5x AND by more than the
           absolute noise floor ($2), use the LOWER one (their median would
           just split the difference; over-listing eats returns,
           under-listing only eats margin).
         Either correction sets outlier_flag and forces needs_review.

    Cardmarket trend price should already be FX-converted to USD by caller.
    """
    sources: dict[str, Optional[float]] = {
        "tcgplayer_market": tcgplayer_market,
        "ebay_median_30d": ebay_median_30d,
        "ebay_max_30d": ebay_max_30d,
        "cardmarket_trend_usd": cardmarket_trend_usd,
        "terapeak_median_usd": terapeak_median_usd,
    }

    # Bad-data correction: TCG vs Cardmarket disagreement on the SAME card.
    # Implausible spreads here are almost always a variant-mismatch on the
    # pokemontcg.io side, not a real market disagreement. Drop the outlier
    # so we don't list overpriced and eat returns.
    data_correction_note = ""
    tcg = tcgplayer_market
    cm = cardmarket_trend_usd
    if tcg and cm and tcg > 0 and cm > 0:
        ratio = max(tcg, cm) / min(tcg, cm)
        if ratio > 5.0:
            # The note says what disagreed, in words a person reads
            # (lib/price_notes.py); which one was dropped shows in the price.
            data_correction_note = price_notes.disagree([("TCGplayer", tcg), ("Cardmarket", cm)])
            if cm > tcg:
                cardmarket_trend_usd = None
            else:
                tcgplayer_market = None

    prices = [
        tcgplayer_market,
        ebay_median_30d,
        cardmarket_trend_usd,
        terapeak_median_usd,
    ]
    valid = _non_null(prices)

    if not valid:
        return PricingResult(
            price=None,
            sources=sources,
            confidence=0.0,
            outlier_flag=False,
            needs_review=True,
            notes=price_notes.NO_PRICE,
        )

    median = statistics.median(valid)
    hi = max(valid)
    lo = min(valid)
    outlier_flag = False
    notes = data_correction_note
    used = valid

    if len(valid) >= 3 and hi > OUTLIER_MULTIPLIER * median:
        used = sorted(valid)[:-1]
        price = statistics.median(used)
        outlier_flag = True
        extra = price_notes.HIGH_LEFT_OUT
        notes = f"{notes}; {extra}" if notes else extra
    elif (
        len(valid) == 2
        and hi > OUTLIER_MULTIPLIER * lo
        and (hi - lo) > ABSOLUTE_NOISE_FLOOR_USD  # $0.10-vs-$0.30 is noise, not an outlier
    ):
        used = [lo]
        price = lo
        outlier_flag = True
        extra = price_notes.disagree([(name, v) for name, v in zip(_SOURCE_NAMES, prices) if v in (lo, hi)])
        notes = f"{notes}; {extra}" if notes else extra
    else:
        price = median

    confidence = _confidence(used)
    needs_review = confidence < LOW_CONFIDENCE_THRESHOLD or outlier_flag

    return PricingResult(
        price=round(price, 2),
        sources=sources,
        confidence=round(confidence, 3),
        outlier_flag=outlier_flag,
        needs_review=needs_review,
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
