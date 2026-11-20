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
    # One source is noted beside the price; it does not hold the card back.
    assert not r.needs_review and r.notes == "Single pricing source."


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


def test_three_sources_far_apart_use_the_middle_one_and_say_so():
    r = aggregate(10.0, 100.0, None, cardmarket_trend_usd=11.0)
    assert r.price == 11.0
    assert r.outlier_flag and not r.needs_review
    assert r.notes == "Prices vary: TCGplayer $10.00, Cardmarket $11.00, eBay $100.00."


def test_two_sources_far_apart_use_their_average():
    r = aggregate(2.0, 40.0, None)
    assert r.price == 21.0
    assert r.outlier_flag and not r.needs_review


def test_tcg_and_cardmarket_far_apart_are_averaged_and_noted():
    r = aggregate(0.08, None, None, cardmarket_trend_usd=7.96)
    assert r.price == 4.02
    assert r.notes == "Prices vary: TCGplayer $0.08, Cardmarket $7.96."


def test_cheap_card_noise_floor_full_confidence():
    # $0.20 absolute spread on a cheap card is noise → confidence 1.0.
    r = aggregate(0.10, 0.30, None)
    assert r.confidence == 1.0
    assert not r.needs_review
