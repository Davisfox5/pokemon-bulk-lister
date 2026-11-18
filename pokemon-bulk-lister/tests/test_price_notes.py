"""Price notes are short sentences a person can read, including notes saved
by older versions."""
import re

import pytest

from lib import price_notes
from lib.pricing import aggregate

TECHNICAL = re.compile(r"—|_|pokemontcg|\bmatch:|\b(sw|sv|swsh|sm|xy|mcd)\d|\d(\.\d)?x\b|×|variant mismatch|\bCM\b|\bTCG\b")


def readable(text):
    assert text and not TECHNICAL.search(text), text
    assert len(re.findall(r"[.!?](\s|$)", text)) <= 3, text
    return text


def test_the_note_from_the_report_reads_plainly():
    stored = ("Cardmarket $7.06 vs TCG $1.06 (7x apart) — dropping CM as likely variant mismatch on "
              "pokemontcg.io; ⚠ name-only match to swsh45 — set_code was not provided, match may not "
              "reflect the actual printing; TCG/CM disagree 6.7x on swsh45 — variant mismatch?; match: no_set")
    assert readable(price_notes.plain(stored)) == (
        "Prices disagree: TCGplayer $1.06, Cardmarket $7.06. Set not confirmed. Check it before listing.")


@pytest.mark.parametrize("stored, shown", [
    ("⚠ TCG/CM disagree 2.4x on sv3pt5 — variant mismatch?; match: no_number", price_notes.DISAGREE),
    ("TCG $9.10 vs Cardmarket $1.20 (8x apart) — dropping TCG as likely variant mismatch",
     "Prices disagree: TCGplayer $9.10, Cardmarket $1.20."),
    ("⚠ TCGplayer and Cardmarket disagree (3.4×) on pgo. Check this printing.", price_notes.DISAGREE),
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
    ("two sources disagree >2.5x (0.40 vs 4.10); using lower",
     "Prices disagree: One source $0.40, another $4.10."),
    ("two sources disagree >2.5x (0.40 vs 2.10); using lower", price_notes.CLOSE),
    ("match: no_number", price_notes.FALLBACK),
    ("From your spreadsheet", "From your spreadsheet."),
])
def test_every_stored_wording_becomes_a_plain_sentence(stored, shown):
    assert readable(price_notes.plain(stored)) == shown


def test_at_most_two_sentences_and_the_most_important_first():
    stored = "; ".join([price_notes.CM_ESTIMATE, price_notes.warn(price_notes.NUMBER_UNCONFIRMED),
                        price_notes.warn(price_notes.JAPANESE)])
    assert price_notes.plain(stored) == f"{price_notes.JAPANESE} {price_notes.NUMBER_UNCONFIRMED}"
    assert price_notes.plain("") == "" and price_notes.plain(None) == ""


def test_new_prices_are_noted_in_plain_words():
    r = aggregate(tcgplayer_market=1.06, ebay_median_30d=None, ebay_max_30d=None,
                  cardmarket_trend_usd=7.06, terapeak_median_usd=None)
    assert r.price == 1.06 and readable(r.notes) == "Prices disagree: TCGplayer $1.06, Cardmarket $7.06."
    r = aggregate(tcgplayer_market=1.00, ebay_median_30d=4.00, ebay_max_30d=None,
                  cardmarket_trend_usd=None, terapeak_median_usd=None)
    assert r.price == 1.00 and readable(r.notes) == "Prices disagree: TCGplayer $1.00, eBay $4.00."
    r = aggregate(tcgplayer_market=1.0, ebay_median_30d=1.1, ebay_max_30d=None,
                  cardmarket_trend_usd=1.05, terapeak_median_usd=9.0)
    assert readable(r.notes) == price_notes.HIGH_LEFT_OUT
    r = aggregate(tcgplayer_market=None, ebay_median_30d=None, ebay_max_30d=None,
                  cardmarket_trend_usd=None, terapeak_median_usd=None)
    assert r.notes == price_notes.NO_PRICE


def test_an_older_disagreement_is_priced_from_the_cards_own_prices():
    stored = "⚠ TCG/CM disagree 6.7x on swsh45 — variant mismatch?; match: no_set"
    assert price_notes.plain(stored, prices=[("TCGplayer", 1.06), ("Cardmarket", 7.06)]) == (
        "Prices disagree: TCGplayer $1.06, Cardmarket $7.06.")
    # Without both prices it stays short and general.
    assert price_notes.plain(stored, prices=[("TCGplayer", 1.06), ("Cardmarket", None)]) == price_notes.DISAGREE


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
        def build_query(self, **kw):
            return "q"

        def sold_stats(self, **kw):
            return dict(median=None, max=None, count=0)

    monkeypatch.setattr(bench, "get_tcg", TCG)
    monkeypatch.setattr(bench, "get_ebay", Ebay)
    patch = bench._price_card(dict(name="Pikachu", set_code="t", card_number="23", id_confidence=0.97))
    assert not patch["needs_review"] and "⚠" not in (patch["pricing_notes"] or "")
    assert patch["final_price"] in (0.18, 0.19)


def test_an_older_note_about_a_few_cents_no_longer_says_they_disagree():
    assert price_notes.plain("Prices disagree: TCGplayer $0.07, Cardmarket $0.30.") == price_notes.CLOSE
    old = "⚠ TCG/CM disagree 4.3x on sv1 — variant mismatch?; match: no_number"
    assert price_notes.plain(old, prices=[("TCGplayer", 0.07), ("Cardmarket", 0.30)]) == price_notes.CLOSE
    # With something else to say, that is what shows.
    assert price_notes.plain(old + "; ⚠ Matched on the name only (sv1).",
                             prices=[("TCGplayer", 0.07), ("Cardmarket", 0.30)]) == price_notes.SET_UNCONFIRMED
