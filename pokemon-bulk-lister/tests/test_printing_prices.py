"""Prices and listing IDs must refer to the requested physical finish."""
import pytest

from lib.tcgplayer_client import TCGPlayerClient
from lib.tcgplayer_catalog import TcgCatalog
from webapp import models


@pytest.fixture
def client():
    return TCGPlayerClient()


@pytest.mark.parametrize('finish,value,without', [('normal', 1, 12), ('holofoil', 12, 1), ('reverseHolofoil', 4, None)])
def test_tcg_uses_the_requested_finish(client, finish, value, without):
    card = {'tcgplayer': {'prices': {k: {'market': v} for k, v in [('normal', 1), ('holofoil', 12), ('reverseHolofoil', 4)]}}}
    assert client.market_price(card, printing=finish) == value
    del card['tcgplayer']['prices'][finish]
    # A card with one ordinary finish is that card, however it was read.
    # A reverse holo that is not offered has no price.
    assert client.market_price(card, printing=finish) == without


def test_legacy_holo_flag_does_not_fall_back_to_reverse_or_first_edition(client):
    assert client.market_price({'tcgplayer': {'prices': {'reverseHolofoil': {'market': 2}}}}, prefer_holo=True) is None
    assert client.market_price({'tcgplayer': {'prices': {'holofoil': {'market': 2}}}}, prefer_holo=False) == 2
    assert client.market_price({'tcgplayer': {'prices': {'1stEditionHolofoil': {'market': 999}}}}, prefer_holo=True) is None


@pytest.mark.parametrize('finish,value', [('normal', 10), ('holofoil', 10), ('reverseHolofoil', 3)])
def test_cardmarket_does_not_cross_normal_and_reverse_prices(client, finish, value):
    card = {'cardmarket': {'prices': {'trendPrice': 10, 'reverseHoloTrend': 3}}}
    assert client.cardmarket_trend_eur(card, printing=finish) == value
    card['cardmarket']['prices'] = {'reverseHoloTrend': 3} if finish != 'reverseHolofoil' else {'trendPrice': 10}
    assert client.cardmarket_trend_eur(card, printing=finish) is None


def test_cardmarket_rejects_a_finish_the_product_does_not_offer(client):
    card = {'cardmarket': {'prices': {'trendPrice': 10}}, 'tcgplayer': {'prices': {'reverseHolofoil': {'market': 10}}}}
    assert client.cardmarket_trend_eur(card, prefer_holo=True) is None
    card['tcgplayer']['prices'] = {'normal': {'market': 10}}      # its one ordinary finish
    assert client.cardmarket_trend_eur(card, prefer_holo=True) == 10
    assert client.cardmarket_trend_eur(card, printing='unknown') is None


def test_number_disagreement_cannot_be_reported_as_an_exact_match(client, monkeypatch):
    monkeypatch.setattr(client, '_get', lambda *a, **kw: {'data': [{'number': 'TG05', 'name': 'Pikachu'}]})
    assert client.find_card('Pikachu', card_number='tg5')['number'] == 'TG05'
    assert client.find_card('Pikachu', card_number='TG06') is None


def test_reverse_holo_has_a_distinct_catalog_identity_and_exact_export_id(tmp_path):
    models.init_models(f"sqlite:///{tmp_path / 'printing.sqlite'}")
    session = models.get_session()
    try:
        base = dict(name='Pikachu', set_name='Test', set_code='test', card_number='23')
        plain = models.get_or_create_catalog_card(session, dict(base, printing='normal'))
        holo = models.get_or_create_catalog_card(session, dict(base, printing='holofoil'))
        reverse = models.get_or_create_catalog_card(session, dict(base, printing='reverseHolofoil'))
        session.commit()
        assert len({plain.id, holo.id, reverse.id}) == 3
        assert models.get_or_create_catalog_card(session, dict(base, is_holo=False)).id == plain.id
        assert reverse.printing == 'reverseHolofoil' and not reverse.is_holo
        catalog = TcgCatalog.parse('TCGplayer Id,Product Name,Number,Set Name,Condition\n1,Pikachu,23,Test,Near Mint\n2,Pikachu,23,Test,Near Mint Holofoil\n3,Pikachu,23,Test,Near Mint Reverse Holofoil\n')
        assert catalog.match(dict(base, printing='reverseHolofoil')) == '3'
        assert catalog.match(dict(base, printing='unknown')) is None
    finally:
        session.close()


def test_scheduler_discards_a_quote_when_its_finish_is_unavailable(tmp_path, monkeypatch):
    from webapp import scheduler
    models.init_models(f"sqlite:///{tmp_path / 'stale.sqlite'}")
    session = models.get_session()
    card = models.get_or_create_catalog_card(session, dict(name='Pikachu', set_code='test', card_number='23', printing='holofoil'))
    models.record_price(session, card, dict(final_price=999, tcgplayer_market=999))
    card_id = card.id
    session.commit()
    session.close()
    monkeypatch.setattr(scheduler, '_price_fn', lambda card: dict(final_price=None, tcgplayer_market=None, printing_unavailable=True))
    scheduler.refresh_cards([card_id])
    session = models.get_session()
    try:
        assert session.get(models.CatalogCard, card_id).final_price is None
    finally:
        session.close()


def test_old_sets_and_single_finish_cards_still_get_a_price():
    from lib.tcgplayer_client import TCGPlayerClient
    t = TCGPlayerClient()
    cm = {"prices": {"trendPrice": 1.2, "reverseHoloTrend": 9.0}}
    old = {"tcgplayer": {"prices": {"1stEdition": {"market": 4.0}, "unlimited": {"market": 1.5}}}, "cardmarket": cm}
    old_holo = {"tcgplayer": {"prices": {"unlimitedHolofoil": {"market": 30.0}, "1stEditionHolofoil": {"market": 200.0}}},
                "cardmarket": cm}
    holo_rare = {"tcgplayer": {"prices": {"holofoil": {"market": 0.5}, "reverseHolofoil": {"market": 0.8}}}, "cardmarket": cm}
    common = {"tcgplayer": {"prices": {"normal": {"market": 0.1}, "reverseHolofoil": {"market": 0.3}}}, "cardmarket": cm}
    both = {"tcgplayer": {"prices": {"normal": {"market": 0.1}, "holofoil": {"market": 2.0}}}, "cardmarket": cm}

    assert (t.market_price(old), t.cardmarket_trend_eur(old)) == (1.5, 1.2)                       # unlimited, not 1st edition
    assert (t.market_price(old_holo, prefer_holo=True), t.cardmarket_trend_eur(old_holo, prefer_holo=True)) == (30.0, 1.2)
    # A holo rare has no non-holo printing: read as non-holo, it is still that card.
    assert (t.market_price(holo_rare), t.cardmarket_trend_eur(holo_rare)) == (0.5, 1.2)
    assert t.market_price(common, prefer_holo=True) == 0.1
    # Where both finishes exist, each keeps its own price.
    assert (t.market_price(both), t.market_price(both, prefer_holo=True)) == (0.1, 2.0)
    # A reverse holo never borrows, and is never borrowed.
    assert t.market_price(old, printing="reverseHolofoil") is None
    assert t.cardmarket_trend_eur(old, printing="reverseHolofoil") == 9.0
    assert t.market_price({"tcgplayer": {"prices": {"reverseHolofoil": {"market": 0.8}}}}) is None
    assert t.market_price({"tcgplayer": {"prices": {"1stEdition": {"market": 40.0}}}}) is None      # a different product
