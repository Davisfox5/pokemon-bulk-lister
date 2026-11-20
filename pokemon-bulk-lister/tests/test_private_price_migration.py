"""Old shared sheet values are quarantined, not reassigned to an arbitrary owner."""
import json
from webapp import models


def test_shared_legacy_estimate_is_preserved_but_not_used_for_market_prices(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.sqlite'}"
    models.init_models(url)
    session = models.get_session()
    card = models.get_or_create_catalog_card(session, dict(name='Pikachu', set_code='test', card_number='23'))
    models.record_price(session, card, dict(final_price=999, pricing_confidence=0.5, pricing_notes='From your spreadsheet (old file)'))
    card_id = card.id
    session.commit()
    session.close()
    models.init_models(url)
    session = models.get_session()
    try:
        card = session.get(models.CatalogCard, card_id)
        assert card.final_price is None and card.last_priced_at is None
        assert session.query(models.PriceSnapshot).count() == 0
        saved = json.loads(session.get(models.AppState, f'quarantined-sheet-price:{card_id}').value)
        assert saved['price'] == 999 and saved['history'][0]['price'] == 999
    finally:
        session.close()
    models.init_models(url)  # safe on every subsequent startup
    session = models.get_session()
    try:
        assert session.query(models.AppState).filter(models.AppState.key.like('quarantined-sheet-price:%')).count() == 1
    finally:
        session.close()
