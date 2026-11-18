"""eBay's own AI listing writer: the Inventory Mapping API.

eBay takes up to 10 products per request (title, item specifics, photos)
and, a few minutes later, returns a listing preview for each, with an
AI-written title and description. It is a GraphQL API on eBay's federated
gateway, U.S. marketplace only, and needs its own OAuth scope
(sell.inventory.mapping) on the seller's token.

Shapes are from eBay's published schema
(developer.ebay.com/develop/api/spec/inventory_mapping.graphqls) and the
"Using eBay GraphQL APIs" and "Listing Creation" guides, read 18 Nov 2026:

  mutation startListingPreviewsCreation(input: {externalProducts: [...]})
      -> {errors: [{errorDescription}], listingPreviewsCreationTask: {id}}
  query listingPreviewsCreationTaskById(input: {id})
      -> {listingPreviewsCreationTask: {id, result}}; result is null until
         the task finishes, then {completionStatus: COMPLETED |
         COMPLETED_WITH_ERROR | FAILED, listingPreviews: [{sku, title,
         description, ...}], invalidProducts, unmappedProducts,
         unprocessedProducts}

Not yet run against eBay itself: no eBay account is connected to this app.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests

from lib.ebay_lister import EbayLister, card_aspects, card_title

GRAPHQL_URL = "https://graphqlapi.ebay.com/graphql"
# eBay's guide names only the production gateway. The sandbox host follows
# eBay's usual pattern and is unconfirmed.
SANDBOX_GRAPHQL_URL = "https://graphqlapi.sandbox.ebay.com/graphql"
MAPPING_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory.mapping"
BATCH_SIZE = 10            # eBay's maximum products per request
MARKETPLACE = "EBAY_US"    # the only marketplace eBay supports for this

_START = """
mutation Start($input: StartListingPreviewsCreationInput!) {
  startListingPreviewsCreation(input: $input) {
    errors { errorDescription }
    listingPreviewsCreationTask { id }
  }
}"""

_TASK = """
query Task($input: ListingPreviewsCreationTaskByIdInput!) {
  listingPreviewsCreationTaskById(input: $input) {
    listingPreviewsCreationTask {
      id
      result {
        completionStatus
        listingPreviews { sku description clientProvidedProductDetails { sku } }
        invalidProducts { description clientProvidedProductDetails { sku } }
        unmappedProducts { sku }
        unprocessedProducts { sku }
      }
    }
  }
}"""


class EbayMappingError(RuntimeError):
    pass


class EbayMappingLimit(EbayMappingError):
    """eBay refused because the day's allowance is used up."""


def product_input(card: dict) -> dict:
    """One card as eBay's ExternalProductDetailsInput. The card's own facts
    only: the owner's notes are added under eBay's text, never sent."""
    aspects = [{"name": name, "values": values} for name, values in card_aspects(card).items()]
    product: dict[str, Any] = {"sku": EbayLister.card_sku(card), "title": card_title(card), "aspects": aspects}
    image = card.get("image_url") or ""
    if image.startswith(("http://", "https://")):
        product["images"] = [image]
    return product


class EbayMapping:
    def __init__(self, auth, env: Optional[str] = None, session: Optional[requests.Session] = None):
        self.auth = auth
        env = (env or os.getenv("EBAY_ENV", "production")).lower()
        self.url = SANDBOX_GRAPHQL_URL if env == "sandbox" else GRAPHQL_URL
        self.session = session or requests.Session()

    def ready(self) -> bool:
        """The connected eBay account granted the scope this needs. A token
        from before the scope was asked for has not: reconnecting eBay
        grants it."""
        try:
            return bool(self.auth.has_scope(MAPPING_SCOPE))
        except Exception:
            return False

    def _post(self, query: str, variables: dict) -> dict:
        try:
            resp = self.session.post(self.url, json={"query": query, "variables": variables}, timeout=30, headers={
                "Authorization": f"Bearer {self.auth.access_token()}",
                "Content-Type": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE,
            })
        except requests.RequestException as exc:
            raise EbayMappingError(f"eBay could not be reached: {exc}") from exc
        if resp.status_code == 429:
            raise EbayMappingLimit("eBay's daily allowance for listing previews is used up")
        try:
            body = resp.json()
        except ValueError:
            raise EbayMappingError(f"eBay answered {resp.status_code} with no JSON")
        errors = body.get("errors") or []
        if errors:
            text = "; ".join(str(e.get("message") or e) for e in errors)[:300]
            codes = " ".join(str((e.get("extensions") or {}).get("code") or "") for e in errors).lower()
            if "rate" in codes or "limit" in codes or "too many" in text.lower():
                raise EbayMappingLimit(text)
            raise EbayMappingError(text)
        if resp.status_code >= 400:
            raise EbayMappingError(f"eBay answered {resp.status_code}")
        return body.get("data") or {}

    def start(self, cards: list[dict]) -> str:
        """Ask eBay to write listings for up to 10 cards. Returns the task id."""
        if not cards or len(cards) > BATCH_SIZE:
            raise ValueError(f"send 1 to {BATCH_SIZE} cards")
        data = self._post(_START, {"input": {"externalProducts": [product_input(c) for c in cards]}})
        out = data.get("startListingPreviewsCreation") or {}
        problems = [e.get("errorDescription") for e in out.get("errors") or [] if e.get("errorDescription")]
        task = (out.get("listingPreviewsCreationTask") or {}).get("id")
        if not task:
            raise EbayMappingError("; ".join(problems) or "eBay returned no task id")
        return str(task)

    def result(self, task_id: str) -> Optional[dict]:
        """None while eBay is still working. Then {"status", "descriptions":
        {sku: text}, "failed": [sku, ...]}; a card eBay could not write for
        is in "failed"."""
        data = self._post(_TASK, {"input": {"id": task_id}})
        task = (data.get("listingPreviewsCreationTaskById") or {}).get("listingPreviewsCreationTask") or {}
        result = task.get("result")
        if not result:
            return None
        descriptions: dict[str, str] = {}
        for preview in result.get("listingPreviews") or []:
            sku = preview.get("sku") or (preview.get("clientProvidedProductDetails") or {}).get("sku")
            text = (preview.get("description") or "").strip()
            if sku and text:
                descriptions[str(sku)] = text
        failed = [str((p.get("clientProvidedProductDetails") or {}).get("sku"))
                  for p in result.get("invalidProducts") or []]
        failed += [str(p.get("sku")) for key in ("unmappedProducts", "unprocessedProducts")
                   for p in result.get(key) or []]
        return {"status": result.get("completionStatus") or "FAILED", "descriptions": descriptions,
                "failed": [s for s in failed if s and s != "None" and s not in descriptions]}
