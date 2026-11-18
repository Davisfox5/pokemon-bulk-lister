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
        def card_stats(self, spec, **kw):
            sold = dict(median=10, max=10, min=10, count=50, results=60, usable=True, query='q', url='')
            return {'sold': sold, 'asking': None, 'query': 'q'}
    monkeypatch.setattr(bench, 'get_tcg', TCG)
    monkeypatch.setattr(bench, 'get_ebay', Ebay)
    patch = bench._price_card(dict(name='Pikachu', set_code='test', card_number='23', id_confidence=confidence))
    assert patch['needs_review'] and patch['pricing_confidence'] <= 0.2
    assert '⚠' in patch['pricing_notes']
    from lib import price_notes
    wanted = price_notes.NUMBER_UNCONFIRMED if drop_number else price_notes.CHECK_ID
    assert price_notes.warn(wanted) in patch['pricing_notes'].split('; ')
    assert 'match:' not in patch['pricing_notes'] and '—' not in patch['pricing_notes']


# ---------------------------------------------------------------- which card is this

def _two_sets(monkeypatch):
    from lib import card_catalog as cc
    sets = [{"id": "old128", "name": "Old Set", "printedTotal": 128, "ptcgoCode": "OLD", "releaseDate": "2010/01/01"},
            {"id": "new128", "name": "New Set", "printedTotal": 128, "ptcgoCode": "NEW", "releaseDate": "2024/01/01"}]
    cards = {"old128": [{"id": "o23", "name": "Geodude", "number": "23", "rarity": "Common"},
                        {"id": "o24", "name": "Graveler", "number": "24", "rarity": "Uncommon"}],
             "new128": [{"id": "n23", "name": "Geodude", "number": "23", "rarity": "Common"},
                        {"id": "n77", "name": "Pikachu", "number": "77", "rarity": "Common"}]}
    monkeypatch.setattr(cc, "sets", lambda: sets)
    monkeypatch.setattr(cc, "set_cards", lambda set_id: cards[set_id])
    return cc


def test_choices_lists_the_cards_a_doubtful_reading_could_be(monkeypatch):
    cc = _two_sets(monkeypatch)
    # The same name and number in two sets of one size: both, newest set first.
    both = cc.choices("Geodude", "23", total=128)
    assert [(c["set_code"], c["why"]) for c in both] == [("new128", "name and number match"),
                                                          ("old128", "name and number match")]
    # A number that belongs to another card: the named card first, then the card with that number.
    mixed = cc.choices("Pikachu", "24", total=128)
    assert [(c["name"], c["card_number"]) for c in mixed] == [("Pikachu", "77"), ("Graveler", "24")]
    assert cc.choices("Nosuchmon", "99", total=128) == []


def test_what_was_printed_stays_with_the_card_and_is_used_when_it_is_checked_again(bench, monkeypatch):
    import json
    cc = _two_sets(monkeypatch)
    cards, _ = add_cards(bench, 1)
    patch = bench._identification_patch(
        dict(name="Pikachu", set_name="Old Set", set_code="", card_number="077", number_total="128",
             set_code_printed="NEW", name_printed="Pikachu", language="en", confidence=0.9), cards[0])
    assert json.loads(patch["id_read"])["number_total"] == "128"
    assert (patch["set_code"], patch["id_confidence"]) == ("new128", bench.ID_VERIFIED)

    # Later the set is wrong on file. The printed total and code still find it.
    with db.connect(bench.DB_PATH) as conn:
        db.update_card(conn, cards[0]["id"], {**{k: v for k, v in patch.items() if not k.startswith("_")},
                                             "set_name": "Old Set", "set_code": "old128", "id_confidence": 0.5})
        card = db.get_card(conn, cards[0]["id"])
    changes, status = bench._checked_identity(card)
    assert status == "verified" and changes["set_code"] == "new128"

    client = bench.app.test_client()
    data = client.get(f"/api/cards/{cards[0]['id']}/choices").get_json()
    assert data["read"]["number_total"] == "128"
    assert [c["set_code"] for c in data["choices"]] == ["new128"]
    assert client.get("/api/cards/999999/choices").status_code == 404


def test_changing_which_card_it_is_clears_the_old_price_so_it_is_priced_again(bench):
    cards, _ = add_cards(bench, 1)
    client = bench.app.test_client()
    cid = cards[0]["id"]
    with db.connect(bench.DB_PATH) as conn:
        db.update_card(conn, cid, {"name": "Geodude", "set_code": "old128", "card_number": "23", "id_confidence": 0.97})
        db.update_card(conn, cid, {"final_price": 4.2, "pricing_confidence": 1.0, "pricing_notes": "two sources"})
    same = client.patch(f"/api/cards/{cid}", json={"rarity": "Common"}).get_json()
    assert same["final_price"] == 4.2
    moved = client.patch(f"/api/cards/{cid}", json={"set_code": "new128"}).get_json()
    # No price and no note is what the page prices by itself.
    assert moved["final_price"] is None and not moved["pricing_notes"]


@pytest.mark.skipif(not __import__("os").getenv("TEST_DATABASE_URL"), reason="needs a Postgres to run against")
def test_a_postgres_table_from_before_gets_the_columns_added_since(bench):
    with db.connect(bench.DB_PATH) as conn:
        conn.execute("ALTER TABLE cards DROP COLUMN id_read")
        conn.execute("ALTER TABLE cards DROP COLUMN printing")
    db.init_db(bench.DB_PATH)
    with db.connect(bench.DB_PATH) as conn:
        have = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'cards'").fetchall()}
    assert {"id_read", "printing"} <= have


# ---------------------------------------------------------------- the owner's price and notes

def test_one_price_for_many_cards_and_what_the_listing_says(bench):
    cards, _ = add_cards(bench, 3)
    ids = [c["id"] for c in cards]
    with db.connect(bench.DB_PATH) as conn:
        for cid in ids:
            db.update_card(conn, cid, {"name": "Pikachu", "set_name": "Base", "card_number": "58", "id_confidence": 0.97})
            db.update_card(conn, cid, {"final_price": 0.17, "pricing_confidence": 1.0, "needs_review": 1})
    client = bench.app.test_client()
    assert client.post("/api/cards/listing", json={"ids": ids[:2]}).status_code == 400
    assert client.post("/api/cards/listing", json={"ids": ids[:2], "list_price": "free"}).status_code == 400
    assert client.post("/api/cards/listing", json={"ids": ids[:2], "list_price": 0}).status_code == 400
    res = client.post("/api/cards/listing", json={"ids": ids[:2], "list_price": "1", "listing_notes": " Ships in a sleeve. "})
    assert res.get_json() == {"changed": 2, "on_ebay": []}
    listed = client.get("/api/cards").get_json()["cards"]
    assert {c["id"]: (c["list_price"], c["final_price"]) for c in listed} == {
        ids[0]: (1.0, 0.17), ids[1]: (1.0, 0.17), ids[2]: (None, 0.17)}

    preview = client.get(f"/api/cards/{ids[0]}/listing").get_json()
    assert (preview["price"], preview["price_is_yours"], preview["estimate"]) == (1.0, True, 0.17)
    assert preview["title"].startswith("Pikachu #58 Base") and preview["description"].endswith("Ships in a sleeve.")
    assert client.get(f"/api/cards/{ids[2]}/listing").get_json()["price_is_yours"] is False

    # What goes to eBay and into exports is the owner's price, and a price a
    # person set is not held back for a review of the estimate.
    with db.connect(bench.DB_PATH) as conn:
        shaped = {c["id"]: c for c in bench._cards_payload(db.list_cards(conn, sort="oldest"))}
    assert (shaped[ids[0]]["final_price"], shaped[ids[0]]["needs_review"]) == (1.0, False)
    assert (shaped[ids[2]]["final_price"], shaped[ids[2]]["needs_review"]) == (0.17, True)

    # Back to the estimate, and one card through the edit route.
    assert client.post("/api/cards/listing", json={"ids": ids[:1], "list_price": None}).get_json()["changed"] == 1
    assert client.patch(f"/api/cards/{ids[2]}", json={"list_price": "2.50"}).get_json()["list_price"] == 2.5
    assert client.patch(f"/api/cards/{ids[2]}", json={"list_price": "-1"}).status_code == 400
    after = {c["id"]: c["list_price"] for c in client.get("/api/cards").get_json()["cards"]}
    assert after == {ids[0]: None, ids[1]: 1.0, ids[2]: 2.5}


def test_editing_a_card_that_is_live_on_ebay_updates_the_listing_when_asked(bench, monkeypatch):
    from lib import ebay_lister
    from webapp import connections

    cards, _ = add_cards(bench, 2)
    live, not_live = cards[0]["id"], cards[1]["id"]
    with db.connect(bench.DB_PATH) as conn:
        for cid in (live, not_live):
            db.update_card(conn, cid, {"name": "Pikachu", "set_name": "Base", "card_number": "58", "id_confidence": 0.97})
            db.update_card(conn, cid, {"final_price": 0.17, "image_url": "https://img.example/p.jpg"})
        db.update_card(conn, live, {"ebay_listing_id": "L1", "ebay_offer_id": "O1", "ebay_listing_status": "listed"})
    sent = []

    class Lister:
        def __init__(self, auth=None):
            pass

        def revise_card(self, card):
            sent.append((card["id"], card["final_price"], card["ebay_offer_id"]))
            return {}

    monkeypatch.setattr(ebay_lister, "EbayLister", Lister)
    monkeypatch.setattr(connections, "_ebay_auth", lambda: object())
    client = bench.app.test_client()
    assert client.post("/api/cards/listing", json={"ids": [live, not_live], "list_price": 1}).get_json()["on_ebay"] == [live]
    out = client.post("/api/publish/ebay/revise", json={"ids": [live, not_live]}).get_json()
    assert out == {"updated": 1, "failed": [], "remaining": 0}
    assert sent == [(live, 1.0, "O1")]                      # only the live one, at the owner's price
    assert client.post("/api/publish/ebay/revise", json={}).status_code == 400


def test_the_scan_list_shows_an_old_price_note_in_plain_words(bench):
    rows, _ = add_cards(bench, 1)
    stored = ("Cardmarket $7.06 vs TCG $1.06 (7x apart) — dropping CM as likely variant mismatch on "
              "pokemontcg.io; ⚠ name-only match to swsh45 — set_code was not provided, match may not "
              "reflect the actual printing; TCG/CM disagree 6.7x on swsh45 — variant mismatch?; match: no_set")
    with db.connect(bench.DB_PATH) as conn:
        db.update_card(conn, rows[0]['id'], {'name': 'Pikachu', 'final_price': 1.06, 'tcgplayer_market': 1.06,
                                             'cardmarket_trend_usd': 7.06, 'pricing_notes': stored,
                                             'needs_review': True})
    client = bench.app.test_client()
    shown = client.get('/api/cards').get_json()['cards'][0]['pricing_notes']
    assert shown == 'Prices disagree: TCGplayer $1.06, Cardmarket $7.06. Using $1.06. Set not confirmed. Check it before listing.'
    assert client.get(f"/api/cards/{rows[0]['id']}").get_json()['pricing_notes'] == shown
    # The stored note is untouched: the review hold still reads its marker.
    with db.connect(bench.DB_PATH) as conn:
        assert db.get_card(conn, rows[0]['id'])['pricing_notes'] == stored
