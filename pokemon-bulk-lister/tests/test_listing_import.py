"""Cards already listed on eBay, TCGplayer or Whatnot, read into the library."""
import io

import pytest

from lib import ebay_listings
from test_accounts_sales_payouts import _add, _events, _scanned_card, _user, app  # noqa: F401
from webapp import listing_import, models

TCG_EXPORT = """TCGplayer Id,Product Line,Set Name,Product Name,Title,Number,Rarity,Condition,TCG Market Price,Total Quantity,Add to Quantity,TCG Marketplace Price
7001,Pokemon,SV06: Twilight Masquerade,Dragapult ex,,130/167,Double Rare,Near Mint Holofoil,14.50,2,0,15.00
7002,Pokemon,SV06: Twilight Masquerade,Dragapult ex,,130/167,Double Rare,Lightly Played Holofoil,12.00,0,0,
7003,Pokemon,SV06: Twilight Masquerade,Dreepy,,128/167,Common,Near Mint,0.10,0,0,
7004,Pokemon,SV06: Twilight Masquerade,Dreepy,,128/167,Common,Lightly Played Reverse Holofoil,0.30,3,0,0.45
7009,Pokemon,SV06: Twilight Masquerade,Twilight Masquerade Booster Box,,,,Unopened,120.00,1,0,125.00
9001,Magic,Some Set,Dragapult ex,,130,Rare,Near Mint,1.00,4,0,1.00
"""

CARDS_130_167 = [{"name": "Dragapult ex", "card_number": "130", "set_name": "Twilight Masquerade",
                  "set_code": "sv6", "rarity": "Double Rare", "image_url": "https://img.example/sv6-130.png"}]
CARDS_63_198 = [
    {"name": "Pikachu", "card_number": "63", "set_name": "Scarlet & Violet", "set_code": "sv1",
     "rarity": "Common", "image_url": None},
    {"name": "Pikachu ex", "card_number": "63", "set_name": "Made Up Set", "set_code": "zz1",
     "rarity": "Double Rare", "image_url": None},
]


@pytest.fixture
def site(app, monkeypatch):  # noqa: F811
    app.register_blueprint(listing_import.listings_bp)
    calls = []

    def lookup(number, total):
        calls.append((number, total))
        return {("130", 167): CARDS_130_167, ("63", 198): CARDS_63_198}.get((number, total), [])

    monkeypatch.setattr(listing_import, "lookup_cards", lookup)
    monkeypatch.setattr(listing_import, "_lookup_cache", {})
    app.lookups = calls
    return app


def _upload(client, source, text, dry_run=False):
    data = {"source": source, "file": (io.BytesIO(text.encode()), "export.csv")}
    if dry_run:
        data["dry_run"] = "1"
    return client.post("/api/listings/import", data=data, content_type="multipart/form-data")


def _have(client):
    return client.get("/api/inventory").get_json()["items"]


# ---------------------------------------------------------------- TCGplayer

def test_a_tcgplayer_export_adds_only_the_rows_with_a_quantity(site):
    me = _user(site, "a@example.com")
    p = _upload(me, "tcgplayer", TCG_EXPORT, dry_run=True).get_json()
    assert (p["dry_run"], p["listings"], p["new"], p["new_cards"], p["not_listed"]) == (True, 3, 3, 6, 2)
    assert _have(me) == []                                         # a preview changes nothing

    r = _upload(me, "tcgplayer", TCG_EXPORT).get_json()
    assert (r["added"], r["cards"], r["unmatched"], r["duplicates"]) == (2, 5, 1, 0)
    have = {i["name"]: i for i in _have(me)}
    assert (have["Dragapult ex"]["quantity"], have["Dragapult ex"]["condition"], have["Dragapult ex"]["is_holo"]) == (2, "NM", True)
    assert (have["Dreepy"]["quantity"], have["Dreepy"]["condition"], have["Dreepy"]["is_holo"]) == (3, "LP", False)
    assert have["Dreepy"]["notes"] == "Reverse Holofoil"
    assert have["Dragapult ex"]["listed_on"] == [
        {"source": "tcgplayer", "site": "TCGplayer", "price": 15.0, "url": None}]
    added = _events(me, "added")["events"]
    assert {e["channel"] for e in added} == {"tcgplayer"} and len(added) == 2

    # The sealed box is kept for a person, not turned into a card.
    status = me.get("/api/listings/external").get_json()
    assert [(u["title"].split(" · ")[0], u["detail"]) for u in status["unmatched"]] == [
        ("Twilight Masquerade Booster Box", "the row has no card number, set or known condition")]

    # Reading the same file again adds nothing.
    again = _upload(me, "tcgplayer", TCG_EXPORT).get_json()
    assert (again["added"], again["duplicates"]) == (0, 3)
    assert sum(i["quantity"] for i in _have(me)) == 5


def test_a_tcgplayer_row_lands_on_the_catalog_card_a_scan_already_made(site):
    me = _user(site, "a@example.com")
    mine = _add(me, name="Dragapult ex", set_name="Twilight Masquerade", set_code="sv6",
                card_number="130", is_holo=True)
    _upload(me, "tcgplayer", TCG_EXPORT)
    same = [i for i in _have(me) if i["name"] == "Dragapult ex"]
    assert len(same) == 2 and {i["catalog_card_id"] for i in same} == {mine["catalog_card_id"]}


def test_files_that_are_not_the_right_export_are_refused_with_the_reason(site):
    me = _user(site, "a@example.com")
    r = _upload(me, "tcgplayer", "SKU,Price\nx,1\n")
    assert r.status_code == 400 and "TCGplayer Id" in r.get_json()["error"]
    r = _upload(me, "whatnot", "SKU,Price\nx,1\n")
    assert r.status_code == 400 and "no Title column" in r.get_json()["error"]
    assert _upload(me, "amazon", "Title\nx\n").status_code == 400
    assert site.test_client().get("/api/listings/external").status_code == 401


# ---------------------------------------------------------------- titles (Whatnot, eBay)

WHATNOT = """Title,Quantity,Price,SKU
Pokemon Dragapult ex 130/167 Twilight Masquerade LP,1,$14.00,wn-1
Pikachu ex 063/198 holo,2,9.50,
Pokemon card lot 130/167 and more,1,5.00,
Charizard holo no number,1,50.00,
Dragapult ex 130/167 scanned copy,1,13.00,page01_r0c0
Sold out thing 130/167 Dragapult ex,0,1.00,
"""


def test_titles_are_identified_by_number_set_size_and_name_or_left_for_a_person(site):
    me = _user(site, "a@example.com")
    _scanned_card(site, me)                                        # SKU page01_r0c0 is already in the library
    p = _upload(me, "whatnot", WHATNOT, dry_run=True).get_json()
    assert (p["listings"], p["new"], p["already_in_library"], p["not_listed"]) == (5, 4, 1, 1)

    r = _upload(me, "whatnot", WHATNOT).get_json()
    assert (r["added"], r["cards"], r["linked"], r["unmatched"]) == (2, 3, 1, 2)
    have = _have(me)
    assert sum(i["quantity"] for i in have) == 4                   # 1 scanned + 1 + 2; the scanned one not doubled
    drag = next(i for i in have if i["name"] == "Dragapult ex" and i["crop_path"] is None)
    assert (drag["condition"], drag["set_code"], drag["image_url"]) == ("LP", "sv6", "https://img.example/sv6-130.png")
    pika = next(i for i in have if i["name"].startswith("Pikachu"))
    assert (pika["name"], pika["quantity"], pika["is_holo"]) == ("Pikachu ex", 2, True)   # not plain "Pikachu"
    scanned = next(i for i in have if i["crop_path"])
    assert scanned["listed_on"] == [{"source": "whatnot", "site": "Whatnot", "price": 13.0, "url": None}]

    status = me.get("/api/listings/external").get_json()
    why = {u["title"]: u["detail"] for u in status["unmatched"]}
    assert why == {"Pokemon card lot 130/167 and more": "looks like a lot or a multi-card listing",
                   "Charizard holo no number": "no collector number like 130/167 in the title"}
    assert status["counts"] == {"imported": 2, "linked": 1, "unmatched": 2}


def test_a_person_assigns_or_dismisses_what_could_not_be_identified(site):
    me, other = _user(site, "a@example.com"), _user(site, "b@example.com")
    _upload(me, "whatnot", "Title,Quantity,Price\nCharizard holo no number,2,50.00\nJunk,1,1\n")
    rows = {u["title"]: u["id"] for u in me.get("/api/listings/external").get_json()["unmatched"]}
    char, junk = rows["Charizard holo no number"], rows["Junk"]

    card = {"name": "Charizard", "set_name": "Base Set", "card_number": "4", "is_holo": True, "condition": "MP"}
    assert other.post(f"/api/listings/external/{char}/assign", json=card).status_code == 404   # not theirs
    assert me.post(f"/api/listings/external/{char}/assign", json={"name": ""}).status_code == 400
    assert me.post(f"/api/listings/external/{char}/assign", json=card).get_json()["quantity"] == 2
    assert me.post(f"/api/listings/external/{char}/assign", json=card).status_code == 404       # once
    assert [(i["name"], i["quantity"], i["condition"]) for i in _have(me)] == [("Charizard", 2, "MP")]

    assert other.post(f"/api/listings/external/{junk}/dismiss").status_code == 404
    assert me.post(f"/api/listings/external/{junk}/dismiss").status_code == 200
    assert me.get("/api/listings/external").get_json()["unmatched"] == []
    assert _have(other) == []


def test_lookups_are_rationed_and_try_again_continues(site, monkeypatch):
    monkeypatch.setattr(listing_import, "LOOKUPS_PER_RUN", 1)
    me = _user(site, "a@example.com")
    r = _upload(me, "whatnot", "Title,Quantity\nDragapult ex 130/167,1\nPikachu 63/198,1\n").get_json()
    assert (r["added"], r["unmatched"]) == (1, 1) and site.lookups == [("130", 167)]
    waiting = me.get("/api/listings/external").get_json()["unmatched"][0]
    assert waiting["detail"].startswith("not looked up yet")

    again = me.post("/api/listings/external/rematch").get_json()
    assert (again["added"], again["still_unmatched"]) == (1, 0)
    assert sorted(i["name"] for i in _have(me)) == ["Dragapult ex", "Pikachu"]


def test_a_failed_lookup_leaves_the_listing_for_later(site, monkeypatch):
    def down(number, total):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(listing_import, "lookup_cards", down)
    me = _user(site, "a@example.com")
    r = _upload(me, "whatnot", "Title,Quantity\nDragapult ex 130/167,1\n").get_json()
    assert (r["added"], r["unmatched"]) == (0, 1) and _have(me) == []


# ---------------------------------------------------------------- eBay

def _ebay_page(items, pages=1, ack="Success", error=""):
    body = "".join(
        f"<Item><ItemID>{i['id']}</ItemID><Title>{i['title']}</Title>"
        + (f"<SKU>{i['sku']}</SKU>" if i.get("sku") else "")
        + f"<QuantityAvailable>{i.get('qty', 1)}</QuantityAvailable>"
        + f"<BuyItNowPrice currencyID=\"{i.get('cur', 'USD')}\">{i.get('price', '0.0')}</BuyItNowPrice>"
        + f"<SellingStatus><CurrentPrice currencyID=\"USD\">{i.get('current', '0.0')}</CurrentPrice></SellingStatus>"
        + f"<ListingDetails><ViewItemURL>https://www.ebay.com/itm/{i['id']}</ViewItemURL></ListingDetails>"
        + ("<Variations><Variation/></Variations>" if i.get("multi") else "")
        + "</Item>" for i in items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<Ack>{ack}</Ack>" + (f"<Errors><LongMessage>{error}</LongMessage></Errors>" if error else "")
        + f"<ActiveList><ItemArray>{body}</ItemArray><PaginationResult>"
        f"<TotalNumberOfPages>{pages}</TotalNumberOfPages></PaginationResult></ActiveList>"
        "</GetMyeBaySellingResponse>")


class _Http:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def post(self, url, data=None, headers=None, timeout=None):
        self.requests.append((url, data.decode(), headers))
        text = self.pages[len(self.requests) - 1]
        return type("R", (), {"status_code": 200, "text": text})()


class _Auth:
    is_sandbox = False

    def access_token(self):
        return "user-token"

    def is_authorized(self):
        return True


def test_ebay_listings_are_read_page_by_page_with_the_documented_request():
    http = _Http([
        _ebay_page([{"id": "111", "title": "Dragapult ex 130/167", "sku": "page01_r0c0", "price": "14.99", "qty": 2}], pages=2),
        _ebay_page([{"id": "222", "title": "Auction card", "current": "3.50"},
                    {"id": "333", "title": "Priced in pounds", "price": "9.00", "cur": "GBP", "multi": True}], pages=2),
    ])
    lines = ebay_listings.fetch_active_listings(_Auth(), session=http)
    assert [(l["external_id"], l["sku"], l["quantity"], l["price"], l["multi"]) for l in lines] == [
        ("111", "page01_r0c0", 2, 14.99, False), ("222", None, 1, 3.5, False), ("333", None, 1, None, True)]
    assert lines[0]["url"] == "https://www.ebay.com/itm/111"
    url, body, headers = http.requests[1]
    assert url == "https://api.ebay.com/ws/api.dll" and "<PageNumber>2</PageNumber>" in body
    assert "<ActiveList><Include>true</Include>" in body and "<EntriesPerPage>200</EntriesPerPage>" in body
    assert headers["X-EBAY-API-CALL-NAME"] == "GetMyeBaySelling" and headers["X-EBAY-API-IAF-TOKEN"] == "user-token"

    with pytest.raises(ebay_listings.EbayListingsError, match="Auth token is invalid"):
        ebay_listings.parse_active_list(_ebay_page([], ack="Failure", error="Auth token is invalid."))


def test_ebay_listings_import_into_the_library_of_the_connected_account(site, monkeypatch):
    me = _user(site, "a@example.com")
    assert me.get("/api/listings/external").get_json()["ebay_connected"] is False
    assert me.post("/api/listings/import/ebay", json={}).status_code == 409

    monkeypatch.setattr(listing_import, "_ebay_auths", lambda s, uid: [_Auth()])
    found = [
        {"external_id": "111", "sku": None, "title": "Dragapult ex 130/167 PSA 10", "quantity": 1, "price": 80.0,
         "url": "https://www.ebay.com/itm/111", "image_url": None, "multi": False},
        {"external_id": "333", "sku": None, "title": "Dragapult ex 130/167 pick your card", "quantity": 5,
         "price": None, "url": None, "image_url": None, "multi": True},
    ]
    monkeypatch.setattr(ebay_listings, "fetch_active_listings", lambda auth: list(found))
    p = me.post("/api/listings/import/ebay", json={"dry_run": True}).get_json()
    assert (p["listings"], p["new"]) == (2, 2) and _have(me) == []
    r = me.post("/api/listings/import/ebay", json={}).get_json()
    assert (r["added"], r["unmatched"]) == (1, 1)
    item = _have(me)[0]
    assert item["notes"] == "graded; see the listing title"
    assert item["listed_on"] == [{"source": "ebay", "site": "eBay", "price": 80.0,
                                  "url": "https://www.ebay.com/itm/111"}]
    assert me.post("/api/listings/import/ebay", json={}).get_json()["duplicates"] == 2

    def refused(auth):
        raise ebay_listings.EbayListingsError("eBay refused the request: Auth token is invalid.")

    monkeypatch.setattr(ebay_listings, "fetch_active_listings", refused)
    r = me.post("/api/listings/import/ebay", json={})
    assert r.status_code == 502 and "Auth token is invalid" in r.get_json()["error"]


def test_the_table_is_new_and_one_listing_is_stored_once_per_account(site):
    a, b = _user(site, "a@example.com"), _user(site, "b@example.com")
    text = "Title,Quantity,SKU\nDragapult ex 130/167,1,same-sku\n"
    _upload(a, "whatnot", text)
    _upload(b, "whatnot", text)                                    # the same id under another account is theirs
    s = models.get_session()
    try:
        assert s.query(models.ExternalListing).count() == 2
    finally:
        s.close()
    assert len(_have(a)) == 1 and len(_have(b)) == 1


CARD_SHEET = """Card,Expansion,Card No,Copies,Grade,Variant,Rarity,Paid,Comments,Market Value,Binder Page
Dragapult ex,Twilight Masquerade,130/167,2,Near Mint,Holofoil,Double Rare,$9.50,pulled myself,14.50,3
Dreepy,Twilight Masquerade,128,,,Reverse Holo,Common,,,0.30,3
Pikachu,Base Set,58,1,PSA 9,,Common,,gift,,7
"""


def test_a_card_list_says_what_it_read_keeps_the_rest_as_a_note_and_says_what_it_filled_in(site):
    me = _user(site, "a@example.com")
    p = _upload(me, "cards", CARD_SHEET, dry_run=True).get_json()
    assert [(c["column"], c["as"]) for c in p["columns_read"]][:4] == [
        ("Card", "card name"), ("Expansion", "set"), ("Card No", "card number"), ("Copies", "quantity")]
    assert {c["column"] for c in p["columns_read"]} == {
        "Card", "Expansion", "Card No", "Copies", "Grade", "Variant", "Rarity", "Paid", "Comments", "Market Value"}
    assert p["columns_noted"] == ["Binder Page"]                         # no field for it: kept in the note
    assert (p["assumed_condition"], p["assumed_quantity"], p["with_market_price"]) == (2, 1, 2)

    _upload(me, "cards", CARD_SHEET)
    have = {i["name"]: i for i in _have(me)}
    drag = have["Dragapult ex"]
    assert (drag["quantity"], drag["condition"], drag["is_holo"], drag["rarity"], drag["acquired_price"]) == (
        2, "NM", True, "Double Rare", 9.5)
    assert drag["notes"] == "pulled myself; Number as written: 130/167; Binder Page: 3"
    assert (drag["price"], drag["provenance"]["notes"]) == (14.5, "From your spreadsheet")
    assert (have["Dreepy"]["is_holo"], have["Dreepy"]["notes"]) == (False, "Printing: Reverse Holo; Binder Page: 3")
    # A grade the app has no field for is kept as written, not thrown away.
    assert have["Pikachu"]["condition"] == "NM"
    assert have["Pikachu"]["notes"] == "gift; Condition as written: PSA 9; Binder Page: 7"
    assert have["Pikachu"]["price"] is None                              # the sheet gave none


# The header row and two rows of a real sheet that an earlier version read badly.
REAL_SHEET = """Source file,Position,Card name,Set,Number,Rarity/variant,Language,Condition (est.),Market price USD,Price source + URL,Suggested list price,ID confidence,Notes
IMG_5834.HEIC,Single card,Pikachu,Pokémon TCG: 30th Celebration (30C),023/128 (01/30),"Pikachu Rare (sparkle foil, x/30 Pikachu subset)",English,"Near Mint (raw, est. from photo)",$0.87,TCGplayer market via TCGVault (Oct 9),$0.99,High,Number read from card
IMG_5840.HEIC,Single card,Jirachi ex,Pokémon TCG: 30th Celebration (30C),155/128,Special Illustration Rare,English,"Near Mint (raw, est. from photo)",$41.20,TCGplayer market via TCGVault (Oct 9),$44.99,High,
"""


def test_a_sheet_read_badly_before_is_repaired_by_reading_it_again(site, monkeypatch):
    from webapp import portfolio, scheduler

    site.register_blueprint(portfolio.portfolio_bp)
    me = _user(site, "a@example.com")
    # What the earlier version made of it: name, set and number only, every
    # card NM and non-holo, and ids that carried the condition and holo flag.
    _upload(me, "cards", "Card name,Set,Number\nPikachu,Pokémon TCG: 30th Celebration (30C),023/128 (01/30)\n"
                         "Jirachi ex,Pokémon TCG: 30th Celebration (30C),155/128\n")
    s = models.get_session()
    try:
        for row in s.query(models.ExternalListing).all():
            row.external_id += "|NM|n"
        for item in s.query(models.InventoryItem).all():
            item.notes = None
        s.commit()
    finally:
        s.close()
    assert [(i["is_holo"], i["rarity"], i["notes"], i["price"]) for i in _have(me)] == [(False, None, None, None)] * 2

    p = _upload(me, "cards", REAL_SHEET, dry_run=True).get_json()
    assert (p["new"], p["will_update"], p["assumed_condition"], p["with_market_price"]) == (0, 2, 0, 2)
    read = {c["column"]: c["as"] for c in p["columns_read"]}
    assert read["Condition (est.)"] == "condition" and read["Rarity/variant"].startswith("rarity")
    assert read["Market price USD"].startswith("market price") and "Price source + URL" in read
    assert p["columns_noted"] == ["Source file", "Position", "Language", "Suggested list price", "ID confidence"]

    r = _upload(me, "cards", REAL_SHEET).get_json()
    assert (r["added"], r["updated"], r["duplicates"]) == (0, 2, 2)
    have = {i["name"]: i for i in _have(me)}
    assert len(have) == 2 and sum(i["quantity"] for i in have.values()) == 2      # repaired, not doubled
    pika = have["Pikachu"]
    assert (pika["is_holo"], pika["condition"], pika["card_number"]) == (True, "NM", "23")
    assert pika["rarity"] == "Pikachu Rare (sparkle foil, x/30 Pikachu subset)"
    assert pika["notes"] == (
        "Number read from card; Condition as written: Near Mint (raw, est. from photo); "
        "Number as written: 023/128 (01/30); Source file: IMG_5834.HEIC; Position: Single card; "
        "Language: English; Suggested list price: $0.99; ID confidence: High")
    assert pika["price"] == 0.87
    assert pika["provenance"]["notes"] == "From your spreadsheet (TCGplayer market via TCGVault (Oct 9))"
    assert (have["Jirachi ex"]["is_holo"], have["Jirachi ex"]["price"]) == (False, 41.2)

    # Reading it a third time changes nothing.
    assert _upload(me, "cards", REAL_SHEET).get_json()["updated"] == 0

    # A live lookup that finds nothing does not erase the sheet's price; one that finds a price replaces it.
    monkeypatch.setattr(scheduler, "_price_fn", lambda card: (
        {"final_price": 1.1, "pricing_confidence": 0.9, "pricing_notes": None} if card["name"] == "Pikachu" else {}))
    assert me.post("/api/portfolio/price-step", json={"mode": "new"}).get_json()["tried"] == 0   # both have a price
    me.post("/api/portfolio/price-step", json={"mode": "all"})
    have = {i["name"]: i for i in _have(me)}
    assert (have["Pikachu"]["price"], have["Jirachi ex"]["price"]) == (1.1, 41.2)


def test_new_cards_are_priced_in_steps_and_the_rest_of_the_library_is_left_alone(site, monkeypatch):
    from webapp import portfolio, scheduler

    site.register_blueprint(portfolio.portfolio_bp)
    asked = []

    def price(card):
        asked.append(card["name"])
        return {} if card["name"] == "Unknown Card" else {"final_price": 2.5, "pricing_confidence": 0.7}

    monkeypatch.setattr(scheduler, "_price_fn", price)
    monkeypatch.setattr(portfolio, "PRICE_STEP", 2)
    me, other = _user(site, "a@example.com"), _user(site, "b@example.com")
    for name in ("Alpha", "Beta", "Unknown Card", "Delta"):
        _add(me, name=name, card_number="1")
    _add(other, name="Someone Elses", card_number="9")

    steps, after = [], 0
    while True:
        r = me.post("/api/portfolio/price-step", json={"mode": "new", "after": after}).get_json()
        steps.append((r["tried"], r["priced"], r["remaining"]))
        after = r["last_id"]
        if not r["tried"] or not r["remaining"]:
            break
    assert steps == [(2, 2, 2), (2, 1, 0)]
    assert asked == ["Alpha", "Beta", "Unknown Card", "Delta"]            # nobody else's cards
    assert {i["name"]: i["price"] for i in _have(me)} == {
        "Alpha": 2.5, "Beta": 2.5, "Unknown Card": None, "Delta": 2.5}

    # Adding one more card prices that card (and retries the one with no price), not the library.
    asked.clear()
    _add(me, name="Epsilon", card_number="5")
    r = me.post("/api/portfolio/price-step", json={"mode": "new"}).get_json()
    assert sorted(asked) == ["Epsilon", "Unknown Card"] and r["remaining"] == 0
    # "all" is the Refresh prices button: every card of this account.
    asked.clear()
    r = me.post("/api/portfolio/price-step", json={"mode": "all"}).get_json()
    assert (r["tried"], r["remaining"]) == (2, 3)
    assert site.test_client().post("/api/portfolio/price-step", json={}).status_code == 401
