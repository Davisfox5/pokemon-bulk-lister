"""Tests for lib.pricing.aggregate — median-based aggregation + outlier handling."""
import pytest

from lib.pricing import aggregate


def test_no_sources():
    r = aggregate(None, None, None)
    assert r.price is None
    assert r.needs_review
    assert r.confidence == 0.0


def test_single_source_low_confidence():
    r = aggregate(1.50, None, None)
    assert r.price == 1.50
    assert r.confidence == 0.5
    assert r.needs_review  # below 0.6 threshold


def test_price_is_median_not_max():
    # 3 agreeing-ish sources: price must be the median, not the max.
    r = aggregate(10.0, 12.0, None, cardmarket_trend_usd=11.0)
    assert r.price == 11.0
    assert not r.outlier_flag


def test_ebay_max_excluded_from_aggregate():
    # ebay_max is recorded but must not pull the price up.
    r = aggregate(10.0, 10.0, 40.0)
    assert r.price == 10.0
    assert not r.outlier_flag
    assert r.sources["ebay_max_30d"] == 40.0


def test_outlier_dropped_with_three_sources():
    # 100 is > 2.5x the median of [10, 11, 100]; drop it, median the rest.
    r = aggregate(10.0, 100.0, None, cardmarket_trend_usd=11.0)
    assert r.price == 10.5
    assert r.outlier_flag
    assert r.needs_review


def test_two_sources_wild_disagreement_takes_lower():
    r = aggregate(2.0, 40.0, None)
    assert r.price == 2.0
    assert r.outlier_flag
    assert r.needs_review


def test_tcg_cm_variant_mismatch_drops_smaller():
    # >5x apart: CM larger → CM dropped, TCG kept.
    r = aggregate(0.08, None, None, cardmarket_trend_usd=7.96)
    assert r.price == 0.08
    assert r.notes == "Prices disagree: TCGplayer $0.08, Cardmarket $7.96."


def test_cheap_card_noise_floor_full_confidence():
    # $0.20 absolute spread on a cheap card is noise → confidence 1.0.
    r = aggregate(0.10, 0.30, None)
    assert r.confidence == 1.0
    assert not r.needs_review
