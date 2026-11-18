"""Price notes are short sentences a person can read, including notes saved
by older versions, and a price disagreement always reads the same way."""
import re

import pytest

from lib import price_notes
from lib.pricing import aggregate

TECHNICAL = re.compile(r"—|_|pokemontcg|\bmatch:|\b(sw|sv|swsh|sm|xy|mcd)\d|\d(\.\d)?x\b|×|variant mismatch|\bCM\b|\bTCG\b")
# The one format: sources in a fixed order, each to the cent, then the price used.
FORMAT = re.compile(r"^Prices disagree: (TCGplayer \$[\d,]+\.\d\d)?(, )?(Cardmarket \$[\d,]+\.\d\d)?(, )?"
                    r"(eBay \$[\d,]+\.\d\d)?\.( Using \$[\d,]+\.\d\d\.)?$")


def readable(text):
    assert text and not TECHNICAL.search(text), text
    assert len(re.findall(r"[.!?](\s|$)", text)) <= 4, text
    return text


CARD = [("TCGplayer", 1.06), ("Cardmarket", 7.06), ("eBay", None)]


def test_the_note_from_the_report_reads_plainly():
    stored = ("Cardmarket $7.06 vs TCG $1.06 (7x apart) — dropping CM as likely variant mismatch on "
              "pokemontcg.io; ⚠ name-only match to swsh45 — set_code was not provided, match may not "
              "reflect the actual printing; TCG/CM disagree 6.7x on swsh45 — variant mismatch?; match: no_set")
    assert readable(price_notes.plain(stored, prices=CARD, using=1.06)) == (
        "Prices disagree: TCGplayer $1.06, Cardmarket $7.06. Using $1.06. Set not confirmed. Check the set before listing.")
    # Without the card's prices, the ones the note spelled out are used.
    assert price_notes.plain(stored).startswith("Prices disagree: TCGplayer $1.06, Cardmarket $7.06.")


# Every wording a disagreement has ever been stored in, on the same card.
DISAGREEMENTS = [
    "⚠ TCG/CM disagree 6.7x on swsh45 — variant mismatch?; match: no_set",
    "Cardmarket $7.06 vs TCG $1.06 (7x apart) — dropping CM as likely variant mismatch on pokemontcg.io",
    "TCG $1.06 vs Cardmarket $7.06 (7x apart) — dropping TCG as likely variant mismatch",
    "⚠ TCGplayer and Cardmarket disagree (6.7×) on pgo. Check this printing.",
    "two sources disagree >2.5x (1.06 vs 7.06); using lower",
    "max 7.06 > 2.5x median 1.06; dropped from aggregate",
    "One price was far above the others and was left out.",
    "Prices disagree: TCGplayer $1.06, Cardmarket $7.06.",
    "⚠ Prices disagree. Check the printing.",
    "Prices disagree.",
]


@pytest.mark.parametrize("stored", DISAGREEMENTS)
def test_every_disagreement_reads_exactly_the_same(stored):
    shown = price_notes.plain(stored, prices=CARD, using=1.06)
    assert shown == "Prices disagree: TCGplayer $1.06, Cardmarket $7.06. Using $1.06."
    assert FORMAT.match(shown)


def test_a_flagged_card_with_no_note_still_shows_the_disagreement():
    assert price_notes.plain("", prices=CARD, using=1.06, outlier=True) == (
        "Prices disagree: TCGplayer $1.06, Cardmarket $7.06. Using $1.06.")
    assert price_notes.plain("", prices=CARD) == ""


def test_sources_always_in_the_same_order_with_the_price_used():
    shown = price_notes.disagreement([("eBay", 12.5), ("Cardmarket", 3), ("TCGplayer", 2.999)], using=3.0)
    assert shown == "Prices disagree: TCGplayer $3.00, Cardmarket $3.00, eBay $12.50. Using $3.00."
    assert FORMAT.match(shown)
    assert price_notes.disagreement([("TCGplayer", 1250.0), ("Cardmarket", 980.0)], using=980.0) == (
        "Prices disagree: TCGplayer $1,250.00, Cardmarket $980.00. Using $980.00.")


@pytest.mark.parametrize("stored, shown", [
    ("⚠ The card number was not used (mcd19). This may be a different printing.", price_notes.NUMBER_UNCONFIRMED),
    ("⚠ Matched on the name only (sv2). Check the set before listing.", price_notes.SET_UNCONFIRMED),
    ("⚠ identification needs review; compare the card to its photo", price_notes.CHECK_ID),
    ("⚠ Japanese card: this is the English printing's price, check before listing", price_notes.JAPANESE),
    ("⚠ Finish not checked. Choose non-holo, holo or reverse holo before pricing.", price_notes.FINISH),
    ("no price sources available", price_notes.NO_PRICE),
    ("tcg lookup: 502 Server Error: Bad Gateway for url: https://api.pokemontcg.io/v2/cards?q=x",
     price_notes.LOOKUP_FAILED),
    ("cardmarket bulk-floor estimate (p25 of 6 printings — conservative; verify if this could be a special print)",
     price_notes.CM_ESTIMATE),
    ("tcg lookup: 500 Server Error: Internal Server Error for url: https://api.pokemontcg.io/v2/cards?q=x; "
     "cardmarket bulk-floor estimate (p25 of 79 printings — conservative; verify if this could be a special print)",
     f"{price_notes.LOOKUP_FAILED} {price_notes.CM_ESTIMATE}"),
    ("match: no_number", price_notes.FALLBACK),
    ("From your spreadsheet", "From your spreadsheet."),
])
def test_every_other_stored_wording_becomes_a_plain_sentence(stored, shown):
    assert readable(price_notes.plain(stored)) == shown


def test_at_most_two_sentences_and_the_most_important_first():
    stored = "; ".join([price_notes.CM_ESTIMATE, price_notes.warn(price_notes.NUMBER_UNCONFIRMED),
                        price_notes.warn(price_notes.JAPANESE)])
    assert price_notes.plain(stored) == f"{price_notes.JAPANESE} {price_notes.NUMBER_UNCONFIRMED}"
    assert price_notes.plain("") == "" and price_notes.plain(None) == ""


def test_new_prices_are_noted_in_the_same_format():
    r = aggregate(tcgplayer_market=1.06, ebay_median_30d=None, ebay_max_30d=None,
                  cardmarket_trend_usd=7.06, terapeak_median_usd=None)
    assert r.price == 1.06 and r.notes == "Prices disagree: TCGplayer $1.06, Cardmarket $7.06."
    r = aggregate(tcgplayer_market=1.00, ebay_median_30d=4.00, ebay_max_30d=None,
                  cardmarket_trend_usd=None, terapeak_median_usd=None)
    assert r.price == 1.00 and r.notes == "Prices disagree: TCGplayer $1.00, eBay $4.00."
    r = aggregate(tcgplayer_market=1.0, ebay_median_30d=1.1, ebay_max_30d=None,
                  cardmarket_trend_usd=1.05, terapeak_median_usd=9.0)
    assert r.outlier_flag and r.notes == "Prices disagree: TCGplayer $1.00, Cardmarket $1.05, eBay $9.00."
    for note in (r.notes,):
        assert FORMAT.match(price_notes.plain(note, using=r.price))
    r = aggregate(tcgplayer_market=None, ebay_median_30d=None, ebay_max_30d=None,
                  cardmarket_trend_usd=None, terapeak_median_usd=None)
    assert r.notes == price_notes.NO_PRICE


# ---------------------------------------------------------------- a few cents is not a disagreement

def test_close_prices_are_averaged_not_flagged_whatever_their_ratio():
    # 7 cents vs 30 cents is 4x apart but only 23 cents.
    r = aggregate(tcgplayer_market=0.07, ebay_median_30d=None, ebay_max_30d=None,
                  cardmarket_trend_usd=0.30, terapeak_median_usd=None)
    assert (r.outlier_flag, r.needs_review, r.notes) == (False, False, "")
    assert r.price in (0.18, 0.19) and r.sources["cardmarket_trend_usd"] == 0.30
    # The same 4x on a card worth dollars is a real disagreement.
    r = aggregate(tcgplayer_market=1.00, ebay_median_30d=None, ebay_max_30d=None,
                  cardmarket_trend_usd=4.00, terapeak_median_usd=None)
    assert r.outlier_flag and r.needs_review and r.notes.startswith("Prices disagree")
    # Three sources: a high one only a little above the rest stays in.
    r = aggregate(tcgplayer_market=0.10, ebay_median_30d=0.12, ebay_max_30d=None,
                  cardmarket_trend_usd=0.90, terapeak_median_usd=None)
    assert not r.outlier_flag and r.price == 0.12


def test_pricing_a_card_with_close_sources_raises_no_warning(monkeypatch):
    from webapp import app as bench

    class TCG:
        def lookup_prices(self, **kw):
            return {"card": {"id": "t-23", "set": {"id": "t"}},
                    "tcgplayer_market_usd": 0.07, "cardmarket_trend_eur": 0.30 / bench.EUR_USD_RATE}

    class Ebay:
        def card_stats(self, spec, **kw):
            return {"sold": None, "asking": None, "query": "q"}

    monkeypatch.setattr(bench, "get_tcg", TCG)
    monkeypatch.setattr(bench, "get_ebay", Ebay)
    patch = bench._price_card(dict(name="Pikachu", set_code="t", card_number="23", id_confidence=0.97))
    assert not patch["needs_review"] and "⚠" not in (patch["pricing_notes"] or "")
    assert patch["final_price"] in (0.18, 0.19)


def test_an_older_note_about_a_few_cents_no_longer_says_they_disagree():
    close = [("TCGplayer", 0.07), ("Cardmarket", 0.30)]
    assert price_notes.plain("Prices disagree: TCGplayer $0.07, Cardmarket $0.30.") == price_notes.CLOSE
    old = "⚠ TCG/CM disagree 4.3x on sv1 — variant mismatch?; match: no_number"
    assert price_notes.plain(old, prices=close, using=0.07) == price_notes.CLOSE
    assert price_notes.plain("two sources disagree >2.5x (0.40 vs 2.10); using lower") == price_notes.CLOSE
    # With something else to say, that is what shows.
    assert price_notes.plain(old + "; ⚠ Matched on the name only (sv1).", prices=close) == price_notes.SET_UNCONFIRMED


def test_a_failed_lookup_keeps_the_price_a_card_already_had(monkeypatch):
    from webapp import app as bench

    class Down:
        def lookup_prices(self, **kw):
            raise RuntimeError("500 Server Error")

    class Ebay:
        def card_stats(self, spec, **kw):
            return {"sold": None, "asking": None, "query": "q"}

    monkeypatch.setattr(bench, "get_tcg", Down)
    monkeypatch.setattr(bench, "get_ebay", Ebay)
    monkeypatch.setattr(bench.time, "sleep", lambda s: None)
    monkeypatch.setattr(bench, "CARDMARKET_FALLBACK", False)
    card = dict(name="Dewott", set_code="t", card_number="23", id_confidence=0.97, final_price=0.11)
    assert bench._price_card(card) == {}
    # A card with no price yet is told the lookup failed.
    patch = bench._price_card({**card, "final_price": None})
    assert patch["final_price"] is None and price_notes.plain(patch["pricing_notes"]) == price_notes.LOOKUP_FAILED
