"""eBay Sell API lister — creates and publishes live fixed-price listings.

Pipeline per card (the eBay Inventory API model):
    1. PUT  /sell/inventory/v1/inventory_item/{sku}   — product + condition + qty
    2. POST /sell/inventory/v1/offer                  — price, category, policies
    3. POST /sell/inventory/v1/offer/{offerId}/publish → returns listingId

Publishing requires three account-level business policies (payment, return,
fulfillment) and an inventory location. We resolve those once and cache them:
each can be pinned with an env var, otherwise we pick the account's first
policy of each type / first location (creating a stub location if none exist).

Auth is a *user* token (see ``lib/ebay_oauth.py``); the client-credentials
token used for pricing can't create listings.

This is the modern, supported, official path — no scraping, no TOS grey area.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import requests

from lib.ebay_oauth import EbayUserAuth, EbayUserAuthError

PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"

POKEMON_CATEGORY_ID = "183454"  # Collectible Card Games > Pokémon TCG > Individual Cards

# eBay Inventory API condition enum. NM ungraded singles are commonly listed as
# the closest "like new" tier; played grades map down. Override per card via the
# card's condition_guess.
_CONDITION_ENUM = {
    "NM": "LIKE_NEW",
    "LP": "USED_EXCELLENT",
    "MP": "USED_VERY_GOOD",
    "HP": "USED_GOOD",
    "DMG": "USED_ACCEPTABLE",
}


class EbayListingError(RuntimeError):
    pass


def _condition_full(short: str) -> str:
    return {
        "NM": "Near Mint",
        "LP": "Lightly Played",
        "MP": "Moderately Played",
        "HP": "Heavily Played",
        "DMG": "Damaged",
    }.get((short or "NM").upper(), "Near Mint")


def card_title(card: dict) -> str:
    parts = [card.get("name", "")]
    if card.get("card_number"):
        parts.append(f"#{card['card_number']}")
    if card.get("set_name"):
        parts.append(card["set_name"])
    if card.get("printing") == "reverseHolofoil":
        parts.append("Reverse Holo")
    elif card.get("is_holo"):
        parts.append("Holo")
    parts.append("Pokemon TCG")
    return " ".join(p for p in parts if p)[:80]


def card_language(card: dict) -> str:
    return "Japanese" if (card.get("set_name") or "").endswith("(Japanese)") else "English"


def card_finish(card: dict) -> str:
    if card.get("printing") == "reverseHolofoil":
        return "Reverse Holo"
    if card.get("printing") == "unknown":
        return ""
    return "Holo" if card.get("is_holo") else "Non-holo"


def card_description(card: dict) -> str:
    """The listing description: the card's facts, then the owner's own words.

    Built from the card and nothing else, so the same card always reads the
    same. No model writes it. Nothing is promised that the owner did not say:
    how it ships, extra notes on condition and the like go in listing_notes."""
    head = card.get("name") or "Pokemon card"
    where = (card.get("set_name") or "").replace("(Japanese)", "").strip()
    if where:
        head += f", {where}"
    if card.get("card_number"):
        head += f" #{card['card_number']}"
    facts = [("Rarity", card.get("rarity") or ""), ("Finish", card_finish(card)),
             # A condition nobody checked is not stated.
             ("Condition", _condition_full(card["condition_guess"]) if card.get("condition_guess") else ""),
             ("Language", card_language(card))]
    lines = [head + ".", " | ".join(f"{k}: {v}" for k, v in facts if v) + "."]
    # An AI-written description (eBay's, or ours when eBay's is not to be
    # had) takes the place of the facts when the owner asked for one.
    written = (card.get("ai_description") or "").strip()
    if written:
        lines = [written]
    notes = (card.get("listing_notes") or "").strip()
    if notes:
        lines.append(notes)
    return "\n".join(lines)


def listing_price(card: dict) -> Optional[float]:
    """What a card lists for: the owner's own price when they set one,
    otherwise the worked-out estimate."""
    for key in ("list_price", "final_price", "price"):
        try:
            value = float(card.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def card_aspects(card: dict) -> dict[str, list[str]]:
    """eBay item specifics for a card, from its facts."""
    aspects: dict[str, list[str]] = {
        "Game": ["Pokémon TCG"],
        "Language": [card_language(card)],
    }
    if card.get("name"):
        aspects["Card Name"] = [card["name"]]
    if card.get("set_name"):
        aspects["Set"] = [card["set_name"]]
    if card.get("card_number"):
        aspects["Card Number"] = [str(card["card_number"])]
    if card.get("rarity"):
        aspects["Rarity"] = [card["rarity"]]
    if card.get("printing") == "reverseHolofoil":
        aspects["Features"] = ["Reverse Holo"]
    elif card.get("is_holo"):
        aspects["Features"] = ["Holo"]
    return aspects


class EbayLister:
    def __init__(
        self,
        auth: Optional[EbayUserAuth] = None,
        marketplace_id: Optional[str] = None,
        env: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self.auth = auth or EbayUserAuth(env=env)
        self.marketplace_id = marketplace_id or os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
        env = (env or os.getenv("EBAY_ENV", "production")).lower()
        self.base_url = SANDBOX_BASE if env == "sandbox" else PRODUCTION_BASE
        self.timeout = timeout
        self._session = requests.Session()
        # Lazily-resolved account context.
        self._policies: Optional[dict[str, str]] = None
        self._location_key: Optional[str] = None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self.auth.access_token()}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
            "Accept-Language": "en-US",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
        }
        if extra:
            headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = self._session.request(
            method, url, headers=self._headers(kwargs.pop("headers", None)), timeout=self.timeout, **kwargs
        )
        if resp.status_code == 401:
            # Token may have lapsed mid-run; force a refresh and retry once.
            self.auth._access_token = None  # noqa: SLF001 — intentional invalidate
            resp = self._session.request(
                method, url, headers=self._headers(), timeout=self.timeout, **kwargs
            )
        return resp

    @staticmethod
    def _err(resp: requests.Response) -> str:
        try:
            data = resp.json()
            errs = data.get("errors") or data.get("warnings") or []
            if errs:
                return "; ".join(f"{e.get('errorId')}:{e.get('message')}" for e in errs)
        except ValueError:
            pass
        return resp.text[:500]

    # ------------------------------------------------------------------
    # Account context (policies + location), resolved once.
    # ------------------------------------------------------------------

    def _resolve_policies(self) -> dict[str, str]:
        if self._policies is not None:
            return self._policies

        pinned = {
            "fulfillment": os.getenv("EBAY_FULFILLMENT_POLICY_ID", ""),
            "payment": os.getenv("EBAY_PAYMENT_POLICY_ID", ""),
            "return": os.getenv("EBAY_RETURN_POLICY_ID", ""),
        }

        endpoints = {
            "fulfillment": ("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId"),
            "payment": ("payment_policy", "paymentPolicies", "paymentPolicyId"),
            "return": ("return_policy", "returnPolicies", "returnPolicyId"),
        }
        resolved: dict[str, str] = {}
        for kind, (path, list_key, id_key) in endpoints.items():
            if pinned[kind]:
                resolved[kind] = pinned[kind]
                continue
            resp = self._request(
                "GET", f"/sell/account/v1/{path}", params={"marketplace_id": self.marketplace_id}
            )
            if resp.status_code != 200:
                raise EbayListingError(
                    f"could not list {kind} policies ({resp.status_code}: {self._err(resp)}). "
                    f"Opt into Business Policies in eBay account settings, or pin "
                    f"EBAY_{kind.upper()}_POLICY_ID in .env."
                )
            items = resp.json().get(list_key) or []
            if not items:
                raise EbayListingError(
                    f"no {kind} business policy exists for {self.marketplace_id}. "
                    f"Create one in Seller Hub, or pin EBAY_{kind.upper()}_POLICY_ID."
                )
            resolved[kind] = items[0][id_key]

        self._policies = resolved
        return resolved

    def _resolve_location_key(self) -> str:
        if self._location_key is not None:
            return self._location_key

        pinned = os.getenv("EBAY_MERCHANT_LOCATION_KEY", "")
        if pinned:
            self._location_key = pinned
            return pinned

        resp = self._request("GET", "/sell/inventory/v1/location")
        if resp.status_code == 200:
            locations = resp.json().get("locations") or []
            if locations:
                self._location_key = locations[0]["merchantLocationKey"]
                return self._location_key

        # None exist — create a minimal one from env-provided address.
        key = "pokemon-bulk-lister"
        body = {
            "location": {
                "address": {
                    "country": os.getenv("EBAY_LOCATION_COUNTRY", "US"),
                    "postalCode": os.getenv("EBAY_LOCATION_POSTAL_CODE", "10001"),
                }
            },
            "locationInstructions": "Pokemon card singles",
            "name": "Pokemon Bulk Lister",
            "merchantLocationStatus": "ENABLED",
            "locationTypes": ["WAREHOUSE"],
        }
        resp = self._request("POST", f"/sell/inventory/v1/location/{key}", json=body)
        if resp.status_code not in (200, 201, 204):
            # A 409 means it already exists, which is fine.
            if resp.status_code != 409:
                raise EbayListingError(
                    f"could not resolve/create an inventory location ({resp.status_code}: "
                    f"{self._err(resp)}). Pin EBAY_MERCHANT_LOCATION_KEY in .env."
                )
        self._location_key = key
        return key

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    @staticmethod
    def card_sku(card: dict) -> str:
        sku = Path(card.get("crop_path", "")).stem
        return sku or f"card-{card.get('id', 'x')}"

    def _inventory_item_payload(self, card: dict) -> dict:
        condition = _CONDITION_ENUM.get((card.get("condition_guess") or "NM").upper(), "LIKE_NEW")
        aspects = card_aspects(card)

        image_urls = [card["image_url"]] if card.get("image_url") else []
        return {
            "availability": {"shipToLocationAvailability": {"quantity": 1}},
            "condition": condition,
            "product": {
                "title": card_title(card),
                "description": card_description(card),
                "aspects": aspects,
                "imageUrls": image_urls,
            },
        }

    def _offer_payload(self, card: dict, price: float) -> dict:
        policies = self._resolve_policies()
        return {
            "sku": self.card_sku(card),
            "marketplaceId": self.marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": 1,
            "categoryId": POKEMON_CATEGORY_ID,
            "listingDescription": card_description(card),
            "listingPolicies": {
                "fulfillmentPolicyId": policies["fulfillment"],
                "paymentPolicyId": policies["payment"],
                "returnPolicyId": policies["return"],
            },
            "pricingSummary": {"price": {"value": f"{price:.2f}", "currency": "USD"}},
            "merchantLocationKey": self._resolve_location_key(),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish_card(self, card: dict, dry_run: bool = False) -> dict:
        """Create + publish a single live listing. Returns a result dict.

        Raises ``EbayListingError`` on any API failure. The caller should have
        already filtered out cards without a price or an image.
        """
        price = listing_price(card)
        if not price:
            raise EbayListingError("card has no price; price the card or set a list price first")
        if not card.get("image_url"):
            raise EbayListingError("card has no image_url; push the photo to Cloudinary first")

        sku = self.card_sku(card)
        if dry_run:
            return {"sku": sku, "dry_run": True, "title": card_title(card), "price": round(price, 2)}

        # 1. Inventory item (idempotent PUT).
        resp = self._request(
            "PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=self._inventory_item_payload(card)
        )
        if resp.status_code not in (200, 201, 204):
            raise EbayListingError(f"inventory_item failed: {resp.status_code}: {self._err(resp)}")

        # 2. Create the offer (or reuse an existing one for this SKU).
        resp = self._request("POST", "/sell/inventory/v1/offer", json=self._offer_payload(card, price))
        if resp.status_code in (200, 201):
            offer_id = resp.json().get("offerId")
        elif resp.status_code == 409:
            offer_id = self._existing_offer_id(sku)
            if offer_id:
                # Keep price/policies current on the existing offer.
                self._request(
                    "PUT", f"/sell/inventory/v1/offer/{offer_id}", json=self._offer_payload(card, price)
                )
            else:
                raise EbayListingError(f"offer conflict but no existing offer found: {self._err(resp)}")
        else:
            raise EbayListingError(f"create offer failed: {resp.status_code}: {self._err(resp)}")

        if not offer_id:
            raise EbayListingError("no offerId returned")

        # 3. Publish → live listing.
        resp = self._request("POST", f"/sell/inventory/v1/offer/{offer_id}/publish")
        if resp.status_code not in (200, 201):
            raise EbayListingError(f"publish failed: {resp.status_code}: {self._err(resp)}")
        listing_id = resp.json().get("listingId")

        item_url = (
            f"https://www.ebay.com/itm/{listing_id}"
            if listing_id and self.base_url == PRODUCTION_BASE
            else None
        )
        return {"sku": sku, "offer_id": offer_id, "listing_id": listing_id, "url": item_url}

    def revise_card(self, card: dict) -> dict:
        """Bring a card's live listing up to date with the card as it is now:
        title, description, condition, photo and price. The listing keeps its
        id. Raises ``EbayListingError`` when the card has no live offer or
        eBay refuses the change."""
        price = listing_price(card)
        if not price:
            raise EbayListingError("card has no price; price the card or set a list price first")
        sku = self.card_sku(card)
        offer_id = card.get("ebay_offer_id") or self._existing_offer_id(sku)
        if not offer_id:
            raise EbayListingError("this card has no eBay listing to update")
        resp = self._request(
            "PUT", f"/sell/inventory/v1/inventory_item/{sku}", json=self._inventory_item_payload(card)
        )
        if resp.status_code not in (200, 201, 204):
            raise EbayListingError(f"inventory_item failed: {resp.status_code}: {self._err(resp)}")
        # Updating a published offer revises the live listing in place.
        resp = self._request(
            "PUT", f"/sell/inventory/v1/offer/{offer_id}", json=self._offer_payload(card, price)
        )
        if resp.status_code not in (200, 204):
            raise EbayListingError(f"update offer failed: {resp.status_code}: {self._err(resp)}")
        return {"sku": sku, "offer_id": offer_id, "price": round(price, 2)}

    def _existing_offer_id(self, sku: str) -> Optional[str]:
        resp = self._request("GET", "/sell/inventory/v1/offer", params={"sku": sku})
        if resp.status_code != 200:
            return None
        offers = resp.json().get("offers") or []
        return offers[0].get("offerId") if offers else None


def preflight() -> dict:
    """Cheap readiness probe for the UI: is the user authorized?"""
    try:
        auth = EbayUserAuth()
    except EbayUserAuthError as exc:
        return {"ready": False, "reason": str(exc)}
    return {
        "ready": auth.is_authorized(),
        "reason": "" if auth.is_authorized() else "not authorized — run python -m webapp.setup_ebay",
    }
