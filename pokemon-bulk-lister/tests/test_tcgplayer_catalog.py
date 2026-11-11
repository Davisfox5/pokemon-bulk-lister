"""Real TCGplayer Ids from the seller's own TCGplayer export."""
import importlib.util
from pathlib import Path

import pytest

from lib.tcgplayer_catalog import TcgCatalog

EXPORT = """TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,Condition,TCG Market Price,Total Quantity,Add to Quantity,TCG Marketplace Price
7001,Pokemon,SV06: Twilight Masquerade,Dragapult ex,,130/167,Double Rare,Near Mint Holofoil,14.50,0,0,
7002,Pokemon,SV06: Twilight Masquerade,Dragapult ex,,130/167,Double Rare,Lightly Played Holofoil,12.00,0,0,
7003,Pokemon,SV06: Twilight Masquerade,Dreepy,,128/167,Common,Near Mint,0.10,0,0,
7004,Pokemon,SV06: Twilight Masquerade,Dreepy,,128/167,Common,Near Mint Reverse Holofoil,0.30,0,0,
7005,Pokemon,SV01: Scarlet & Violet Base Set,Pikachu,,063/198,Common,Near Mint,0.15,0,0,
7006,Pokemon,SV06: Twilight Masquerade,Pikachu,,063/167,Common,Near Mint,0.20,0,0,
7007,Pokemon,SV06: Twilight Masquerade,Munkidori,,095/167,Rare,Near Mint Holofoil,0.40,0,0,
7008,Pokemon,SV06: Twilight Masquerade,Munkidori,,095/167,Rare,Near Mint Reverse Holofoil,0.60,0,0,
9001,Magic,Some Set,Dragapult ex,,130,Rare,Near Mint,1.00,0,0,
"""


def _card(name, number, set_name="Twilight Masquerade", cond="NM", holo=False, price=1.0):
    return {"name": name, "card_number": number, "set_name": set_name,
            "condition_guess": cond, "is_holo": holo, "price": price, "rarity": ""}


def test_matches_on_name_number_set_condition_and_printing():
    cat = TcgCatalog.parse(EXPORT)
    assert len(cat) == 8                                            # the Magic row is ignored
    assert cat.match(_card("Dragapult ex", "130", holo=True)) == "7001"
    assert cat.match(_card("Dragapult ex", "130", cond="LP", holo=True)) == "7002"
    assert cat.match(_card("Dreepy", "128")) == "7003"              # not the reverse holo
    assert cat.match(_card("Pikachu", "63", set_name="Scarlet & Violet Base Set")) == "7005"
    assert cat.match(_card("Pikachu", "063")) == "7006"             # same name and number, other set
    assert cat.match(_card("dragapult EX", "130/167", holo=True)) == "7001"


def test_anything_ambiguous_or_absent_is_left_out():
    cat = TcgCatalog.parse(EXPORT)
    assert cat.match(_card("Dragapult ex", "130")) == "7001"        # only one printing exists: holofoil
    assert cat.match(_card("Munkidori", "95")) is None              # non-holo wanted, two foil rows: unclear
    assert cat.match(_card("Munkidori", "95", holo=True)) == "7007"
    assert cat.match(_card("Dragapult ex", "130", cond="HP", holo=True)) is None
    assert cat.match(_card("Dragapult ex", "131", holo=True)) is None
    assert cat.match(_card("Dragapult ex", "130", set_name="Paldea Evolved", holo=True)) is None
    assert cat.match(_card("Charizard ex", "125")) is None
    assert cat.match(_card("", "130")) is None and cat.match(_card("Dreepy", "")) is None


def test_a_file_that_is_not_a_tcgplayer_export_is_refused_with_the_reason():
    with pytest.raises(ValueError, match="no TCGplayer Id"):
        TcgCatalog.parse("SKU,Price\nabc,1\n")
    with pytest.raises(ValueError, match="no Pokemon rows"):
        TcgCatalog.parse("TCGplayer Id,Product Line,Product Name,Number,Condition\n1,Magic,X,1,Near Mint\n")
    assert TcgCatalog.load("/nonexistent/file.csv") is None


def test_upload_rows_carry_real_ids_and_nothing_is_written_without_an_export():
    pytest.importorskip("pandas")
    path = Path(__file__).resolve().parent.parent / "scripts" / "05_generate_csvs.py"
    spec = importlib.util.spec_from_file_location("generate_csvs", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    cards = [_card("Dragapult ex", "130", holo=True, price=14.5), _card("Charizard ex", "125", price=30.0),
             {**_card("Dreepy", "128", price=0.1), "tcgplayer_product_id": "sv6-128"}]
    assert gen.tcgplayer_rows(cards) == []                          # no export: no made-up ids
    rows = gen.tcgplayer_rows(cards, TcgCatalog.parse(EXPORT))
    assert [(r["TCGplayer Id"], r["Product Name"], r["TCG Marketplace Price"]) for r in rows] == [
        ("7001", "Dragapult ex", "14.50"), ("7003", "Dreepy", "0.10")]
