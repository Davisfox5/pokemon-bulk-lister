"""AI-written listing descriptions: eBay's writer in batches of 10 for the
first 20 batches a day, ours after that or whenever eBay can't, and the
result on the card and its live listing."""
import json

import cv2
import numpy as np
import pytest

from lib import ebay_lister, ebay_mapping, listing_ai
from webapp import db, descriptions, models, runtime


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICE_REFRESH_ENABLED", "0")
    from webapp import app as mod
    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    monkeypatch.setattr(runtime, "DATA_ROOT", tmp_path)
    crops = tmp_path / "output" / "crops"
    crops.mkdir(parents=True)
    monkeypatch.setattr(mod, "CROPS_DIR", crops)
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "bench.sqlite"))
    db.init_db(mod.DB_PATH)
    db.set_owner(None)
    return mod


def add_cards(bench, count, live=()):
    with db.connect(bench.DB_PATH) as conn:
        gid = db.get_or_create_grid(conn, "photo", "input/grids/photo.jpg")
        ids = []
        for i in range(count):
            path = bench.CROPS_DIR / f"card{i}.jpg"
            cv2.imwrite(str(path), np.full((70, 50, 3), 90, np.uint8))
            cid = db.insert_card_stub(conn, gid, runtime.rel_path(path), i // 3, i % 3)
            db.update_card(conn, cid, {"name": f"Card {i}", "set_name": "Base", "card_number": str(i),
                                       "rarity": "Common", "condition_guess": "NM", "id_confidence": 0.97})
            db.update_card(conn, cid, {"final_price": 1.0, "image_url": f"https://img.example/{i}.jpg"})
            if i in live:
                db.update_card(conn, cid, {"ebay_listing_id": f"L{i}", "ebay_offer_id": f"O{i}",
                                           "ebay_listing_status": "listed"})
            ids.append(cid)
        rows = [db.get_card(conn, cid) for cid in ids]
    return [bench._cards_payload([r])[0] for r in rows]


class FakeEbay:
    def __init__(self, ready=True, limit_after=None):
        self._ready, self.limit_after = ready, limit_after
        self.batches, self.results = [], {}

    def ready(self):
        return self._ready

    def start(self, cards):
        if self.limit_after is not None and len(self.batches) >= self.limit_after:
            raise ebay_mapping.EbayMappingLimit("daily allowance used up")
        self.batches.append([c["crop_path"] for c in cards])
        return f"T{len(self.batches)}"

    def result(self, task_id):
        return self.results.get(task_id)


class FakeWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, cards, examples):
        self.calls.append((len(cards), examples))
        return {c["sku"]: f"Ours for {c['facts']['name']}." for c in cards}


class FakeLister:
    def __init__(self):
        self.revised = []

    def revise_card(self, card):
        self.revised.append((card["id"], ebay_lister.card_description(card), card["ebay_offer_id"]))


def pump(bench, ebay, writer, lister=None, flush=False):
    return descriptions.pump(flush=flush, mapping=ebay, writer=writer,
                             shape=lambda row: bench._cards_payload([row])[0],
                             lister_factory=(lambda: lister) if lister else None)


def jobs():
    s = models.get_session()
    try:
        return {j.sku: j for j in s.query(models.DescriptionJob).all()}
    finally:
        s.close()


def test_ten_at_a_time_to_ebay_and_a_short_batch_waits_for_the_scheduled_run(bench):
    cards = add_cards(bench, 23)
    assert descriptions.enqueue(cards) == 23
    assert descriptions.enqueue(cards) == 0                       # already queued: not twice
    ebay, writer = FakeEbay(), FakeWriter()
    out = pump(bench, ebay, writer)
    assert [len(b) for b in ebay.batches] == [10, 10] and out["sent_to_ebay"] == 20
    assert (out["waiting"], out["at_ebay"]) == (3, 20) and writer.calls == []
    assert [len(b) for b in ebay.batches] == [10, 10]
    pump(bench, ebay, writer)                                     # 3 waiting: no new batch
    assert len(ebay.batches) == 2
    out = pump(bench, ebay, writer, flush=True)                   # the 12-hour run sends the 3
    assert [len(b) for b in ebay.batches] == [10, 10, 3] and out["waiting"] == 0


def test_ebay_results_go_on_the_card_and_its_live_listing(bench):
    cards = add_cards(bench, 10, live={0})
    descriptions.enqueue(cards)
    ebay, writer, lister = FakeEbay(), FakeWriter(), FakeLister()
    pump(bench, ebay, writer, lister)
    skus = [c["crop_path"].split("/")[-1][:-4] for c in cards]
    ebay.results["T1"] = None                                     # still working
    assert pump(bench, ebay, writer, lister)["at_ebay"] == 10
    ebay.results["T1"] = {"status": "COMPLETED_WITH_ERROR",
                          "descriptions": {sku: f"eBay says {sku}." for sku in skus[:9]}, "failed": [skus[9]]}
    out = pump(bench, ebay, writer, lister)
    # Nine from eBay; the one eBay could not write, ours writes.
    assert (out["from_ebay"], out["written_by_us"]) == (9, 1)
    done = jobs()
    assert {done[s].source for s in skus[:9]} == {"ebay"} and done[skus[9]].source == "ours"
    # Ours is shown what eBay wrote, to write the same way.
    assert writer.calls[0][1] and writer.calls[0][1][0]["description"].startswith("eBay says")
    with db.connect(bench.DB_PATH) as conn:
        first = db.get_card(conn, cards[0]["id"])
    assert (first["ai_description"], first["description_source"]) == (f"eBay says {skus[0]}.", "ebay")
    # Only the live card's listing was updated, with the AI text in place of the facts.
    assert lister.revised == [(cards[0]["id"], f"eBay says {skus[0]}.", "O0")]


def test_after_twenty_ebay_batches_in_a_day_ours_writes_the_rest(bench):
    cards = add_cards(bench, 215)
    descriptions.enqueue(cards)
    ebay, writer = FakeEbay(), FakeWriter()
    out = pump(bench, ebay, writer)
    assert len(ebay.batches) == 20 and out["sent_to_ebay"] == 200
    assert out["written_by_us"] == 15 and out["waiting"] == 0
    # The next card that day goes straight to ours.
    descriptions.enqueue(add_cards_more(bench, 1, start=215))
    pump(bench, ebay, writer, flush=True)
    assert len(ebay.batches) == 20


def add_cards_more(bench, count, start):
    with db.connect(bench.DB_PATH) as conn:
        gid = db.get_or_create_grid(conn, "photo2", "input/grids/photo2.jpg")
        out = []
        for i in range(start, start + count):
            path = bench.CROPS_DIR / f"card{i}.jpg"
            cv2.imwrite(str(path), np.full((70, 50, 3), 90, np.uint8))
            cid = db.insert_card_stub(conn, gid, runtime.rel_path(path), 0, 0)
            out.append(db.update_card(conn, cid, {"name": f"Card {i}", "set_name": "Base", "card_number": str(i)}))
    return [bench._cards_payload([r])[0] for r in out]


def test_ours_writes_when_ebay_cannot(bench):
    # Not connected, or connected without the scope: ours, straight away.
    cards = add_cards(bench, 4)
    descriptions.enqueue(cards)
    writer = FakeWriter()
    out = pump(bench, None, writer)
    assert out["written_by_us"] == 4 and writer.calls[0][0] == 4
    descriptions.enqueue(add_cards_more(bench, 12, start=50))
    ebay = FakeEbay(ready=False)
    assert pump(bench, ebay, writer)["written_by_us"] == 12 and ebay.batches == []
    # eBay says the day's allowance is gone: ours for the rest of the day.
    descriptions.enqueue(add_cards_more(bench, 25, start=100))
    ebay = FakeEbay(limit_after=1)
    out = pump(bench, ebay, writer)
    assert len(ebay.batches) == 1 and out["written_by_us"] == 15
    descriptions.enqueue(add_cards_more(bench, 10, start=200))
    pump(bench, ebay, writer)
    assert len(ebay.batches) == 1                                 # still the same day: no more tries


def test_a_card_whose_identity_changes_gets_a_new_description(bench):
    cards = add_cards(bench, 1)
    descriptions.enqueue(cards)
    pump(bench, None, FakeWriter())
    client = bench.app.test_client()
    cid = cards[0]["id"]
    assert client.get(f"/api/cards/{cid}/listing").get_json()["written_by"] == "ours"
    # Notes go under the written description; the facts line is replaced.
    client.patch(f"/api/cards/{cid}", json={"listing_notes": "Ships in a top loader."})
    text = client.get(f"/api/cards/{cid}/listing").get_json()["description"]
    assert text == "Ours for Card 0.\nShips in a top loader."
    # A different card number: the old description is dropped and a new one queued.
    moved = client.patch(f"/api/cards/{cid}", json={"card_number": "99"}).get_json()
    assert moved["ai_description"] is None
    preview = client.get(f"/api/cards/{cid}/listing").get_json()
    assert preview["ai_status"] == "waiting" and preview["description"].startswith("Card 0, Base #99.")


def test_the_scheduled_run_needs_the_secret_and_moves_every_bench(bench, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    client = bench.app.test_client()
    assert client.get("/api/cron/descriptions").status_code == 401
    cards = add_cards(bench, 2)
    descriptions.enqueue(cards)
    writer = FakeWriter()
    monkeypatch.setattr(listing_ai, "describe", writer)
    out = client.get("/api/cron/descriptions", headers={"Authorization": "Bearer s3cret"}).get_json()
    assert out["None"]["written_by_us"] == 2                      # no eBay set up here: ours


def test_ebay_mapping_requests_and_results_match_ebays_schema(monkeypatch):
    sent = []

    class Auth:
        def has_scope(self, scope):
            return scope == ebay_mapping.MAPPING_SCOPE

        def access_token(self):
            return "tok"

    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    replies = []

    class Session:
        def post(self, url, json=None, headers=None, timeout=None):
            sent.append((url, json, headers))
            return replies.pop(0)

    m = ebay_mapping.EbayMapping(Auth(), env="production", session=Session())
    assert m.ready()
    card = {"crop_path": "output/crops/abc_r0c0.jpg", "name": "Pikachu", "set_name": "Base",
            "card_number": "58", "rarity": "Common", "is_holo": False, "image_url": "https://img/p.jpg"}
    replies.append(Reply(200, {"data": {"startListingPreviewsCreation": {
        "errors": [], "listingPreviewsCreationTask": {"id": "task-1"}}}}))
    assert m.start([card]) == "task-1"
    url, body, headers = sent[0]
    assert url == ebay_mapping.GRAPHQL_URL and headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    product = body["variables"]["input"]["externalProducts"][0]
    assert product["sku"] == "abc_r0c0" and product["images"] == ["https://img/p.jpg"]
    assert {"name": "Card Number", "values": ["58"]} in product["aspects"]
    assert "startListingPreviewsCreation(input: $input)" in body["query"]

    replies.append(Reply(200, {"data": {"listingPreviewsCreationTaskById": {
        "listingPreviewsCreationTask": {"id": "task-1", "result": None}}}}))
    assert m.result("task-1") is None
    replies.append(Reply(200, {"data": {"listingPreviewsCreationTaskById": {"listingPreviewsCreationTask": {
        "id": "task-1", "result": {
            "completionStatus": "COMPLETED_WITH_ERROR",
            "listingPreviews": [{"sku": "abc_r0c0", "description": " A Pikachu card. ",
                                 "clientProvidedProductDetails": {"sku": "abc_r0c0"}}],
            "invalidProducts": [{"description": "bad", "clientProvidedProductDetails": {"sku": "x_r0c1"}}],
            "unmappedProducts": [], "unprocessedProducts": [{"sku": "y_r0c2"}]}}}}}))
    assert m.result("task-1") == {"status": "COMPLETED_WITH_ERROR",
                                  "descriptions": {"abc_r0c0": "A Pikachu card."}, "failed": ["x_r0c1", "y_r0c2"]}
    replies.append(Reply(429, {}))
    with pytest.raises(ebay_mapping.EbayMappingLimit):
        m.start([card])
    replies.append(Reply(200, {"errors": [{"message": "Insufficient permissions"}]}))
    with pytest.raises(ebay_mapping.EbayMappingError):
        m.start([card])
    with pytest.raises(ValueError):
        m.start([card] * 11)


def test_our_writer_sends_only_facts_and_keeps_only_the_cards_asked_for(monkeypatch):
    seen = {}

    class Messages:
        def create(self, **kw):
            seen.update(kw)
            text = json.dumps({"descriptions": [{"sku": "a", "description": " Pikachu from Base. "},
                                                {"sku": "zzz", "description": "not asked for"}]})
            return type("R", (), {"stop_reason": "end_turn",
                                  "content": [type("B", (), {"type": "text", "text": text})()]})()

    monkeypatch.setattr(listing_ai, "_client", lambda: type("C", (), {"messages": Messages()})())
    facts = listing_ai.card_facts({"name": "Pikachu", "set_name": "Base (Japanese)", "card_number": "58",
                                   "condition_guess": "", "listing_notes": "secret note"})
    assert facts == {"name": "Pikachu", "set": "Base", "number": "58", "finish": "Non-holo", "language": "Japanese"}
    out = listing_ai.describe([{"sku": "a", "facts": facts}],
                              [{"facts": {"name": "Eevee"}, "description": "An Eevee card from Jungle."}])
    assert out == {"a": "Pikachu from Base."}
    assert seen["model"] == listing_ai.DEFAULT_MODEL
    assert "An Eevee card from Jungle." in seen["system"] and "secret note" not in json.dumps(seen)


def library_listing(status="live"):
    """A Library card listed with the account's own eBay keys: its user,
    inventory item and eBay listing record. Returns (user id, the card dict
    the listing was published from)."""
    s = models.get_session()
    try:
        user = models.User(email="seller@example.com", password_hash="x")
        s.add(user)
        s.flush()
        cc = models.get_or_create_catalog_card(s, {"name": "Pikachu", "set_name": "Base", "card_number": "58"})
        item = models.InventoryItem(user_id=user.id, catalog_card_id=cc.id, source_crop_path="output/crops/pk.jpg")
        s.add(item)
        s.flush()
        s.add(models.Listing(user_id=user.id, inventory_item_id=item.id, marketplace="ebay",
                             sku="pk", offer_id="OF1", status=status, price=4.0))
        s.commit()
        card = {"id": f"inv{item.id}", "inventory_item_id": item.id, "name": "Pikachu", "set_name": "Base",
                "card_number": "58", "rarity": "Common", "condition_guess": "NM",
                "crop_path": "output/crops/pk.jpg", "image_url": "https://img.example/pk.jpg", "final_price": 4.0}
        return user.id, card
    finally:
        s.close()


def test_a_library_listing_gets_its_ai_description_through_the_accounts_own_ebay(bench):
    uid, card = library_listing()
    assert descriptions.enqueue_item(card, uid, "OF1") == "pk"
    ebay, writer, lister = FakeEbay(ready=False), FakeWriter(), FakeLister()
    asked = []

    def item_lister(user_id):
        asked.append(user_id)
        return lister

    out = descriptions.pump(mapping=ebay, writer=writer, item_lister=item_lister)
    assert (out["written_by_us"], out["applied"]) == (1, 1)
    assert asked == [uid]
    # The live listing now reads the AI text, at the price it was listed for.
    assert lister.revised == [(card["id"], "Ours for Pikachu.", "OF1")]
    assert jobs()["pk"].applied and jobs()["pk"].card_id == 0
    # The bench is untouched: a Library card has no bench row.
    with db.connect(bench.DB_PATH) as conn:
        assert db.list_cards(conn) == []


def test_a_library_listing_no_longer_live_keeps_its_listing_as_it_is(bench):
    uid, card = library_listing(status="ended")
    descriptions.enqueue_item(card, uid, "OF1")
    lister = FakeLister()
    out = descriptions.pump(mapping=None, writer=FakeWriter(), item_lister=lambda _uid: lister)
    assert out["written_by_us"] == 1 and out["applied"] == 0
    assert lister.revised == [] and jobs()["pk"].applied
