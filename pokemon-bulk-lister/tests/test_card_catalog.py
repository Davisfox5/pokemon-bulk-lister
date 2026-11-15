"""lib/card_catalog: is what was read off a card a card that exists?"""
import time

import pytest

from lib import card_catalog as cc
card_catalog = cc

SETS = [
    {"id": "sv8", "name": "Surging Sparks", "ptcgoCode": "SSP", "printedTotal": 191, "releaseDate": "2024/11/08"},
    {"id": "me55", "name": "30th Celebration", "ptcgoCode": "30C", "printedTotal": 128, "releaseDate": "2026/09/16"},
    {"id": "old128", "name": "An Older Set Of 128", "ptcgoCode": "OLD", "printedTotal": 128, "releaseDate": "2010/01/01"},
]
CARDS = {
    "me55": [{"id": "me55-23", "name": "Pikachu", "number": "23", "rarity": "Pikachu Rare"},
             {"id": "me55-108", "name": "Gholdengo", "number": "108", "rarity": "Common"},
             {"id": "me55-155", "name": "Jirachi ex", "number": "155", "rarity": "Special Illustration Rare"},
             {"id": "me55-64", "name": "Mewtwo ex", "number": "64", "rarity": "Double Rare"}],
    "sv8": [{"id": "sv8-57", "name": "Pikachu ex", "number": "57", "rarity": "Double Rare"}],
    "old128": [{"id": "old128-23", "name": "Geodude", "number": "23", "rarity": "Common"}],
}


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    box = {}
    monkeypatch.setattr(cc, "_store", type("S", (), {"get": staticmethod(box.get),
                                                    "set": staticmethod(box.__setitem__)})())
    monkeypatch.setattr(cc, "_bundled_sets", lambda: [])
    calls = []

    def fetch(path, params):
        calls.append((path, params.get("q")))
        if path == "/sets":
            return {"data": SETS, "totalCount": len(SETS)}
        rows = CARDS.get((params.get("q") or "").split(":")[-1], [])
        return {"data": rows, "totalCount": len(rows)}

    monkeypatch.setattr(cc, "_fetch", fetch)
    return calls


def test_name_and_number_over_total_find_the_card_and_its_set():
    r = cc.resolve("Pikachu", "023", total="128", printed_code="30C")
    assert r["status"] == "verified"
    assert r["card"] == {"name": "Pikachu", "set_name": "30th Celebration", "set_code": "me55",
                         "card_number": "23", "rarity": "Pikachu Rare"}
    # The total alone is enough, written either way; the newer of two sets that size is tried first.
    assert cc.resolve("Pikachu", "023/128")["card"]["set_code"] == "me55"
    assert cc.resolve("Geodude", "23", total=128)["card"]["set_code"] == "old128"


def test_a_printed_code_does_not_borrow_a_card_from_another_set_of_the_same_size():
    # 30C and the older set are both 128 cards. Geodude #23 is only in the older one.
    r = cc.resolve("Geodude", "23", total=128, printed_code="30C")
    assert r["status"] == "unverified"
    r = cc.resolve("Goldengo", "108", total=128)
    assert r["status"] == "verified" and r["card"]["name"] == "Gholdengo"


def test_a_wrong_name_or_a_set_remembered_wrongly_is_not_verified():
    assert cc.resolve("Charizard ex", "155", total=128)["status"] == "unverified"      # that number is Jirachi ex
    wrong_set = cc.resolve("Pikachu", "60", set_code="sv8", set_name="Surging Sparks")
    assert wrong_set["status"] == "unverified" and "Surging Sparks" in wrong_set["why"]
    made_up = cc.resolve("Eevee", "55", set_code="sv04pt", set_name="Made Up Set")
    assert made_up["status"] == "unverified" and "no set fits" in made_up["why"]


def test_a_misread_number_is_only_called_probable():
    r = cc.resolve("Mewtwo ex", "230", total=128)
    assert r["status"] == "probable" and r["card"]["card_number"] == "64" and "64" in r["why"]


def test_card_lists_are_fetched_once_then_kept(catalog):
    cc.resolve("Pikachu", "23", total=128)
    cc.resolve("Jirachi ex", "155", total=128)
    assert [c for c in catalog if c[0] == "/cards"] == [("/cards", "set.id:me55")]


def test_nothing_is_decided_when_the_catalog_cannot_be_reached(monkeypatch):
    cc.sets()                                           # the set list is already in hand

    def down(path, params):
        raise RuntimeError("card catalog unreachable")

    monkeypatch.setattr(cc, "_fetch", down)
    assert cc.resolve("Pikachu", "23", total=128)["status"] == "unknown"
    assert [s["id"] for s in cc.sets()] == ["old128", "sv8", "me55"]       # still served from the cache


def test_the_prompt_lists_the_newest_sets_first():
    text = cc.prompt_reference(2)
    assert text.splitlines()[0].startswith("30th Celebration | id me55 | printed code 30C | 128 cards")
    assert len(text.splitlines()) == 2


# ---------------------------------------------------------------- Japanese cards

def test_japanese_names_come_from_a_table_not_a_model():
    assert card_catalog.english_name("ガントル") == "Boldore"
    assert card_catalog.english_name("モンメン") == "Cottonee"
    assert card_catalog.english_name("ロケット団のニドラン♂") == "Team Rocket's Nidoran♂"
    assert card_catalog.english_name("カットロトム") == "Mow Rotom"
    assert card_catalog.english_name("ヒスイダイケンキ") == "Hisuian Samurott"
    assert card_catalog.english_name("ピカチュウex") == "Pikachu ex"
    assert card_catalog.english_name("ロケット団のサカキ") is None          # a trainer: not in the table
    assert card_catalog.is_japanese("ダゲキ") and not card_catalog.is_japanese("Sawk")


def test_a_japanese_card_is_named_and_filed_as_japanese():
    found = card_catalog.resolve_japanese("ロケット団のドガース", name_read="Koffing", number="069", total="098",
                                          printed_code="sv10")
    assert found["status"] == "probable"
    assert found["card"] == {"name": "Team Rocket's Koffing", "set_name": "Glory of Team Rocket (Japanese)",
                             "set_code": "", "card_number": "69", "rarity": None}
    # A trainer keeps the name the reader gave it.
    found = card_catalog.resolve_japanese("ロケット団のサカキ", name_read="Team Rocket's Giovanni", number="093",
                                          printed_code="sv10")
    assert found["status"] == "unknown" and found["card"]["name"] == "Team Rocket's Giovanni"
    assert found["card"]["set_name"] == "Glory of Team Rocket (Japanese)"


def test_a_japanese_card_is_verified_against_an_english_twin_set(monkeypatch):
    monkeypatch.setattr(card_catalog, "set_cards", lambda set_id: [
        {"id": "rsv10pt5-4", "name": "Cottonee", "number": "4", "rarity": "Common"},
        {"id": "rsv10pt5-5", "name": "Whimsicott", "number": "5", "rarity": "Uncommon"}] if set_id == "rsv10pt5" else [])
    found = card_catalog.resolve_japanese("モンメン", name_read="Munna", number="012", total="086", printed_code="sv11W")
    assert found["status"] == "verified"         # the English set numbers it 4; the Japanese number is kept
    assert found["card"] == {"name": "Cottonee", "set_name": "White Flare (Japanese)", "set_code": "",
                             "card_number": "12", "rarity": "Common"}
    monkeypatch.setattr(card_catalog, "set_cards", lambda set_id: [
        {"id": "a", "name": "Team Rocket's Nidoran♀", "number": "110", "rarity": "Common"},
        {"id": "b", "name": "Team Rocket's Nidoran♂", "number": "114", "rarity": "Common"}])
    found = card_catalog.resolve_japanese("ロケット団のニドラン♂", number="061", printed_code="sv10")
    assert found["card"]["name"] == "Team Rocket's Nidoran♂"
    monkeypatch.setattr(card_catalog, "set_cards", lambda set_id: [
        {"id": "full", "name": "Cottonee", "number": "186", "rarity": "Illustration Rare"},
        {"id": "rsv10pt5-4", "name": "Cottonee", "number": "4", "rarity": "Common"}])
    found = card_catalog.resolve_japanese("モンメン", number="012", printed_code="sv11W")
    assert found["card"]["rarity"] == "Common"
    # A name the English set does not have is still named, not verified.
    found = card_catalog.resolve_japanese("ダゲキ", number="046", printed_code="sv11W")
    assert found["status"] == "probable" and found["card"]["name"] == "Sawk"
    # A trainer the table cannot name is never matched by number alone.
    found = card_catalog.resolve_japanese("古びたはねの化石", name_read="Plume Fossil", number="005", printed_code="sv11W")
    assert found["status"] == "unknown" and found["card"]["name"] == "Plume Fossil"
    assert card_catalog.japanese_set_named("White Flare (Japanese)")["twin"] == "rsv10pt5"


def test_a_set_list_comes_from_the_mirror_when_the_api_is_down(monkeypatch):
    saved = {}
    monkeypatch.setattr(card_catalog, "_store", type("S", (), {"get": lambda self, k: saved.get(k),
                                                               "set": lambda self, k, v: saved.__setitem__(k, v)})())
    def down(*a, **k):
        raise RuntimeError("card catalog unreachable")
    monkeypatch.setattr(card_catalog, "_all_pages", down)
    monkeypatch.setattr(card_catalog, "_mirror_cards", lambda set_id: [{"id": "x-1", "name": "Fennekin", "number": "25", "rarity": "Common"}])
    assert card_catalog.set_cards("swsh12")[0]["name"] == "Fennekin"
    assert saved["set-cards:swsh12"]["cards"][0]["number"] == "25"
