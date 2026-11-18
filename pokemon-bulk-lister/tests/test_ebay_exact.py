"""eBay prices come from listings of the exact same card only: same name,
set, card number, finish, edition or stamp and language, and not graded."""
import json

import pytest

from lib import ebay_client as eb
from lib.ebay_client import CardSpec, reject_reason


def base_charizard(**kw):
    spec = dict(name="Charizard", number="4", total="102", set_name="Base", set_codes=["BS"], finish="holofoil")
    spec.update(kw)
    return CardSpec(**spec)


# ---------------------------------------------------------------- finish

def test_a_holo_listing_never_counts_toward_a_non_holo_card():
    plain = base_charizard(finish="normal")
    assert reject_reason("Charizard 4/102 Base Set Holo Rare Pokemon Card WOTC", plain) == "finish"
    assert reject_reason("Charizard 4/102 Base Set Reverse Holo Pokemon", plain) == "finish"
    assert reject_reason("Charizard 4/102 Base Set Non-Holo Rare Pokemon Card", plain) is None
    assert reject_reason("Charizard 4/102 Base Set Rare Pokemon Card", plain) is None


def test_a_holo_card_needs_holo_and_a_reverse_holo_card_needs_reverse():
    holo = base_charizard()
    assert reject_reason("Charizard 4/102 Base Set Holo Rare WOTC 1999", holo) is None
    assert reject_reason("Charizard 4/102 Base Set Rare WOTC 1999", holo) == "finish"
    assert reject_reason("Charizard 4/102 Base Set Reverse Holo", holo) == "finish"
    assert reject_reason("Charizard 4/102 Base Set Non Holo", holo) == "finish"
    rev = CardSpec(name="Pikachu", number="63", total="198", set_name="Scarlet & Violet", finish="reverseHolofoil")
    assert reject_reason("Pikachu 063/198 Scarlet and Violet Reverse Holo Common NM", rev) is None
    assert reject_reason("Pikachu 063/198 Scarlet & Violet Rev Holo NM", rev) is None
    assert reject_reason("Pikachu 063/198 Scarlet & Violet Holo NM", rev) == "finish"
    assert reject_reason("Pikachu 063/198 Scarlet & Violet Common NM", rev) == "finish"


# ---------------------------------------------------------------- set and number

def test_the_exact_card_number_is_required():
    holo = base_charizard()
    assert reject_reason("Charizard 14/102 Base Set Holo", holo) == "number"
    assert reject_reason("Charizard 4/130 Base Set 2 Holo", holo) == "number"
    assert reject_reason("Charizard Base Set Holo Rare", holo) == "number"
    assert reject_reason("Charizard 004/102 Base Set Holo Rare", holo) is None
    assert reject_reason("Charizard 4 / 102 Base Set Holo Rare", holo) is None
    tg = CardSpec(name="Pikachu VMAX", number="TG17", set_name="Lost Origin", finish="holofoil")
    assert reject_reason("Pikachu VMAX TG17/TG30 Lost Origin Trainer Gallery Holo", tg) is None
    assert reject_reason("Pikachu VMAX TG1/TG30 Lost Origin Trainer Gallery Holo", tg) == "number"
    promo = CardSpec(name="Pikachu", number="SWSH020", set_name="SWSH Black Star Promos", finish="holofoil",
                     editions={"promo"})
    assert reject_reason("Pikachu SWSH020 Black Star Promo Holo SWSH Black Star Promos", promo) is None


def test_the_set_name_or_set_code_is_required():
    obf = CardSpec(name="Charizard ex", number="125", total="197", set_name="Obsidian Flames",
                   set_codes=["OBF"], finish="holofoil")
    assert reject_reason("Charizard ex 125/197 Obsidian Flames Double Rare Holo", obf) is None
    assert reject_reason("Charizard ex 125/197 OBF Holo Pokemon", obf) is None
    assert reject_reason("Charizard ex 125/197 Pokemon Holo Double Rare", obf) == "set"
    assert reject_reason("Charizard ex 125/197 Paldea Evolved Holo", obf) == "set"


def test_the_name_is_required_word_for_word():
    vmax = CardSpec(name="Pikachu V", number="43", total="185", set_name="Vivid Voltage", finish="holofoil")
    assert reject_reason("Pikachu VMAX 43/185 Vivid Voltage Holo", vmax) == "name"
    assert reject_reason("Raichu 43/185 Vivid Voltage Holo", vmax) == "name"


# ---------------------------------------------------------------- editions and stamps

def test_first_edition_and_shadowless_never_count_toward_unlimited():
    unlimited = base_charizard()
    assert reject_reason("1st Edition Charizard 4/102 Base Set Holo Rare", unlimited) == "edition"
    assert reject_reason("Charizard 4/102 Base Set 1st Ed Holo", unlimited) == "edition"
    assert reject_reason("Charizard 4/102 Shadowless Base Set Holo", unlimited) == "edition"
    first = base_charizard(editions={"1st edition"})
    assert reject_reason("1st Edition Charizard 4/102 Base Set Holo Rare", first) is None
    assert reject_reason("Charizard 4/102 Base Set Holo Rare Unlimited", first) == "edition"


def test_stamped_and_promo_versions_count_only_for_that_version():
    plain = CardSpec(name="Pikachu", number="27", total="78", set_name="Pokemon GO", finish="normal")
    assert reject_reason("Pikachu 027/078 Pokemon GO Prerelease Stamp", plain) == "edition"
    assert reject_reason("Pikachu 027/078 Pokemon GO Staff Stamped", plain) == "edition"
    assert reject_reason("Pikachu 027/078 Pokemon GO Promo", plain) == "edition"
    staff = CardSpec(name="Pikachu", number="27", total="78", set_name="Pokemon GO", finish="normal",
                     editions={"staff"})
    assert reject_reason("Pikachu 027/078 Pokemon GO Staff Stamp", staff) is None
    assert reject_reason("Pikachu 027/078 Pokemon GO", staff) == "edition"
    assert eb.editions_in("Pikachu 1st Edition Base Set Shadowless") == {"1st edition", "shadowless"}


# ---------------------------------------------------------------- language

def test_a_japanese_or_other_language_listing_never_counts_toward_an_english_card():
    holo = base_charizard()
    for title in ("Charizard 4/102 Base Set Holo Japanese", "Charizard 4/102 Base Set Holo JP",
                  "Charizard 4/102 Base Set Holo Korean", "Charizard 4/102 Base Set Holo Chinese",
                  "Charizard 4/102 Base Set Holo German Deutsch"):
        assert reject_reason(title, holo) == "language", title


def test_a_japanese_card_is_matched_against_japanese_listings_only():
    ja = CardSpec(name="Pikachu ex", number="45", total="86", set_name="White Flare", set_codes=["sv11w"],
                  finish="holofoil", language="ja")
    assert reject_reason("Pikachu ex 045/086 SV11W White Flare Japanese Holo", ja) is None
    assert reject_reason("Pikachu ex 045/086 White Flare Holo", ja) == "language"
    assert reject_reason("Pikachu ex 045/086 SV11W Korean Holo", ja) == "language"
    assert "japanese" in ja.query()


# ---------------------------------------------------------------- graded

@pytest.mark.parametrize("title", [
    "PSA 9 Charizard 4/102 Base Set Holo", "Charizard 4/102 Base Set Holo CGC 9.5",
    "Charizard 4/102 Base Set Holo BGS 8.5", "Charizard 4/102 Base Set Holo SGC 10",
    "Charizard 4/102 Base Set Holo TAG 10", "Charizard 4/102 Base Set Holo ACE 9",
    "Charizard 4/102 Base Set Holo PSA10", "Charizard 4/102 Base Set Holo Slabbed",
    "Charizard 4/102 Base Set Holo PSA Graded",
])
def test_graded_listings_are_left_out(title):
    assert reject_reason(title, base_charizard()) == "graded"


@pytest.mark.parametrize("title", [
    "Charizard 4/102 Base Set Holo Ungraded Raw NM",
    "Charizard 4/102 Base Set Holo graded by me as NM",
])
def test_ungraded_listings_are_not_mistaken_for_slabs(title):
    assert reject_reason(title, base_charizard()) is None


def test_ace_spec_and_tag_team_cards_are_not_graded():
    assert not eb.GRADED_TITLE_RE.search("Prime Catcher ACE SPEC 157/162 Temporal Forces")
    assert not eb.GRADED_TITLE_RE.search("Pikachu & Zekrom GX TAG TEAM 33/181 Team Up")


# ---------------------------------------------------------------- stats

def _sales(*rows):
    return [{"title": t, "lastSoldPrice": {"value": str(p), "currency": c}} for t, p, c in rows]


def test_the_median_of_exact_matches_only_and_thin_results_are_not_usable():
    spec = base_charizard()
    items = _sales(("Charizard 4/102 Base Set Holo", 300, "USD"),
                   ("Charizard 4/102 Base Set Holo Rare", 320, "USD"),
                   ("Charizard 4/102 Base Set Holo WOTC", 900, "USD"),          # median, not mean
                   ("1st Edition Charizard 4/102 Base Set Holo", 9000, "USD"),
                   ("PSA 9 Charizard 4/102 Base Set Holo", 4000, "USD"),
                   ("Charizard 4/102 Base Set Holo", 250, "GBP"))
    stats = eb.match_stats(items, spec, "lastSoldPrice", spec.query(), sold=True)
    assert (stats["count"], stats["results"], stats["median"], stats["usable"]) == (3, 6, 320.0, True)
    assert "LH_Sold=1" in stats["url"] and "_sacat=183454" in stats["url"]
    thin = eb.match_stats(items[:2], spec, "lastSoldPrice", spec.query(), sold=True)
    assert (thin["count"], thin["usable"], thin["median"]) == (2, False, 310.0)


# ---------------------------------------------------------------- the client

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p = status, payload or {}

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    """Stands in for eBay: no Insights scope, Browse returns listings."""
    def __init__(self, listings, insights=False):
        self.listings, self.insights, self.calls = listings, insights, []

    def post(self, url, data=None, **kw):
        self.calls.append(("token", data["scope"]))
        if "insights" in data["scope"] and not self.insights:
            return _Resp(400, {"error": "invalid_scope"})
        return _Resp(200, {"access_token": "t", "expires_in": 7200})

    def get(self, url, params=None, **kw):
        self.calls.append(("get", url.split("/buy/")[1], params))
        if "marketplace_insights" in url:
            return _Resp(403) if not self.insights else _Resp(200, {"itemSales": []})
        return _Resp(200, {"itemSummaries": self.listings})


def _listings(*rows):
    return [{"title": t, "price": {"value": str(p), "currency": "USD"}} for t, p in rows]


def test_without_insights_access_asking_prices_come_from_the_browse_api():
    client = eb.EbayClient(client_id="id", client_secret="s")
    client._session = _Session(_listings(("Charizard 4/102 Base Set Holo", 350), ("Charizard 4/102 Base Holo", 380),
                                         ("Charizard 4/102 Base Set Holo Rare", 400),
                                         ("Charizard 4/102 Base Set Holo Japanese", 90)))
    out = client.card_stats(base_charizard())
    assert out["sold"] is None and client.insights is False
    assert (out["asking"]["count"], out["asking"]["results"], out["asking"]["median"]) == (3, 4, 380.0)
    assert "LH_BIN=1" in out["asking"]["url"]
    browse = [c for c in client._session.calls if c[0] == "get" and c[1].startswith("browse")][0][2]
    assert browse["category_ids"] == "183454" and "buyingOptions:{FIXED_PRICE}" in browse["filter"]
    assert "priceCurrency:USD" in browse["filter"]
    # The keyset was refused the Insights scope, then given the base one.
    assert [c[1] for c in client._session.calls if c[0] == "token"][-1] == eb.BASE_SCOPE


# ---------------------------------------------------------------- pricing a card

@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICE_REFRESH_ENABLED", "0")
    from webapp import app as mod, db, models, runtime
    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    monkeypatch.setattr(runtime, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "bench.sqlite"))
    db.init_db(mod.DB_PATH)
    db.set_owner(None)
    return mod


def _tcg(bench, price):
    class TCG:
        def lookup_prices(self, **kw):
            return {"card": {"id": "base1-4", "set": {"id": "base1"}}, "tcgplayer_market_usd": price,
                    "cardmarket_trend_eur": None}
    return TCG


def test_holo_cards_are_priced_on_ebay_and_thin_or_asking_prices_stay_out_of_the_price(bench, monkeypatch):
    specs = []

    class Ebay:
        def card_stats(self, spec, **kw):
            specs.append(spec)
            sold = eb.match_stats(_sales(("Charizard 4/102 Base Set Holo", 500, "USD"),
                                         ("Charizard 4/102 Base Set Holo", 520, "USD")),
                                  spec, "lastSoldPrice", spec.query(), sold=True)
            asking = eb.match_stats(_listings(("Charizard 4/102 Base Set Holo", 900),
                                              ("Charizard 4/102 Base Set Holo", 950),
                                              ("Charizard 4/102 Base Set Holo", 990)),
                                    spec, "price", spec.query(), sold=False)
            return {"sold": sold, "asking": asking, "query": spec.query()}

    monkeypatch.setattr(bench, "get_tcg", _tcg(bench, 300.0))
    monkeypatch.setattr(bench, "get_ebay", Ebay)
    card = dict(id=7, crop_path="output/crops/c7.jpg", name="Charizard", set_name="Base", set_code="base1",
                card_number="4", printing="holofoil", is_holo=1, id_confidence=0.97)
    patch = bench._price_card(card)
    assert specs and specs[0].finish == "holofoil"
    # Two sales are too few; asking prices never count. TCGplayer alone prices it.
    assert patch["ebay_median_30d"] is None and patch["ebay_sold_count_30d"] == 2
    assert patch["final_price"] == 300.0
    saved = bench.runtime.state_get(bench._ebay_key(7))
    assert saved["sold"]["count"] == 2 and saved["asking"]["median"] == 950.0
    row = dict(card)
    bench._attach_ebay([row])
    assert row["ebay_match"]["asking"]["count"] == 3
    # A card that has since changed does not show the old match.
    changed = dict(card, card_number="5")
    bench._attach_ebay([changed])
    assert changed["ebay_match"] is None


def test_enough_exact_sales_go_into_the_price(bench, monkeypatch):
    class Ebay:
        def card_stats(self, spec, **kw):
            sold = eb.match_stats(_sales(*[("Charizard 4/102 Base Set Holo", p, "USD") for p in (310, 320, 330)]),
                                  spec, "lastSoldPrice", spec.query(), sold=True)
            return {"sold": sold, "asking": None, "query": spec.query()}

    monkeypatch.setattr(bench, "get_tcg", _tcg(bench, 300.0))
    monkeypatch.setattr(bench, "get_ebay", Ebay)
    patch = bench._price_card(dict(name="Charizard", set_name="Base", set_code="base1", card_number="4",
                                   printing="holofoil", is_holo=1, id_confidence=0.97))
    assert patch["ebay_median_30d"] == 320.0 and patch["ebay_sold_count_30d"] == 3
    assert patch["final_price"] == 310.0          # median of 300 and 320


def test_a_japanese_card_keeps_its_own_set_and_number_for_ebay(bench):
    spec = bench._ebay_spec({"rarity": "Double Rare"}, "Pikachu ex", "White Flare (Japanese)", None, "045",
                            "holofoil", True)
    assert (spec.language, spec.number, spec.total, spec.set_name) == ("ja", "045", "86", "White Flare")
    assert "sv11w" in spec.set_codes
    en = bench._ebay_spec({"rarity": "Promo", "set_name": "SWSH Black Star Promos"}, "Pikachu",
                          "SWSH Black Star Promos", None, "SWSH020", "holofoil", False)
    assert en.editions == {"promo"} and en.language == "en"
