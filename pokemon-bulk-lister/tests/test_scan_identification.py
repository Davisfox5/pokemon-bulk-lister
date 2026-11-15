"""Regression checks for photo intake, catalog-aware retries and reusable IDs."""
import io
from pathlib import Path

import cv2
import numpy as np
import pytest

from webapp import db, runtime


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv('PRICE_REFRESH_ENABLED', '0')
    from webapp import app as mod
    monkeypatch.setattr(runtime, 'DATA_ROOT', tmp_path)
    for name, folder in [('INPUT_DIR', 'input/grids'), ('OUTPUT_DIR', 'output'),
                         ('CROPS_DIR', 'output/crops'), ('CSVS_DIR', 'output/csvs')]:
        dest = tmp_path / folder
        dest.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mod, name, dest)
    monkeypatch.setattr(mod, 'DB_PATH', str(tmp_path / 'bench.sqlite'))
    db.init_db(mod.DB_PATH)
    db.set_owner(None)
    monkeypatch.setattr(mod, 'ART_MATCH_ENABLED', False)
    return mod


def add_cards(bench, count):
    with db.connect(bench.DB_PATH) as conn:
        gid = db.get_or_create_grid(conn, 'photo', 'input/grids/photo.jpg')
        for i in range(count):
            path = bench.CROPS_DIR / f'card{i}.jpg'
            cv2.imwrite(str(path), np.full((671, 480, 3), i + 30, np.uint8))
            db.insert_card_stub(conn, gid, runtime.rel_path(path), i // 3, i % 3)
        return db.list_cards(conn, sort='oldest'), db.get_grid(conn, gid)


def result(name='Pikachu', confidence=0.99, **extra):
    return dict(name=name, set_name='Test', set_code='test', card_number='23',
                confidence=confidence, **extra)


def test_catalog_rejection_retries_even_when_the_model_is_confident(bench, monkeypatch):
    from lib import vision_identify as vision
    cards, grid = add_cards(bench, 3)
    monkeypatch.setattr(vision, 'identify_page', lambda *a, **k: [result('Wrong', row=0, col=i) for i in range(3)])
    retries = []
    def crop(path):
        retries.append(path)
        return result('Correct', confidence=0.8)
    monkeypatch.setattr(vision, 'identify_crop', crop)
    monkeypatch.setattr(bench, '_checked_identity', lambda fields, **kw: (
        {'id_confidence': 0.97 if fields['name'] == 'Correct' else 0.3},
        'verified' if fields['name'] == 'Correct' else 'unverified'))
    tally = dict(done=0, by_image=0, by_ai=0)
    bench._identify_group(cards, grid, tally)
    assert len(retries) == 3
    with db.connect(bench.DB_PATH) as conn:
        assert {c['name'] for c in db.list_cards(conn)} == {'Correct'}


def test_large_photos_use_bounded_sheets_of_only_unresolved_crops(bench, monkeypatch):
    from lib import vision_identify as vision
    cards, grid = add_cards(bench, 17)
    calls = []
    def page(path, rows=3, cols=3):
        calls.append((rows, cv2.imread(path).shape))
        return [result(row=r, col=c) for r in range(rows) for c in range(cols)]
    monkeypatch.setattr(vision, 'identify_page', page)
    monkeypatch.setattr(vision, 'identify_crop', lambda p: pytest.fail('No crop retry needed'))
    monkeypatch.setattr(bench, '_checked_identity', lambda fields, **kw: ({'id_confidence': 0.97}, 'verified'))
    tally = dict(done=0, by_image=0, by_ai=0)
    bench._identify_group(cards, grid, tally)
    assert len(calls) == 2 and all(rows <= 3 for rows, shape in calls)
    assert tally == dict(done=17, by_image=0, by_ai=17)


def test_offline_catalog_flags_the_card_without_spending_on_a_pointless_retry(bench, monkeypatch):
    from lib import vision_identify as vision
    cards, grid = add_cards(bench, 3)
    monkeypatch.setattr(vision, 'identify_page', lambda *a, **k: [result(row=0, col=i) for i in range(3)])
    monkeypatch.setattr(vision, 'identify_crop', lambda p: pytest.fail('An offline catalog is not an image problem'))
    monkeypatch.setattr(bench, '_checked_identity', lambda fields, **kw: ({}, 'unknown'))
    bench._identify_group(cards, grid, dict(done=0, by_image=0, by_ai=0))
    with db.connect(bench.DB_PATH) as conn:
        assert all(c['needs_review'] and c['id_confidence'] <= 0.6 for c in db.list_cards(conn))


def test_learned_cache_keeps_catalog_corrections_and_rejects_unverified_answers(bench, monkeypatch):
    from lib import art_match
    monkeypatch.setattr(bench, 'ART_MATCH_ENABLED', True)
    monkeypatch.setattr(art_match, 'index_descriptor', lambda path: 'descriptor')
    learned = []
    class Cache:
        def add(self, descriptor, meta):
            learned.append(meta)
    monkeypatch.setattr(bench, '_art_matchers', lambda: (None, Cache()))
    raw = result('Wrong', confidence=0.99)
    checked = dict(name='Correct', set_name='Real Set', set_code='real', card_number='42', id_confidence=0.97, _catalog_status='verified')
    bench._remember_identification(Path('unused.jpg'), raw, dict(checked, _catalog_status='unknown'))
    assert learned == []
    bench._remember_identification(Path('unused.jpg'), raw, checked)
    assert learned[0]['name'] == 'Correct' and learned[0]['card_number'] == '42'


def test_unreadable_photo_does_not_discard_the_rest_of_an_upload(bench):
    ok, photo = cv2.imencode('.jpg', np.full((671, 480, 3), 120, np.uint8))
    response = bench.app.test_client().post('/api/grids/upload', data={
        'file': [(io.BytesIO(b'not a photo'), 'broken.jpg'), (io.BytesIO(photo.tobytes()), 'good.jpg')],
        'mode': 'single',
    }, content_type='multipart/form-data')
    assert response.status_code == 200
    data = response.get_json()
    assert data['total_crops'] == 1 and data['skipped'] == ['broken.jpg']
    assert data['errors'][0]['filename'] == 'broken.jpg'
    assert not (bench.INPUT_DIR / 'broken.jpg').exists()


@pytest.mark.parametrize('confidence,drop_number', [(0.3, False), (0.97, True)])
def test_pricing_keeps_identity_and_printing_disagreements_visible(bench, monkeypatch, confidence, drop_number):
    class TCG:
        def lookup_prices(self, **kw):
            if drop_number and kw.get('card_number'):
                return {}
            return {'card': {'id': 'test-23', 'set': {'id': 'test'}},
                    'tcgplayer_market_usd': 10, 'cardmarket_trend_eur': 10 / bench.EUR_USD_RATE}
    class Ebay:
        def build_query(self, **kw):
            return 'query'
        def sold_stats(self, **kw):
            return dict(median=10, max=10, count=50)
    monkeypatch.setattr(bench, 'get_tcg', TCG)
    monkeypatch.setattr(bench, 'get_ebay', Ebay)
    patch = bench._price_card(dict(name='Pikachu', set_code='test', card_number='23', id_confidence=confidence))
    assert patch['needs_review'] and patch['pricing_confidence'] <= 0.2
    assert '⚠' in patch['pricing_notes']
    assert ('collector number' if drop_number else 'identification needs review') in patch['pricing_notes']
