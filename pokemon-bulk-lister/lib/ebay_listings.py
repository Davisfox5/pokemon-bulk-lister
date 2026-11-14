"""Read a seller's active eBay listings (Trading API, GetMyeBaySelling).

The Sell Inventory API this app publishes through only knows listings that
were created through it. Listings made on eBay's own site or in its app are
only visible through the older Trading API, so that is what is used here.

Request and response shapes are taken from eBay's published Trading schema
(ebaySvc.xsd, version 1489):

  POST {base}/ws/api.dll        XML body, namespace urn:ebay:apis:eBLBaseComponents
  GetMyeBaySellingRequest
    ActiveList / Include, Pagination / EntriesPerPage (max 200), PageNumber
  GetMyeBaySellingResponse
    Ack, Errors / LongMessage
    ActiveList / ItemArray / Item[]
        ItemID, Title, SKU, Quantity, QuantityAvailable, BuyItNowPrice,
        SellingStatus / CurrentPrice, ListingDetails / ViewItemURL,
        PictureDetails / GalleryURL, Variations
    ActiveList / PaginationResult / TotalNumberOfPages

Not checked against a live account in this environment: that the OAuth user
token this app already holds is accepted in the X-EBAY-API-IAF-TOKEN header
with the scopes it was granted (eBay documents that header for OAuth tokens
on Trading calls). If eBay refuses, the error text is passed through.

Nothing here writes to eBay.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

import requests

from lib.ebay_oauth import EbayUserAuth

PRODUCTION_URL = "https://api.ebay.com/ws/api.dll"
SANDBOX_URL = "https://api.sandbox.ebay.com/ws/api.dll"
NS = "{urn:ebay:apis:eBLBaseComponents}"
COMPATIBILITY_LEVEL = "1489"
PAGE_SIZE = 200     # the call's maximum
MAX_PAGES = 25      # 5,000 active listings


class EbayListingsError(RuntimeError):
    pass


def _text(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    found = node.find("/".join(NS + part for part in path.split("/")))
    return found.text.strip() if found is not None and found.text else None


def _usd(node: Optional[ET.Element], path: str) -> Optional[float]:
    """An amount, only when it is in US dollars and above zero."""
    if node is None:
        return None
    found = node.find("/".join(NS + part for part in path.split("/")))
    if found is None or not found.text or found.get("currencyID") not in (None, "USD"):
        return None
    try:
        value = float(found.text)
    except ValueError:
        return None
    return round(value, 2) if value > 0 else None


def _request_body(page: int) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        "<ActiveList><Include>true</Include><Pagination>"
        f"<EntriesPerPage>{PAGE_SIZE}</EntriesPerPage><PageNumber>{page}</PageNumber>"
        "</Pagination></ActiveList>"
        "</GetMyeBaySellingRequest>"
    )


def parse_active_list(xml_text: str) -> tuple[list[dict], int]:
    """One response page -> (listing lines, total pages)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EbayListingsError(f"eBay sent something that is not XML: {exc}") from exc
    if _text(root, "Ack") not in ("Success", "Warning"):
        message = _text(root, "Errors/LongMessage") or _text(root, "Errors/ShortMessage") or "no reason given"
        raise EbayListingsError(f"eBay refused the request: {message}")
    active = root.find(NS + "ActiveList")
    lines: list[dict] = []
    if active is None:
        return lines, 0
    for item in active.findall(f"{NS}ItemArray/{NS}Item"):
        item_id = _text(item, "ItemID")
        if not item_id:
            continue
        available = _text(item, "QuantityAvailable") or _text(item, "Quantity") or "1"
        try:
            quantity = int(available)
        except ValueError:
            quantity = 1
        lines.append({
            "external_id": item_id,
            "sku": _text(item, "SKU"),
            "title": _text(item, "Title") or "",
            "quantity": quantity,
            "price": _usd(item, "BuyItNowPrice") or _usd(item, "SellingStatus/CurrentPrice"),
            "url": _text(item, "ListingDetails/ViewItemURL"),
            "image_url": _text(item, "PictureDetails/GalleryURL"),
            # One listing with several variations is not one card.
            "multi": item.find(NS + "Variations") is not None,
        })
    pages = _text(active, "PaginationResult/TotalNumberOfPages")
    return lines, int(pages) if pages and pages.isdigit() else 1


def fetch_active_listings(auth: EbayUserAuth, session: Optional[Any] = None,
                          timeout: int = 30) -> list[dict]:
    """Every active listing on the connected eBay account."""
    http = session or requests.Session()
    url = SANDBOX_URL if auth.is_sandbox else PRODUCTION_URL
    lines: list[dict] = []
    page = 1
    while page <= MAX_PAGES:
        resp = http.post(
            url,
            data=_request_body(page).encode("utf-8"),
            headers={
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
                "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
                "X-EBAY-API-IAF-TOKEN": auth.access_token(),
                "Content-Type": "text/xml",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise EbayListingsError(f"eBay GetMyeBaySelling failed {resp.status_code}: {resp.text[:300]}")
        found, pages = parse_active_list(resp.text)
        lines.extend(found)
        if page >= pages:
            break
        page += 1
    return lines
