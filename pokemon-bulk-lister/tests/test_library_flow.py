"""End-to-end over the multi-user layer: library ledger, selling, decks.

Builds a small Flask app from the blueprints against a throwaway SQLite file,
so the tests do not start webapp.app's scheduler or touch output/.
"""
from pathlib import Path

import pytest
from flask import Flask

from webapp import catalog, decks as decks_view, models
from webapp import db as bench_db
from conftest import verify_link


@pytest.fixture
def app(tmp_path, monkeypatch, mail_outbox):
    monkeypatch.setenv("PRICE_REFRESH_ENABLED", "0")
    models.init_models(f"sqlite:///{tmp_path / 'app.sqlite'}")
    from webapp.auth import auth_bp
    from webapp.library import library_bp
    from webapp.marketplace import market_bp

    bench = str(tmp_path / "bench.sqlite")
    bench_db.init_db(bench)
    monkeypatch.setattr(catalog, "BENCH_DB_PATH", bench)
    monkeypatch.setattr(decks_view, "POPULAR_CACHE", tmp_path / "popular.json")

    templates = Path(__file__).resolve().parent.parent / "webapp" / "templates"
    flask_app = Flask(__name__, template_folder=str(templates))
    flask_app.secret_key = "test"
    for bp in (auth_bp, catalog.catalog_bp, market_bp, library_bp, decks_view.decks_bp):
        flask_app.register_blueprint(bp)
    flask_app.bench_path = bench
    flask_app.outbox = mail_outbox
    return flask_app


def _user(app, email, verify=True):
    from webapp import auth
    c = app.test_client()
    r = c.post("/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.get_data(as_text=True)
    if verify:
        # open the link from the email, as the user would
        assert "verify=done" in c.get(verify_link(app.outbox, email)).headers["Location"]
    return c


def _add(c, **kw):
    body = {"name": "Dragapult ex", "set_code": "sv6", "set_name": "Twilight Masquerade",
            "card_number": "130"}
    body.update(kw)
    r = c.post("/api/inventory/add", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _events(c, kind=None):
    return c.get("/api/library/events" + (f"?kind={kind}" if kind else "")).get_json()


def _set_price(card_name, price, confidence=0.9, outlier=False):
    s = models.get_session()
    try:
        cc = s.query(models.CatalogCard).filter_by(name=card_name).first()
        models.record_price(s, cc, {"final_price": price, "pricing_confidence": confidence,
                                    "outlier_flag": outlier, "tcgplayer_market": price})
        s.commit()
    finally:
        s.close()


def test_library_api_requires_login(app):
    c = app.test_client()
    assert c.get("/api/library/events").status_code == 401
    assert c.get("/api/decks").status_code == 401
    assert c.post("/api/inventory/add", json={"name": "x"}).status_code == 401


def test_manual_add_logs_a_purchase_when_a_price_is_given(app):
    c = _user(app, "a@example.com")
    item = _add(c, price=12.5, quantity=2, channel="tcgplayer")
    assert item["acquired_price"] == 12.5 and item["quantity"] == 2
    _add(c, name="Pikachu", card_number="63")

    bought = _events(c, "purchased")
    assert [(e["name"], e["quantity"], e["total"], e["channel"]) for e in bought["events"]] == [
        ("Dragapult ex", 2, 25.0, "tcgplayer")]
    assert bought["totals"]["purchased_spend"] == 25.0
    assert [e["name"] for e in _events(c, "added")["events"]] == ["Pikachu"]


def test_manual_add_validates_input(app):
    c = _user(app, "a@example.com")
    assert c.post("/api/inventory/add", json={"name": " "}).status_code == 400
    assert c.post("/api/inventory/add", json={"name": "x", "price": "abc"}).status_code == 400
    assert c.post("/api/inventory/add", json={"name": "x", "price": -1}).status_code == 400
    assert c.post("/api/inventory/add", json={"name": "x", "quantity": 0}).status_code == 400
    assert c.post("/api/inventory/add", json={"name": "x", "condition": "MINT"}).status_code == 400


def test_record_sale_takes_cards_out_and_keeps_the_history(app):
    c = _user(app, "a@example.com")
    item = _add(c, price=10, quantity=3)
    r = c.post(f"/api/inventory/{item['id']}/record-sale",
               json={"price": 18, "quantity": 2, "channel": "ebay", "fees": 4.5,
                     "sold_on": "2026-10-01"})
    assert r.status_code == 200 and r.get_json()["remaining"] == 1

    sold = _events(c, "sold")
    e = sold["events"][0]
    assert (e["quantity"], e["total"], e["net"], e["gain"]) == (2, 36.0, 31.5, 11.5)
    assert e["occurred_at"].startswith("2026-10-01")
    assert sold["totals"]["sold_net"] == 31.5 and sold["totals"]["realized_gain"] == 11.5

    assert c.post(f"/api/inventory/{item['id']}/record-sale",
                  json={"price": 18, "quantity": 5}).status_code == 400
    assert c.post(f"/api/inventory/{item['id']}/record-sale", json={}).status_code == 400

    # Selling the last copy empties the library but the sale stays on record.
    assert c.post(f"/api/inventory/{item['id']}/record-sale",
                  json={"price": 20}).status_code == 200
    assert c.get("/api/inventory").get_json()["items"] == []
    assert _events(c, "sold")["totals"]["sold_cards"] == 3
    assert c.post(f"/api/inventory/{item['id']}/record-sale",
                  json={"price": 20}).status_code == 404


def test_marketplace_sale_moves_the_card_and_logs_both_sides(app):
    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    item = _add(seller, price=5)
    listing = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 20}).get_json()
    assert listing["fee_on_sale"] == 2.0

    browse = buyer.get("/api/market/listings").get_json()
    assert browse["payments"] == "offline"
    assert browse["listings"][0]["provenance"]["priced_at"] is None

    order = buyer.post(f"/api/market/listings/{listing['listing_id']}/buy").get_json()
    assert order["status"] == "pending_payment"
    # The card is spoken for: it cannot be removed, sold elsewhere or repriced.
    assert seller.delete(f"/api/inventory/{item['id']}").status_code == 409
    assert seller.post(f"/api/inventory/{item['id']}/record-sale", json={"price": 1}).status_code == 409
    assert seller.patch(f"/api/market/listings/{listing['listing_id']}", json={"price": 9}).status_code == 409

    assert seller.post(f"/api/market/orders/{order['order_id']}/mark-paid").status_code == 200

    sold = _events(seller, "sold")["events"][0]
    assert (sold["channel"], sold["total"], sold["fees"], sold["net"], sold["gain"]) == (
        "market", 20.0, 2.0, 18.0, 13.0)
    bought = _events(buyer, "purchased")["events"][0]
    assert (bought["channel"], bought["total"], bought["order_id"]) == ("market", 20.0, order["order_id"])

    assert seller.get("/api/inventory").get_json()["items"] == []
    mine = buyer.get("/api/inventory").get_json()["items"]
    assert len(mine) == 1 and mine[0]["acquired_price"] == 20.0

    # The buyer removing the card later must not break the order history.
    assert buyer.delete(f"/api/inventory/{mine[0]['id']}").status_code == 200
    assert buyer.get("/api/inventory").get_json()["items"] == []
    assert len(_events(buyer, "purchased")["events"]) == 1
    assert len(seller.get("/api/market/my").get_json()["sales"]) == 1


def test_backfill_writes_ledger_rows_once_for_old_orders(app):
    from webapp.library import backfill_order_events

    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    item = _add(seller)
    lid = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 8}).get_json()["listing_id"]
    oid = buyer.post(f"/api/market/listings/{lid}/buy").get_json()["order_id"]
    seller.post(f"/api/market/orders/{oid}/mark-paid")

    s = models.get_session()
    try:  # simulate an order paid before the ledger existed
        s.query(models.LibraryEvent).filter_by(order_id=oid).delete()
        s.commit()
    finally:
        s.close()
    assert _events(seller, "sold")["events"] == []
    assert backfill_order_events() == 2
    assert backfill_order_events() == 0
    assert _events(seller, "sold")["events"][0]["total"] == 8.0
    assert _events(buyer, "purchased")["events"][0]["total"] == 8.0


def test_list_for_sale_rejects_bad_prices(app):
    c = _user(app, "a@example.com")
    item = _add(c)
    url = f"/api/inventory/{item['id']}/list-for-sale"
    assert c.post(url, json={}).status_code == 400          # no market price known
    assert c.post(url, json={"price": "abc"}).status_code == 400
    assert c.post(url, json={"price": -3}).status_code == 400
    ok = c.post(url, json={"price": "4.50"})
    assert ok.status_code == 200 and ok.get_json()["price"] == 4.5
    assert c.post(url, json={"price": 5}).status_code == 409
    lid = ok.get_json()["listing_id"]
    assert c.patch(f"/api/market/listings/{lid}", json={"price": 6}).get_json()["price"] == 6.0
    assert c.patch(f"/api/market/listings/{lid}", json={"price": 0}).status_code == 400


def test_bulk_list_reports_what_it_will_do_and_skips_flagged_prices(app):
    c = _user(app, "a@example.com")
    good = _add(c, name="Good Card", card_number="1")
    flagged = _add(c, name="Shaky Card", card_number="2")
    unpriced = _add(c, name="Unpriced Card", card_number="3")
    listed = _add(c, name="Listed Card", card_number="4")
    _set_price("Good Card", 10)
    _set_price("Shaky Card", 50, confidence=0.2)
    _set_price("Listed Card", 3)
    c.post(f"/api/inventory/{listed['id']}/list-for-sale", json={})
    ids = [good["id"], flagged["id"], unpriced["id"], listed["id"]]

    preview = c.post("/api/inventory/list-for-sale", json={"item_ids": ids, "dry_run": True}).get_json()
    assert (preview["to_list"], preview["total"], preview["listed"]) == (1, 10.0, 0)
    assert (preview["skipped_flagged"], preview["skipped_no_price"],
            preview["skipped_already_listed"]) == (1, 1, 1)
    assert len(c.get("/api/market/my").get_json()["active_listings"]) == 1

    done = c.post("/api/inventory/list-for-sale", json={"item_ids": ids}).get_json()
    assert done["listed"] == 1
    everything = c.post("/api/inventory/list-for-sale",
                        json={"item_ids": ids, "include_flagged": True}).get_json()
    assert everything["listed"] == 1 and everything["skipped_already_listed"] == 2
    assert len(c.get("/api/market/my").get_json()["active_listings"]) == 3


def test_promote_from_bench_logs_added_and_does_not_reimport_sold_cards(app):
    with bench_db.connect(app.bench_path) as conn:
        gid = bench_db.get_or_create_grid(conn, "page1", "input/grids/page1.jpg")
        cid = bench_db.insert_card_stub(conn, gid, "output/crops/page1_r0c0.jpg", 0, 0)
        bench_db.update_card(conn, cid, {"name": "Pikachu", "set_code": "sv1", "card_number": "63",
                                         "final_price": 1.25, "pricing_confidence": 0.8})
    c = _user(app, "a@example.com")
    assert c.post("/api/inventory/promote", json={"all_priced": True}).get_json()["promoted"] == 1
    assert _events(c, "added")["events"][0]["channel"] == "scan"
    item = c.get("/api/inventory").get_json()["items"][0]
    assert item["provenance"]["sources"] == {} and item["price"] == 1.25

    c.post(f"/api/inventory/{item['id']}/record-sale", json={"price": 2})
    again = c.post("/api/inventory/promote", json={"all_priced": True}).get_json()
    assert (again["promoted"], again["skipped_duplicates"]) == (0, 1)


def _sell(seller, buyer, item_id, price=5):
    lid = seller.post(f"/api/inventory/{item_id}/list-for-sale", json={"price": price}).get_json()["listing_id"]
    oid = buyer.post(f"/api/market/listings/{lid}/buy").get_json()["order_id"]
    return lid, oid


def test_a_listed_card_cannot_be_emptied_out_from_under_a_buyer(app):
    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    item = _add(seller)
    lid = seller.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 5}).get_json()["listing_id"]

    # Setting quantity to 0 takes the listing down with it.
    assert seller.patch(f"/api/inventory/{item['id']}", json={"quantity": 0}).status_code == 200
    assert buyer.get("/api/market/listings").get_json()["listings"] == []
    assert buyer.post(f"/api/market/listings/{lid}/buy").status_code == 404

    # With a buyer waiting, the quantity cannot be zeroed at all.
    seller.patch(f"/api/inventory/{item['id']}", json={"quantity": 1})
    _sell(seller, buyer, item["id"])
    assert seller.patch(f"/api/inventory/{item['id']}", json={"quantity": 0}).status_code == 409
    assert seller.patch(f"/api/inventory/{item['id']}", json={"quantity": "x"}).status_code == 400


def test_non_finite_and_absurd_amounts_are_rejected(app):
    c = _user(app, "a@example.com")
    item = _add(c)
    for bad in ("inf", "nan", "1e999", 2_000_000):
        assert c.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": bad}).status_code == 400
        assert c.post(f"/api/inventory/{item['id']}/record-sale", json={"price": bad}).status_code == 400
        assert c.post("/api/inventory/add", json={"name": "x", "price": bad}).status_code == 400
        assert c.patch(f"/api/inventory/{item['id']}", json={"acquired_price": bad}).status_code == 400
    lid = c.post(f"/api/inventory/{item['id']}/list-for-sale", json={"price": 3}).get_json()["listing_id"]
    assert c.patch(f"/api/market/listings/{lid}", json={"price": "inf"}).status_code == 400
    # a non-string note is stored as text, not a server error
    assert c.post("/api/inventory/add", json={"name": "y", "note": {"a": 1}}).status_code == 200


def test_a_card_sold_on_the_marketplace_is_not_reimported_from_the_bench(app):
    with bench_db.connect(app.bench_path) as conn:
        gid = bench_db.get_or_create_grid(conn, "page1", "input/grids/page1.jpg")
        cid = bench_db.insert_card_stub(conn, gid, "output/crops/page1_r0c0.jpg", 0, 0)
        bench_db.update_card(conn, cid, {"name": "Pikachu", "final_price": 1.25})
    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    assert seller.post("/api/inventory/promote", json={"card_ids": [cid]}).get_json()["promoted"] == 1
    item = seller.get("/api/inventory").get_json()["items"][0]
    _, oid = _sell(seller, buyer, item["id"])
    seller.post(f"/api/market/orders/{oid}/mark-paid")

    again = seller.post("/api/inventory/promote", json={"all_priced": True}).get_json()
    assert (again["promoted"], again["skipped_duplicates"]) == (0, 1)
    assert seller.get("/api/inventory").get_json()["items"] == []
    totals = _events(seller)["totals"]
    assert (totals["added_cards"], totals["sold_cards"]) == (1, 1)


def test_an_unpaid_order_can_be_cancelled_by_either_side(app):
    seller, buyer, other = (_user(app, f"{n}@example.com") for n in ("seller", "buyer", "other"))
    item = _add(seller)
    lid, oid = _sell(seller, buyer, item["id"])
    assert other.post(f"/api/market/orders/{oid}/cancel").status_code == 404
    assert buyer.post(f"/api/market/orders/{oid}/cancel").status_code == 200
    # the card is free again: repricing works and someone else can buy it
    assert seller.patch(f"/api/market/listings/{lid}", json={"price": 6}).status_code == 200
    oid2 = other.post(f"/api/market/listings/{lid}/buy").get_json()["order_id"]
    assert seller.post(f"/api/market/orders/{oid2}/cancel").status_code == 200
    assert seller.post(f"/api/market/orders/{oid}/mark-paid").status_code == 404
    assert _events(seller, "sold")["events"] == []


def test_settling_an_order_twice_records_one_sale(app):
    from webapp import marketplace

    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    item = _add(seller, quantity=3)
    _, oid = _sell(seller, buyer, item["id"])
    assert seller.post(f"/api/market/orders/{oid}/mark-paid").status_code == 200

    s = models.get_session()
    try:  # a retried webhook or a second click arriving with a stale order object
        order = s.get(models.Order, oid)
        with pytest.raises(marketplace.PaymentError):
            marketplace._apply_payment(s, order)
        s.rollback()
    finally:
        s.close()
    assert _events(seller, "sold")["totals"]["sold_cards"] == 1
    assert seller.get("/api/inventory").get_json()["items"][0]["quantity"] == 2
    assert len(buyer.get("/api/inventory").get_json()["items"]) == 1


def test_payment_is_refused_when_the_seller_no_longer_holds_the_card(app):
    seller, buyer = _user(app, "seller@example.com"), _user(app, "buyer@example.com")
    item = _add(seller)
    _, oid = _sell(seller, buyer, item["id"])
    s = models.get_session()
    try:  # the state the guards prevent, forced directly
        s.get(models.InventoryItem, item["id"]).quantity = 0
        s.commit()
    finally:
        s.close()
    r = seller.post(f"/api/market/orders/{oid}/mark-paid")
    assert r.status_code == 409 and "no longer has this card" in r.get_json()["error"]
    assert _events(seller, "sold")["events"] == [] and _events(buyer, "purchased")["events"] == []
    assert buyer.get("/api/market/my").get_json()["purchases"][0]["status"] == "cancelled"


DECK = """Pokémon: 2
4 Dragapult ex TWM 130
2 Pikachu SVI 63
Trainer: 1
4 Ultra Ball SVI 196
Energy: 1
6 Fire Energy MEE 10
"""


def test_deck_import_shows_coverage_missing_cards_listings_and_cost(app):
    me, seller = _user(app, "me@example.com"), _user(app, "seller@example.com")
    _add(me, quantity=1)                                            # 1 of 4 Dragapult ex
    _add(me, name="Ultra Ball", set_code="sv4pt5", card_number="91", quantity=4)

    theirs = _add(seller, quantity=2)
    _set_price("Dragapult ex", 15.0)
    seller.post(f"/api/inventory/{theirs['id']}/list-for-sale", json={"price": 14})

    r = me.post("/api/decks", json={"name": "Test deck", "text": DECK})
    assert r.status_code == 200
    assert any("16 cards" in w for w in r.get_json()["warnings"])
    ref = r.get_json()["ref"]

    listing = me.get("/api/decks").get_json()
    assert listing["saved"][0]["name"] == "Test deck"
    assert (listing["saved"][0]["have"], listing["saved"][0]["missing"]) == (5, 11)
    assert listing["popular"] and listing["popular_source"]["origin"] == "bundled"
    assert all(d["total"] == 60 for d in listing["popular"])

    d = me.get(f"/api/decks/{ref}").get_json()
    by_name = {c["name"]: c for c in d["cards"]}
    drag = by_name["Dragapult ex"]
    assert (drag["have"], drag["missing"]) == (1, 3)
    assert drag["price"]["price"] == 15.0 and drag["price"]["basis"] == "this printing"
    assert drag["price"]["provenance"]["sources"] == {"tcgplayer": 15.0}
    assert drag["listing_count"] == 1
    assert (drag["listings"][0]["price"], drag["listings"][0]["exact"]) == (14.0, True)
    assert "tcgplayer.com" in drag["links"]["tcgplayer"]
    assert by_name["Ultra Ball"]["missing"] == 0 and by_name["Ultra Ball"]["have_other"] == 4
    assert by_name["Fire Energy"]["basic_energy"] is True

    assert d["cost"] == {
        "estimate": 45.0, "priced_missing": 3, "unpriced_missing": 2,
        "basic_energy_missing": 6, "source": d["cost"]["source"],
    }
    assert d["missing_lines_on_platform"] == 1
    assert d["missing_list"].splitlines() == [
        "3 Dragapult ex TWM 130", "2 Pikachu SVI 63", "6 Fire Energy MEE 10"]

    # Only Pikachu lacks a price; basic energy is never looked up.
    dry = me.post(f"/api/decks/{ref}/price-missing", json={"dry_run": True}).get_json()
    assert dry == {"to_price": 1, "over_cap": 0, "started": False}

    assert me.delete(f"/api/decks/{ref}").status_code == 200
    assert me.get(f"/api/decks/{ref}").status_code == 404


def test_decks_are_private_and_popular_decks_can_be_saved(app):
    me, other = _user(app, "me@example.com"), _user(app, "other@example.com")
    ref = me.post("/api/decks", json={"text": DECK}).get_json()["ref"]
    assert other.get(f"/api/decks/{ref}").status_code == 404
    assert other.delete(f"/api/decks/{ref}").status_code == 404
    assert me.post("/api/decks", json={"text": "nothing here"}).status_code == 400

    popular_ref = me.get("/api/decks").get_json()["popular"][0]["ref"]
    detail = me.get(f"/api/decks/{popular_ref}").get_json()
    assert detail["source"] == "popular" and detail["summary"]["total"] == 60
    assert detail["meta"]["sample"]["tournament"]
    saved = me.post("/api/decks", json={"popular_ref": popular_ref}).get_json()
    kept = me.get(f"/api/decks/{saved['ref']}").get_json()
    assert kept["saved"] and kept["source"] == "popular"
    assert kept["meta"]["sample"]["tournament"] == detail["meta"]["sample"]["tournament"]
    assert me.get("/api/decks/p-does-not-exist").status_code == 404


def test_removing_or_editing_quantity_by_hand_goes_in_the_ledger(app):
    c = _user(app, "a@example.com")
    item = _add(c, quantity=3)
    c.patch(f"/api/inventory/{item['id']}", json={"quantity": 5})
    c.patch(f"/api/inventory/{item['id']}", json={"quantity": 4})
    c.patch(f"/api/inventory/{item['id']}", json={"quantity": 4})      # no change, no row
    assert c.delete(f"/api/inventory/{item['id']}").status_code == 200

    data = _events(c)
    assert [(e["kind"], e["quantity"]) for e in reversed(data["events"])] == [
        ("added", 3), ("added", 2), ("removed", 1), ("removed", 4)]
    totals = data["totals"]
    # in minus out equals what is held now
    assert totals["added_cards"] - totals["removed_cards"] - totals["sold_cards"] == 0
    assert c.get("/api/inventory").get_json()["items"] == []
    assert [e["kind"] for e in _events(c, "removed")["events"]] == ["removed", "removed"]
