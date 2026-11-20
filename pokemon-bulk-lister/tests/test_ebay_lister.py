"""eBay lister payload builders + publish preconditions (no network)."""
from __future__ import annotations

import pytest

from lib import ebay_lister
from lib.ebay_lister import EbayLister, EbayListingError
from lib.ebay_oauth import EbayUserAuth


@pytest.fixture
def card():
    return {
        "id": 1,
        "crop_path": "output/crops/page01_r0c0.jpg",
        "name": "Charizard",
        "set_name": "Base Set",
        "card_number": "4",
        "rarity": "Rare Holo",
        "is_holo": True,
        "condition_guess": "NM",
        "final_price": 250.0,
        "image_url": "https://img/charizard.jpg",
    }


@pytest.fixture
def lister(tmp_path):
    auth = EbayUserAuth(
        client_id="c", client_secret="s", redirect_uri="r",
        env="production", token_path=str(tmp_path / "tok.json"),
    )
    return EbayLister(auth=auth, env="production")


def test_title_includes_key_fields_and_is_capped(card):
    title = ebay_lister.card_title(card)
    assert title.startswith("Charizard #4 Base Set Holo")
    assert len(title) <= 80


def test_sku_from_crop_stem(card):
    assert EbayLister.card_sku(card) == "page01_r0c0"


def test_inventory_payload(card, lister):
    inv = lister._inventory_item_payload(card)
    assert inv["condition"] == "LIKE_NEW"
    assert inv["product"]["aspects"]["Features"] == ["Holo"]
    assert inv["product"]["imageUrls"] == ["https://img/charizard.jpg"]
    assert inv["availability"]["shipToLocationAvailability"]["quantity"] == 1


def test_condition_mapping(card, lister):
    card["condition_guess"] = "MP"
    assert lister._inventory_item_payload(card)["condition"] == "USED_VERY_GOOD"


def test_publish_dry_run(card, lister):
    res = lister.publish_card(card, dry_run=True)
    assert res["dry_run"] is True
    assert res["sku"] == "page01_r0c0"
    assert res["price"] == 250.0


def test_publish_requires_price(lister):
    with pytest.raises(EbayListingError, match="no price"):
        lister.publish_card({"crop_path": "x.jpg", "image_url": "u"}, dry_run=True)


def test_publish_requires_image(lister):
    with pytest.raises(EbayListingError, match="image_url"):
        lister.publish_card({"crop_path": "x.jpg", "final_price": 5}, dry_run=True)


def test_reverse_holo_finish_is_preserved_in_listing_text_and_features(card, lister):
    card.update(is_holo=False, printing='reverseHolofoil')
    assert 'Reverse Holo' in ebay_lister.card_title(card)
    assert 'Reverse Holo' in ebay_lister.card_description(card)
    assert lister._inventory_item_payload(card)['product']['aspects']['Features'] == ['Reverse Holo']


# ---------------------------------------------------------------- listing text, price and edits

def test_description_is_the_cards_facts_then_the_owners_notes(card):
    plain = ebay_lister.card_description(card)
    assert plain == ebay_lister.card_description(dict(card))           # the same card always reads the same
    assert plain.splitlines()[0].startswith("Charizard, Base Set #4")
    assert "Condition: Near Mint" in plain and "Language: English" in plain
    assert "Ships" not in plain                                        # nothing promised the owner did not say
    assert "Condition" not in ebay_lister.card_description({**card, "condition_guess": ""})
    noted = ebay_lister.card_description({**card, "listing_notes": "Ships in a top loader."})
    assert noted.splitlines()[-1] == "Ships in a top loader."
    japanese = {**card, "set_name": "White Flare (Japanese)"}
    assert "Language: Japanese" in ebay_lister.card_description(japanese)
    assert "(Japanese)" not in ebay_lister.card_description(japanese).splitlines()[0]


def test_the_owners_price_is_the_listing_price(card, lister):
    assert ebay_lister.listing_price({**card, "final_price": 0.17}) == 0.17
    assert ebay_lister.listing_price({**card, "final_price": 0.17, "list_price": 1.0}) == 1.0
    assert ebay_lister.listing_price({"final_price": None, "list_price": None}) is None
    assert lister.publish_card({**card, "final_price": 0.17, "list_price": 1.0}, dry_run=True)["price"] == 1.0


def test_revise_updates_the_item_and_the_offer_in_place(card, lister, monkeypatch):
    calls = []

    class Reply:
        def __init__(self, status):
            self.status_code = status
            self.text = ""

        def json(self):
            return {}

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json")))
        return Reply(204)

    monkeypatch.setattr(lister, "_request", request)
    monkeypatch.setattr(lister, "_resolve_policies", lambda: {"fulfillment": "f", "payment": "p", "return": "r"})
    monkeypatch.setattr(lister, "_resolve_location_key", lambda: "loc")
    out = lister.revise_card({**card, "ebay_offer_id": "OFFER1", "list_price": 1.0, "listing_notes": "Ships fast."})
    assert out == {"sku": "page01_r0c0", "offer_id": "OFFER1", "price": 1.0}
    assert [(m, p) for m, p, _ in calls] == [("PUT", "/sell/inventory/v1/inventory_item/page01_r0c0"),
                                             ("PUT", "/sell/inventory/v1/offer/OFFER1")]
    assert calls[1][2]["pricingSummary"]["price"]["value"] == "1.00"
    assert calls[1][2]["listingDescription"].endswith("Ships fast.")
    # No live offer: nothing is created by an edit.
    monkeypatch.setattr(lister, "_existing_offer_id", lambda sku: None)
    with pytest.raises(ebay_lister.EbayListingError, match="no eBay listing"):
        lister.revise_card({**card, "ebay_offer_id": None})
