"""Tests for lib.cardmarket_client — name normalization + guarded name matching."""
from lib.cardmarket_client import CardmarketPriceGuide, _normalize


def _guide(entries):
    return {"priceGuides": entries}


def _products(entries):
    return {"products": entries}


def make(products, guides):
    g = CardmarketPriceGuide(cache_dir="/nonexistent")
    g.load_from_data(_guide(guides), _products(products))
    return g


def test_normalize_strips_disambiguators():
    assert _normalize("Kakuna [Bug Bite | Primal Clash]") == "kakuna"
    assert _normalize("Pikachu (Promo)") == "pikachu"
    assert _normalize("  Mew   ex ") == "mew ex"


def test_single_candidate_match():
    g = make(
        [{"idProduct": 1, "name": "Charcadet [Flare]"}],
        [{"idProduct": 1, "trend": 0.08, "trend-holo": None}],
    )
    price, note = g.lookup_trend_eur("Charcadet")
    assert price == 0.08
    assert "single printing" in note


def test_agreeing_candidates_take_median():
    g = make(
        [{"idProduct": i, "name": "Weedle"} for i in (1, 2, 3)],
        [
            {"idProduct": 1, "trend": 0.05, "trend-holo": None},
            {"idProduct": 2, "trend": 0.10, "trend-holo": None},
            {"idProduct": 3, "trend": 0.12, "trend-holo": None},
        ],
    )
    price, note = g.lookup_trend_eur("Weedle")
    assert price == 0.10


def test_expensive_wide_spread_refused():
    # Printings disagree AND even the cheap quartile is above bulk range:
    # a name-only match can't be trusted.
    g = make(
        [{"idProduct": i, "name": "Charizard"} for i in (1, 2)],
        [
            {"idProduct": 1, "trend": 12.90, "trend-holo": None},
            {"idProduct": 2, "trend": 250.0, "trend-holo": None},
        ],
    )
    price, note = g.lookup_trend_eur("Charizard")
    assert price is None
    assert "ambiguous" in note


def test_bulk_name_gets_conservative_p25():
    # Bulk common with one secret-rare printing: p25 is the safe estimate.
    g = make(
        [{"idProduct": i, "name": "Charcadet"} for i in (1, 2, 3, 4)],
        [
            {"idProduct": 1, "trend": 0.04, "trend-holo": None},
            {"idProduct": 2, "trend": 0.08, "trend-holo": None},
            {"idProduct": 3, "trend": 0.12, "trend-holo": None},
            {"idProduct": 4, "trend": 95.0, "trend-holo": None},
        ],
    )
    price, note = g.lookup_trend_eur("Charcadet")
    assert price == 0.08  # p25 of 4 sorted values -> index 1
    from lib import price_notes
    assert note == price_notes.CM_ESTIMATE


def test_holo_prefers_holo_trend():
    g = make(
        [{"idProduct": 1, "name": "Gardevoir"}],
        [{"idProduct": 1, "trend": 0.20, "trend-holo": 1.10}],
    )
    price, _ = g.lookup_trend_eur("Gardevoir", is_holo=True)
    assert price == 1.10
    price, _ = g.lookup_trend_eur("Gardevoir", is_holo=False)
    assert price == 0.20


def test_no_match():
    g = make([], [])
    price, note = g.lookup_trend_eur("Missingno")
    assert price is None
