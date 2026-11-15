"""Prices and listing IDs must refer to the requested physical finish."""
import pytest

from lib.tcgplayer_client import TCGPlayerClient
from lib.tcgplayer_catalog import TcgCatalog
from webapp import models


@pytest.fixture
def client():
    return TCGPlayerClient()


@pytest.mark.parametrize('finish,value', [('normal', 1), ('holofoil', 12), ('reverseHolofoil', 4)])
def test_tcg_uses_only_the_requested_finish(client, finish, value):
    card = {'tcgplayer': {'prices': {k: {'market': v} for k, v in [('normal', 1), ('holofoil', 12), ('reverseHolofoil', 4)]}}}
    assert client.market_price(card, printing=finish) == value
    del card['tcgplayer']['prices'][finish]
    assert client.market_price(card, printing=finish) is None


def test_legacy_holo_flag_does_not_fall_back_to_reverse_or_normal(client):
    assert client.market_price({'tcgplayer': {'prices': {'reverseHolofoil': {'market': 2}}}}, prefer_holo=True) is None
    assert client.market_price({'tcgplayer': {'prices': {'holofoil': {'market': 2}}}}, prefer_holo=False) is None
    assert client.market_price({'tcgplayer': {'prices': {'1stEditionHolofoil': {'market': 999}}}}, prefer_holo=True) is None


@pytest.mark.parametrize('finish,value', [('normal', 10), ('holofoil', 10), ('reverseHolofoil', 3)])
def test_cardmarket_does_not_cross_normal_and_reverse_prices(client, finish, value):
    card = {'cardmarket': {'prices': {'trendPrice': 10, 'reverseHoloTrend': 3}}}
    assert client.cardmarket_trend_eur(card, printing=finish) == value
    card['cardmarket']['prices'] = {'reverseHoloTrend': 3} if finish != 'reverseHolofoil' else {'trendPrice': 10}
    assert client.cardmarket_trend_eur(card, printing=finish) is None


def test_cardmarket_rejects_a_finish_the_product_does_not_offer(client):
    card = {'cardmarket': {'prices': {'trendPrice': 10}}, 'tcgplayer': {'prices': {'normal': {'market': 10}}}}
    assert client.cardmarket_trend_eur(card, prefer_holo=True) is None
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
