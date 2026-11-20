"""eBay prices for one exact card: sold prices (Marketplace Insights) and,
when those are not to be had, asking prices (Browse API).

A price only counts when its listing is the same card: the same name, set,
card number, finish, edition or stamp and language, and not graded. Every
title is checked against the card with the rules below; a listing that
misses any one is left out. Medians only, never means.

Sold:   GET /buy/marketplace_insights/v1_beta/item_sales/search. Needs the
        buy.marketplace.insights scope, which most keysets are not granted.
Asking: GET /buy/browse/v1/item_summary/search (category 183454, Buy It Now,
        USD), used when Insights refuses or has nothing. Base api_scope.

Fewer than MIN_MATCHES exact matches is too few to price from: the count is
still reported, `usable` is False and the caller leaves it out of the price.
"""
from __future__ import annotations

import base64
import os
import re
import statistics
import sys
import time
from collections import Counter
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import requests


PRODUCTION_BASE = "https://api.ebay.com"
SANDBOX_BASE = "https://api.sandbox.ebay.com"

POKEMON_CATEGORY_ID = "183454"  # Collectible Card Games > Pokémon TCG > Individual Cards
MIN_MATCHES = 3                 # fewer exact matches than this is shown, never priced from
BASE_SCOPE = "https://api.ebay.com/oauth/api_scope"
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"
SEARCH_URL = "https://www.ebay.com/sch/i.html"

# ---------------------------------------------------------------- title rules

# Graded slabs, not raw singles: a grading company with a grade, a grading
# company named alongside "graded", or a slab. "Ungraded" is raw, and "ACE
# SPEC" / "TAG TEAM" cards are not graded (no grade number follows).
_COMPANY = r"(?:psa|bgs|cgc|sgc|tag|ace|beckett)"
GRADED_TITLE_RE = re.compile(
    rf"\b{_COMPANY}\s*[-:#]?\s*(?:10|[1-9](?:\.5)?)(?![\d.])"
    r"|\bslab(?:bed)?\b"
    rf"|\b(?:psa|bgs|cgc|sgc|beckett)\b.*\bgraded\b|\bgraded\b.*\b(?:psa|bgs|cgc|sgc|beckett)\b",
    re.I)

_JAPANESE = re.compile(r"\b(japanese|japan|jp|jpn|jap)\b", re.I)
_OTHER_LANGUAGES = re.compile(
    r"\b(korean|korea|kor|chinese|china|chn|simplified|traditional|t-?chinese|s-?chinese|"
    r"german|deutsch|french|francais|français|italian|italiano|spanish|espanol|español|"
    r"portuguese|thai|indonesian)\b", re.I)

_REVERSE = re.compile(r"\brev(?:erse)?\.?[\s-]*(?:holo(?:foil)?)?\b", re.I)
_NON_HOLO = re.compile(r"\bnon[\s-]?holo(?:foil)?\b", re.I)
_HOLO = re.compile(r"\bholo(?:foil|graphic)?\b", re.I)

# Editions and stamps a card can carry. A listing with one the card does not
# have is another card; a card that has one needs it in the title.
EDITIONS = {
    "1st edition": re.compile(r"\b(?:1st|first)\s*(?:ed(?:ition)?\.?|edition)(?![a-z])", re.I),
    "shadowless": re.compile(r"\bshadow[\s-]?less\b", re.I),
    "staff": re.compile(r"\bstaff\b", re.I),
    "prerelease": re.compile(r"\bpre[\s-]?release\b", re.I),
    "stamped": re.compile(r"\bstamp(?:ed)?\b", re.I),
    "promo": re.compile(r"\bpromos?\b", re.I),
}
# A staff or prerelease card's title says "stamp" too.
_STAMP_KINDS = {"staff", "prerelease", "stamped"}


def log(message: str) -> None:
    """One line on the server log. Never a token, secret or credential."""
    print(f"[ebay] {message}", file=sys.stderr, flush=True)


# Cards that only exist as holos: a listing need not say "holo" for them
# (reverse and non-holo listings are still left out).
_ALWAYS_HOLO_RARITY = re.compile(
    r"holo|radiant|ultra|secret|rainbow|double rare|illustration|hyper|shiny|amazing|"
    r"ace spec|prism|break|legend|\bv\b|vmax|vstar|\bex\b|\bgx\b", re.I)
_ALWAYS_HOLO_NAME = re.compile(r"^radiant\b|\b(ex|gx|v|vmax|vstar|v-union)$", re.I)


def inherently_holo(name: str, rarity: str = "") -> bool:
    return bool(_ALWAYS_HOLO_RARITY.search(rarity or "") or _ALWAYS_HOLO_NAME.search((name or "").strip()))


def _plain(text: str) -> str:
    """Lower case, no accents, '&' as 'and', punctuation as spaces."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9/#]+", " ", text).split())


def editions_in(text: str) -> set[str]:
    return {name for name, pattern in EDITIONS.items() if pattern.search(text or "")}


@dataclass
class CardSpec:
    """The card a listing has to be."""
    name: str
    number: str = ""                 # "4", "004", "4/102", "TG12", "SWSH050"
    total: str = ""                  # printed set total, "102", when known
    set_name: str = ""
    set_codes: list[str] = field(default_factory=list)   # e.g. ptcgo code "MEW"
    finish: str = "normal"           # normal | holofoil | reverseHolofoil
    language: str = "en"             # en | ja
    editions: set[str] = field(default_factory=set)
    always_holo: bool = False        # only printed as a holo: titles need not say so

    def number_text(self) -> str:
        num, total = self.number.split("/")[0].strip(), self.total or ""
        if "/" in self.number:
            total = total or self.number.split("/", 1)[1].strip()
        return f"{num}/{total}" if total else num

    def query(self) -> str:
        """Keywords that find the card; the title rules do the rest."""
        parts = [self.name, self.number_text()]
        if self.finish == "holofoil":
            parts.append("holo")
        elif self.finish == "reverseHolofoil":
            parts.append("reverse holo")
        parts += sorted(e for e in self.editions if e != "promo")
        if self.language == "ja":
            parts.append("japanese")
        return " ".join(p for p in parts if p)


def _number_re(spec: CardSpec) -> Optional[re.Pattern]:
    raw = spec.number.split("/")[0].strip()
    if not raw:
        return None
    m = re.fullmatch(r"([a-zA-Z]*)0*(\d+)([a-zA-Z]?)", raw)
    if not m:
        return re.compile(rf"(?<![a-z0-9]){re.escape(raw.lower())}(?![a-z0-9])", re.I)
    prefix, digits, suffix = m.group(1), m.group(2), m.group(3)
    total = spec.number_text().split("/")[1] if "/" in spec.number_text() else ""
    head = rf"(?<![a-z0-9$.]){re.escape(prefix)}\s*0*{digits}{re.escape(suffix)}"
    if prefix:
        # Lettered numbers (TG12, SV49, SWSH050) are their own format.
        return re.compile(rf"{head}(?![0-9])", re.I)
    if total:
        t_prefix = re.match(r"[a-zA-Z]*", total).group(0)
        t_digits = total[len(t_prefix):].lstrip("0") or "0"
        fraction = rf"{head}\s*/\s*{re.escape(t_prefix)}0*{t_digits}(?![0-9])"
        # Also "#59" and a zero-padded "059" on their own, never a fraction
        # with another total ("059/196") and never a bare "59" (a price, a year).
        return re.compile(rf"{fraction}|#\s*0*{digits}(?![0-9]|\s*/)"
                          rf"|(?<![a-z0-9$.#/])0+{digits}(?![0-9]|\s*/)", re.I)
    # No total known: "4/<total>" or "#4".
    return re.compile(rf"{head}\s*/\s*[a-z]*\d+|#\s*0*{digits}(?![0-9])", re.I)


def reject_reason(title: str, spec: CardSpec) -> Optional[str]:
    """Why a listing is not this card, or None when it is."""
    plain = _plain(title)
    if GRADED_TITLE_RE.search(title):
        return "graded"
    # Language
    if _OTHER_LANGUAGES.search(title):
        return "language"
    if spec.language == "ja":
        if not _JAPANESE.search(title):
            return "language"
    elif _JAPANESE.search(title):
        return "language"
    # Name: every word of it.
    for word in _plain(spec.name).split():
        if not re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", plain):
            return "name"
    # Card number, exact.
    pattern = _number_re(spec)
    if pattern is None or not pattern.search(title):
        return "number"
    # Set: its name, or one of its codes.
    names = [n for n in [_plain(spec.set_name)] if n]
    codes = [c.lower() for c in spec.set_codes if c]
    in_set = any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", plain) for n in names) or \
        any(re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", plain) for c in codes)
    if not in_set:
        return "set"
    # Finish
    reverse = bool(_REVERSE.search(title))
    non_holo = bool(_NON_HOLO.search(title))
    holo = bool(_HOLO.search(_NON_HOLO.sub(" ", title)))
    if spec.finish == "reverseHolofoil":
        if not reverse:
            return "finish"
    elif spec.finish == "holofoil":
        if reverse or non_holo or not (holo or spec.always_holo):
            return "finish"
    elif reverse or holo:
        return "finish"
    # Edition and stamps
    found = editions_in(title)
    wanted = set(spec.editions)
    if wanted & _STAMP_KINDS:
        found -= {"stamped"} - wanted
    if found != wanted:
        return "edition"
    return None


def _log_stats(kind: str, stats: dict) -> None:
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(stats.get("rejected", {}).items())) or "none"
    log(f"{kind}: {stats['query']!r}: {stats['results']} results, {stats['count']} exact matches, "
        f"left out: {reasons}")


def search_link(query: str, sold: bool) -> str:
    """The same search on eBay's site, for a person to check."""
    params = {"_nkw": query, "_sacat": POKEMON_CATEGORY_ID}
    params.update({"LH_Sold": "1", "LH_Complete": "1"} if sold else {"LH_BIN": "1"})
    return f"{SEARCH_URL}?{urlencode(params)}"


def match_stats(items: list[dict], spec: CardSpec, price_key: str, query: str, sold: bool) -> dict:
    """Median, max, min of the exact matches among `items`, with how many
    matched of how many came back."""
    prices: list[float] = []
    rejected: Counter = Counter()
    for it in items:
        reason = reject_reason(it.get("title") or "", spec)
        if reason:
            rejected[reason] += 1
            continue
        price = (it.get(price_key) or {})
        if price.get("value") is None or price.get("currency", "USD") != "USD":
            continue
        try:
            prices.append(float(price["value"]))
        except (TypeError, ValueError):
            continue
    out = {"count": len(prices), "results": len(items), "usable": len(prices) >= MIN_MATCHES,
           "query": query, "url": search_link(query, sold), "rejected": dict(rejected),
           "median": None, "max": None, "min": None}
    if prices:
        out.update(median=round(statistics.median(prices), 2), max=round(max(prices), 2),
                   min=round(min(prices), 2))
    return out


class EbayAuthError(RuntimeError):
    pass


def _scope_names(scopes: str) -> str:
    """'api_scope, buy.marketplace.insights': scope names, nothing secret."""
    return ", ".join(s.rsplit("/", 1)[-1] for s in (scopes or "").split()) or "none"


class EbayClient:
    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        marketplace_id: Optional[str] = None,
        env: Optional[str] = None,
        timeout: int = 20,
    ) -> None:
        self.client_id = client_id or os.getenv("EBAY_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("EBAY_CLIENT_SECRET", "")
        self.marketplace_id = marketplace_id or os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
        env = (env or os.getenv("EBAY_ENV", "production")).lower()
        self.base_url = SANDBOX_BASE if env == "sandbox" else PRODUCTION_BASE
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self.insights = True          # False once eBay says the keyset lacks the scope
        self._session = requests.Session()

    def _auth_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if not self.client_id or not self.client_secret:
            raise EbayAuthError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
        credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()

        def ask(scopes: str):
            return self._session.post(
                f"{self.base_url}/identity/v1/oauth2/token",
                headers={"Authorization": f"Basic {credentials}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials", "scope": scopes},
                timeout=self.timeout)

        wanted = f"{BASE_SCOPE} {INSIGHTS_SCOPE}" if self.insights else BASE_SCOPE
        resp = ask(wanted)
        log(f"token: asked for {_scope_names(wanted)}, eBay answered {resp.status_code}")
        if resp.status_code != 200 and self.insights:
            # A keyset without the Insights scope is refused a token that asks
            # for it. The base scope still covers the Browse API.
            self.insights = False
            wanted = BASE_SCOPE
            resp = ask(wanted)
            log(f"token: asked again for {_scope_names(wanted)} only, eBay answered {resp.status_code}")
        if resp.status_code != 200:
            raise EbayAuthError(f"OAuth failed {resp.status_code}")
        payload = resp.json()
        log(f"token: granted {_scope_names(payload.get('scope') or wanted)}")
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 7200))
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._auth_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict):
        resp = None
        for attempt in range(3):
            resp = self._session.get(f"{self.base_url}{path}", headers=self._headers(),
                                     params=params, timeout=self.timeout)
            if resp.status_code == 401:
                self._token = None
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            break
        return resp

    def search_sold(self, query: str, days: int = 30, condition: Optional[str] = None,
                    limit: int = 200) -> list[dict]:
        """Sold listings (Marketplace Insights). Raises EbayAuthError when the
        keyset has no access."""
        if not self.insights:
            raise EbayAuthError("Marketplace Insights is not available to this keyset")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filters = [f"categoryIds:{{{POKEMON_CATEGORY_ID}}}", "itemLocationCountry:US",
                   f"lastSoldDate:[{cutoff.strftime('%Y-%m-%dT%H:%M:%S.000Z')}..]"]
        if condition:
            filters.insert(1, f"conditions:{{{condition}}}")
        resp = self._get("/buy/marketplace_insights/v1_beta/item_sales/search",
                         {"q": query, "filter": ",".join(filters), "limit": str(min(limit, 200))})
        log(f"insights: {resp.status_code} for {query!r}")
        if resp.status_code == 403:
            self.insights = False
            raise EbayAuthError("Marketplace Insights returned 403 (no buy.marketplace.insights scope)")
        resp.raise_for_status()
        return resp.json().get("itemSales") or []

    def search_asking(self, query: str, limit: int = 200) -> list[dict]:
        """Active Buy It Now listings in USD (Browse API)."""
        resp = self._get("/buy/browse/v1/item_summary/search", {
            "q": query, "category_ids": POKEMON_CATEGORY_ID,
            "filter": "buyingOptions:{FIXED_PRICE},priceCurrency:USD", "limit": str(min(limit, 200))})
        log(f"browse: {resp.status_code} for {query!r}")
        if resp.status_code in (401, 403):
            raise EbayAuthError(f"Browse API returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json().get("itemSummaries") or []

    def card_stats(self, spec: CardSpec, days: int = 30) -> dict:
        """Exact-match eBay prices for one card:
        {"sold": stats or None, "asking": stats or None, "query": str}.
        Asking prices are looked up only when sold ones are not to be had."""
        query = spec.query()
        out: dict = {"sold": None, "asking": None, "query": query}
        self._auth_token()           # learns whether this keyset has Insights at all
        if not self.insights:
            log(f"insights: skipped for {query!r} (this keyset has no access)")
        else:
            try:
                out["sold"] = match_stats(self.search_sold(query, days=days), spec, "lastSoldPrice", query, True)
                _log_stats("sold", out["sold"])
            except EbayAuthError as exc:
                log(f"insights: not used for {query!r}: {exc}")
        if out["sold"] is None or out["sold"]["count"] == 0:
            out["asking"] = match_stats(self.search_asking(query), spec, "price", query, False)
            _log_stats("asking", out["asking"])
        return out

    # ------------------------------------------------ older, query-only use


    def sold_stats(self, query: str, days: int = 30, condition: Optional[str] = None) -> dict:
        """Median / max / count of sold raw listings for a free-text query
        (scripts/03_enrich_pricing.py). Only graded slabs are left out; the
        web app uses card_stats, which matches the exact card."""
        prices: list[float] = []
        for it in self.search_sold(query=query, days=days, condition=condition):
            if GRADED_TITLE_RE.search(it.get("title") or ""):
                continue
            price = it.get("lastSoldPrice") or {}
            if price.get("value") is None or price.get("currency", "USD") != "USD":
                continue
            try:
                prices.append(float(price["value"]))
            except (TypeError, ValueError):
                continue
        if not prices:
            return {"median": None, "max": None, "min": None, "count": 0}
        return {"median": round(statistics.median(prices), 2), "max": round(max(prices), 2),
                "min": round(min(prices), 2), "count": len(prices)}

    def build_query(self, name: str, set_name: Optional[str] = None, card_number: Optional[str] = None) -> str:
        return " ".join(p for p in [name, set_name, card_number, "pokemon"] if p)
