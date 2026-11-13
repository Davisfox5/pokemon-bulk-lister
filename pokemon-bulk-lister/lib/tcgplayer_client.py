"""pokemontcg.io wrapper — provides TCGPlayer + Cardmarket prices in one call.

pokemontcg.io exposes both TCGPlayer (USD) and Cardmarket (EUR) prices for the
same card object, so we hit the API once and extract both.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests


BASE_URL = "https://api.pokemontcg.io/v2"


class TCGPlayerClient:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 15) -> None:
        self.api_key = api_key or os.getenv("POKEMONTCG_API_KEY") or ""
        self.timeout = timeout
        self._session = self._new_session()

    def _new_session(self) -> requests.Session:
        # Keep-alive ON for speed (saves ~150ms TCP+TLS handshake per call),
        # but recycle on the first read-timeout — pokemontcg.io's CDN
        # occasionally serves stale pooled connections that silently hang.
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8, pool_maxsize=16, max_retries=0,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        if self.api_key:
            s.headers.update({"X-Api-Key": self.api_key})
        return s

    def _get(self, path: str, params: dict) -> dict:
        url = f"{BASE_URL}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self._session = self._new_session()
                time.sleep(0.5 + attempt * 0.5)
                continue
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        if last_exc is not None:
            raise last_exc
        resp.raise_for_status()
        return {}

    @staticmethod
    def plain_set_name(set_name: Optional[str]) -> Optional[str]:
        """A set name as the catalog spells it. Sheets and listings often
        write "Pokémon TCG: 30th Celebration (30C)"; the catalog calls that
        set "30th Celebration"."""
        import re

        text = re.sub(r"^\s*pok[eé]mon(\s+tcg|\s+trading\s+card\s+game)?\s*[:\-–]\s*", "", set_name or "", flags=re.I)
        text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
        return text or (set_name or None)

    def find_card(
        self,
        name: str,
        set_name: Optional[str] = None,
        set_code: Optional[str] = None,
        card_number: Optional[str] = None,
    ) -> Optional[dict]:
        """Find the best matching card. Most specific identifiers win."""
        clauses: list[str] = []
        if name:
            clauses.append(f'name:"{_escape(name)}"')
        if set_code:
            clauses.append(f'set.id:{_escape(set_code)}')
        elif set_name:
            clauses.append(f'set.name:"{_escape(self.plain_set_name(set_name))}"')
        if card_number:
            clauses.append(f'number:{_escape(card_number)}')

        if not clauses:
            return None

        data = self._get("/cards", {"q": " ".join(clauses), "pageSize": 10})
        cards = data.get("data") or []
        if not cards:
            return None

        if card_number:
            for c in cards:
                if str(c.get("number", "")).lstrip("0") == str(card_number).lstrip("0"):
                    return c
        return cards[0]

    def market_price(self, card: dict, prefer_holo: bool = False) -> Optional[float]:
        """Pull TCGPlayer market price (USD) from the card payload.

        pokemontcg.io exposes TCGPlayer prices under
        card['tcgplayer']['prices'][variant]['market'].
        """
        tcg = (card or {}).get("tcgplayer") or {}
        prices = tcg.get("prices") or {}
        if not prices:
            return None

        priority = (
            ["holofoil", "reverseHolofoil", "normal", "1stEditionHolofoil", "unlimitedHolofoil"]
            if prefer_holo
            else ["normal", "holofoil", "reverseHolofoil", "1stEdition", "unlimited"]
        )

        for variant in priority:
            v = prices.get(variant)
            if v and isinstance(v, dict) and v.get("market"):
                return float(v["market"])

        for variant_data in prices.values():
            if isinstance(variant_data, dict) and variant_data.get("market"):
                return float(variant_data["market"])
        return None

    def cardmarket_trend_eur(self, card: dict, prefer_holo: bool = False) -> Optional[float]:
        """Pull Cardmarket trend price (EUR) from the card payload.

        pokemontcg.io exposes Cardmarket prices under
        card['cardmarket']['prices'] with keys like 'trendPrice', 'averageSellPrice',
        'reverseHoloTrend', etc. We use trendPrice as the main signal.
        """
        cm = (card or {}).get("cardmarket") or {}
        prices = cm.get("prices") or {}
        if not prices:
            return None

        if prefer_holo:
            for key in ("reverseHoloTrend", "trendPrice", "reverseHoloSell", "averageSellPrice"):
                v = prices.get(key)
                if v:
                    return float(v)
        else:
            for key in ("trendPrice", "averageSellPrice", "reverseHoloTrend", "reverseHoloSell"):
                v = prices.get(key)
                if v:
                    return float(v)
        return None

    def lookup_price(
        self,
        name: str,
        set_name: Optional[str] = None,
        set_code: Optional[str] = None,
        card_number: Optional[str] = None,
        is_holo: bool = False,
    ) -> tuple[Optional[float], Optional[dict]]:
        """Backward-compatible: returns (tcgplayer_market_usd, card)."""
        card = self.find_card(name=name, set_name=set_name, set_code=set_code, card_number=card_number)
        if not card:
            return None, None
        return self.market_price(card, prefer_holo=is_holo), card

    def lookup_prices(
        self,
        name: str,
        set_name: Optional[str] = None,
        set_code: Optional[str] = None,
        card_number: Optional[str] = None,
        is_holo: bool = False,
    ) -> dict:
        """One pokemontcg.io call -> both TCGPlayer (USD) and Cardmarket (EUR) prices.

        Returns:
            {
                "card": <card dict or None>,
                "tcgplayer_market_usd": <float or None>,
                "cardmarket_trend_eur": <float or None>,
            }
        """
        card = self.find_card(
            name=name, set_name=set_name, set_code=set_code,
            card_number=card_number,
        )
        if not card:
            return {"card": None, "tcgplayer_market_usd": None, "cardmarket_trend_eur": None}
        return {
            "card": card,
            "tcgplayer_market_usd": self.market_price(card, prefer_holo=is_holo),
            "cardmarket_trend_eur": self.cardmarket_trend_eur(card, prefer_holo=is_holo),
        }


def _escape(value: str) -> str:
    return value.replace('"', '\\"')
