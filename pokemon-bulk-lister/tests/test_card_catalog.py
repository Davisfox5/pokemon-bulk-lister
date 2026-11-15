"""lib/card_catalog: is what was read off a card a card that exists?"""
import time

import pytest

from lib import card_catalog as cc

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


def test_a_slip_in_the_name_is_forgiven_when_the_number_agrees():
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
