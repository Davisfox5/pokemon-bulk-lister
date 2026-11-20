"""Deck logic: parsing, library matching, popular-deck aggregation."""
import json

from lib import decks

SC = decks.SetCodes({
    "TWM": {"name": "Twilight Masquerade", "ids": ["sv6"]},
    "SVI": {"name": "Scarlet & Violet", "ids": ["sv1"]},
    "PAF": {"name": "Paldean Fates", "ids": ["sv4pt5"]},
})

LIST = """Pokémon: 3
4 Dragapult ex TWM 130
2 Pikachu SVI 63

Trainer: 2
4 Ultra Ball SVI 196
3 Boss's Orders PAL 172

Energy: 1
6 Basic {R} Energy SVE 2

Total Cards: 19
"""


def test_parse_reads_sections_counts_sets_and_numbers():
    parsed = decks.parse_decklist(LIST)
    assert [c["section"] for c in parsed["cards"]] == [
        "pokemon", "pokemon", "trainer", "trainer", "energy"]
    first = parsed["cards"][0]
    assert first == {"section": "pokemon", "count": 4, "name": "Dragapult ex",
                     "set": "TWM", "number": "130"}
    assert parsed["cards"][3]["name"] == "Boss's Orders"
    assert parsed["total"] == 19
    assert any("19 cards" in w for w in parsed["warnings"])


def test_parse_handles_name_only_lines_digit_set_codes_and_junk():
    parsed = decks.parse_decklist("2 Ultra Ball\n1 Mew ex 30C 12\nnot a card\n")
    assert parsed["cards"][0]["set"] is None
    assert parsed["cards"][0]["name"] == "Ultra Ball"
    assert (parsed["cards"][1]["set"], parsed["cards"][1]["number"]) == ("30C", "12")
    assert any("skipped" in w for w in parsed["warnings"])


def test_basic_energy_spellings_normalise_to_one_name():
    names = ["Basic {R} Energy", "Basic Fire Energy", "Fire Energy", "fire  energy"]
    assert {decks.norm_name(n) for n in names} == {"fire energy"}
    assert decks.is_basic_energy("Basic {P} Energy")
    assert not decks.is_basic_energy("Mist Energy")
    assert decks.norm_name("Pokémon Catcher") == "pokemon catcher"
    assert decks.norm_name("Boss’s Orders") == "boss's orders"


def test_set_codes_translate_and_match_by_id_or_name():
    assert SC.ids("twm") == ["sv6"]
    assert SC.code_for("sv6") == "TWM"
    assert SC.same_set("TWM", "sv6")
    assert SC.same_set("TWM", None, "Twilight Masquerade")
    assert not SC.same_set("TWM", "sv1", "Scarlet & Violet")


def _card(section, count, name, set_code=None, number=None):
    return {"section": section, "count": count, "name": name, "set": set_code, "number": number}


def test_match_counts_exact_printings_and_reports_missing():
    owned = [{"name": "Dragapult ex", "set_code": "sv6", "card_number": "130", "quantity": 3}]
    m = decks.match_deck([_card("pokemon", 4, "Dragapult ex", "TWM", "130")], owned, SC)
    c = m["cards"][0]
    assert (c["have_exact"], c["have_other"], c["missing"]) == (3, 0, 1)
    assert m["completion_pct"] == 75.0


def test_match_any_printing_counts_for_trainers_and_energy():
    owned = [
        {"name": "Ultra Ball", "set_code": "sv4pt5", "card_number": "91", "quantity": 2},
        {"name": "Basic Fire Energy", "set_code": "sve", "card_number": "2", "quantity": 10},
    ]
    m = decks.match_deck([
        _card("trainer", 4, "Ultra Ball", "SVI", "196"),
        _card("energy", 6, "Fire Energy", "MEE", "10"),
    ], owned, SC)
    assert (m["cards"][0]["have_other"], m["cards"][0]["missing"]) == (2, 2)
    assert (m["cards"][1]["have"], m["cards"][1]["missing"]) == (6, 0)


def test_match_same_name_pokemon_from_another_set_is_only_a_maybe():
    owned = [{"name": "Pikachu", "set_code": "sv6", "card_number": "57", "quantity": 4}]
    m = decks.match_deck([_card("pokemon", 2, "Pikachu", "SVI", "63")], owned, SC)
    c = m["cards"][0]
    assert (c["have"], c["maybe"], c["missing"]) == (0, 2, 2)
    assert m["maybe"] == 2


def test_match_same_set_different_number_pokemon_is_not_assumed_to_be_the_card():
    # A set often prints two different Basics with one name (Tinkatink PAL
    # 101 and 102), so a different number never counts on its own.
    owned = [{"name": "Dragapult ex", "set_code": "sv6", "card_number": "200", "quantity": 1}]
    m = decks.match_deck([_card("pokemon", 2, "Dragapult ex", "TWM", "130")], owned, SC)
    c = m["cards"][0]
    assert (c["have"], c["maybe"], c["missing"]) == (0, 1, 2)


def test_match_offers_a_stray_copy_to_one_line_only():
    owned = [{"name": "Ralts", "set_code": "sv1", "card_number": "84", "quantity": 1}]
    m = decks.match_deck([
        _card("pokemon", 2, "Ralts", "TWM", "60"),
        _card("pokemon", 2, "Ralts", "PAF", "27"),
    ], owned, SC)
    assert m["maybe"] == 1 and m["have"] == 0


def test_match_name_only_pokemon_line_is_a_maybe_but_a_trainer_counts():
    owned = [
        {"name": "Pikachu", "set_code": "sv6", "card_number": "57", "quantity": 1},
        {"name": "Ultra Ball", "set_code": "sv1", "card_number": "196", "quantity": 1},
    ]
    m = decks.match_deck([_card("pokemon", 1, "Pikachu"), _card("trainer", 1, "Ultra Ball")], owned, SC)
    assert (m["cards"][0]["have"], m["cards"][0]["maybe"]) == (0, 1)
    assert m["cards"][1]["have"] == 1


def test_list_without_headings_only_counts_exact_printings():
    parsed = decks.parse_decklist("4 Ultra Ball SVI 196\n2 Fire Energy MEE 10\n", SC)
    assert [c["section"] for c in parsed["cards"]] == ["unknown", "energy"]
    assert any("headings" in w for w in parsed["warnings"])
    other = [{"name": "Ultra Ball", "set_code": "sv4pt5", "card_number": "91", "quantity": 4}]
    m = decks.match_deck(parsed["cards"], other, SC)
    assert (m["cards"][0]["have"], m["cards"][0]["maybe"]) == (0, 4)
    exact = [{"name": "Ultra Ball", "set_code": "sv1", "card_number": "196", "quantity": 4}]
    assert decks.match_deck(parsed["cards"], exact, SC)["cards"][0]["have"] == 4


def test_parse_does_not_mistake_name_suffixes_for_set_codes():
    parsed = decks.parse_decklist("Pokémon: 2\n2 Charizard EX 12\n1 Mewtwo VSTAR 31\n", SC)
    assert [(c["name"], c["set"]) for c in parsed["cards"]] == [
        ("Charizard EX 12", None), ("Mewtwo VSTAR 31", None)]


def test_parse_accepts_the_number_and_marker_variants_sites_use():
    text = "\n".join([
        "12 Pokémon",
        "1 Pikachu V PR-SW SWSH061",
        "1 Mimikyu CEL 97A",
        "1 Bloodmoon Ursaluna ex PRE 168/131",
        "3 Nest Ball svi 181",          # known code in lower case
        "* 4 Arven SVI 166",
        "4 x Iono PAL 185",
        "2 Xatu PAR 72",
        "## Energy - 2",
        "2 Fire Energy MEE 10",
    ])
    cards = decks.parse_decklist(text, SC)["cards"]
    assert [(c["count"], c["name"], c["set"], c["number"]) for c in cards] == [
        (1, "Pikachu V", "PR-SW", "SWSH061"),
        (1, "Mimikyu", "CEL", "97A"),
        (1, "Bloodmoon Ursaluna ex", "PRE", "168"),
        (3, "Nest Ball", "SVI", "181"),
        (4, "Arven", "SVI", "166"),
        (4, "Iono", "PAL", "185"),
        (2, "Xatu", "PAR", "72"),
        (2, "Fire Energy", "MEE", "10"),
    ]
    assert [c["section"] for c in cards] == ["pokemon"] * 7 + ["energy"]


def test_match_never_spends_one_copy_twice():
    owned = [{"name": "Ultra Ball", "set_code": "sv1", "card_number": "196", "quantity": 3}]
    m = decks.match_deck([
        _card("trainer", 2, "Ultra Ball", "SVI", "196"),
        _card("trainer", 2, "Ultra Ball", "PAF", "91"),
    ], owned, SC)
    assert m["have"] == 3 and m["missing"] == 1


def test_format_decklist_missing_only_is_a_shopping_list():
    m = decks.match_deck([
        _card("pokemon", 4, "Dragapult ex", "TWM", "130"),
        _card("trainer", 1, "Ultra Ball"),
    ], [{"name": "Ultra Ball", "set_code": "sv1", "card_number": "196", "quantity": 1}], SC)
    assert decks.format_decklist(m["cards"], only_missing=True) == "4 Dragapult ex TWM 130"


def _standing(placing, deck_id, deck_name, name="Dragapult ex"):
    return {
        "name": f"player{placing}", "placing": placing,
        "record": {"wins": 5, "losses": 1, "ties": 0},
        "deck": {"id": deck_id, "name": deck_name},
        "decklist": {"pokemon": [{"count": 4, "name": name, "set": "TWM", "number": "130"}],
                     "trainer": [], "energy": []},
    }


def test_aggregate_ranks_by_share_and_keeps_the_best_placing_list():
    t = {"id": "t1", "name": "Big One", "date": "2026-11-07T16:00:00.000Z", "players": 100}
    standings = [
        _standing(1, "a", "Alpha"), _standing(2, "b", "Beta"), _standing(3, "b", "Beta"),
        _standing(4, "other", "Other"),
        {"name": "nolist", "placing": 5, "deck": {"id": "b", "name": "Beta"}, "decklist": None},
    ]
    out = decks.aggregate_popular([(t, standings)], top_n=5)
    assert [d["id"] for d in out] == ["b", "a"]
    beta = out[0]
    assert beta["decks_entered"] == 2 and beta["share_pct"] == 66.7
    assert beta["sample"]["placing"] == 2
    assert beta["sample"]["tournament"] == "Big One"
    assert beta["sample"]["tournament_date"] == "2026-11-07"
    assert beta["sample"]["record"] == "5-1-0"


def test_fetch_popular_uses_the_biggest_tournaments():
    class FakeClient:
        def tournaments(self, fmt, limit):
            return [{"id": "small", "players": 8, "name": "s", "date": "2026-11-01T00:00:00Z"},
                    {"id": "big", "players": 300, "name": "b", "date": "2026-11-02T00:00:00Z"}]

        def standings(self, tid):
            self.asked = getattr(self, "asked", []) + [tid]
            return [_standing(1, "a", "Alpha")]

    client = FakeClient()
    data = decks.fetch_popular(client=client, n_tournaments=1, pause=0)
    assert client.asked == ["big"]
    assert data["decks"][0]["name"] == "Alpha"
    assert data["tournaments"][0]["players"] == 300
    assert data["fetched_at"].endswith("Z")


def test_load_popular_prefers_the_refreshed_cache(tmp_path):
    cache = tmp_path / "popular.json"
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps({"decks": [{"id": "old"}]}))
    assert decks.load_popular(cache, bundled)["origin"] == "bundled"
    decks.save_popular({"decks": [{"id": "new"}]}, cache)
    data = decks.load_popular(cache, bundled)
    assert (data["origin"], data["decks"][0]["id"]) == ("refreshed", "new")
    assert decks.load_popular(tmp_path / "nope", tmp_path / "nope2")["origin"] == "none"


def test_bundled_data_files_are_usable():
    sc = decks.SetCodes.load()
    assert sc.ids("TWM") == ["sv6"]
    data = decks.load_popular()
    assert data["origin"] == "bundled" and len(data["decks"]) >= 5
    for d in data["decks"]:
        assert sum(c["count"] for c in d["cards"]) == 60, d["name"]
        assert d["sample"]["tournament"] and d["sample"]["tournament_date"]


def test_search_links_are_plain_search_urls():
    links = decks.search_links("Boss's Orders", "PAL", "172")
    assert links["tcgplayer"].startswith("https://www.tcgplayer.com/search/pokemon/product?q=Boss%27s+Orders")
    assert "ebay.com/sch" in links["ebay"] and "pokemon" in links["ebay"]
