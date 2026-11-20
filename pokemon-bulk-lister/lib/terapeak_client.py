"""Terapeak (eBay Seller Hub) headless scraper.

Terapeak Research has no public API. It does have ~365 days of real sold history,
free for any Seller Hub seller. This module drives it via Playwright.

eBay's Terapeak Subscription Terms ban automated access. This is a TOS violation;
use is at your own risk. Risk is concentrated on the eBay account used — keep it
to a throwaway/secondary account if possible.

First-run flow:
  1. Browser opens headful with a fresh context.
  2. You manually log in to ebay.com -> Seller Hub once.
  3. The session storage_state is saved to TERAPEAK_STATE_PATH.
  4. Subsequent runs load that state and run headless.

Proxy rotation: comma-separated list in TERAPEAK_PROXIES is round-robined per
search (a new Playwright context is created for each lookup so the proxy can
change). Skip the env var to run without a proxy.
"""
from __future__ import annotations

import os
import random
import re
import statistics
import time
from pathlib import Path
from typing import Optional

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Page,
        Playwright,
        TimeoutError as PWTimeoutError,
        sync_playwright,
    )
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Terapeak scraping requires playwright. Install with:\n"
        "  pip install playwright && playwright install chromium"
    ) from exc


TERAPEAK_URL = "https://www.ebay.com/sh/research"


class TerapeakNotLoggedIn(RuntimeError):
    """Raised when no Terapeak session is on disk. Run the setup script."""


class TerapeakClient:
    """Round-robin proxy, persistent session, rate-limited Terapeak lookups."""

    def __init__(
        self,
        state_path: Optional[str] = None,
        headless: Optional[bool] = None,
        min_delay: Optional[float] = None,
        max_jitter: Optional[float] = None,
        proxies: Optional[list[str]] = None,
    ) -> None:
        self.state_path = Path(state_path or os.getenv("TERAPEAK_STATE_PATH", "output/cache/terapeak_state.json"))
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.headless = headless if headless is not None else os.getenv("TERAPEAK_HEADLESS", "1") == "1"
        self.min_delay = float(min_delay if min_delay is not None else os.getenv("TERAPEAK_MIN_DELAY", "4"))
        self.max_jitter = float(max_jitter if max_jitter is not None else os.getenv("TERAPEAK_MAX_JITTER", "4"))
        env_proxies = os.getenv("TERAPEAK_PROXIES", "").strip()
        self.proxies = proxies if proxies is not None else (
            [p.strip() for p in env_proxies.split(",") if p.strip()] if env_proxies else []
        )
        self._proxy_idx = 0
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._last_search_at: float = 0.0
        self._ensure_logged_in()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _start_browser(self) -> None:
        if self._pw is not None:
            return
        self._pw = sync_playwright().start()
        # Stealth flags MUST match setup_terapeak.py — without these, eBay's
        # bot detector flags `navigator.webdriver` and serves splashui/captcha
        # on every request, invalidating the saved session after a few hits.
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def _next_proxy(self) -> Optional[dict]:
        if not self.proxies:
            return None
        proxy = self.proxies[self._proxy_idx % len(self.proxies)]
        self._proxy_idx += 1
        return {"server": proxy}

    def _new_context(self) -> BrowserContext:
        self._start_browser()
        assert self._browser is not None
        kwargs: dict = {
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7632.6 Safari/537.36"
            ),
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
        }
        if self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)
        proxy = self._next_proxy()
        if proxy:
            kwargs["proxy"] = proxy
        ctx = self._browser.new_context(**kwargs)
        # Mask navigator.webdriver and other automation tells per-context.
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
            "Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});"
            "window.chrome = {runtime: {}};"
        )
        return ctx

    # ------------------------------------------------------------------
    # Login state check (no auto-login — that needs a real terminal)
    # ------------------------------------------------------------------

    def _ensure_logged_in(self) -> None:
        if self.state_path.exists():
            return
        raise TerapeakNotLoggedIn(
            f"No Terapeak session at {self.state_path}. "
            "Run from a terminal:\n"
            "    python -m webapp.setup_terapeak\n"
            "A Chromium window will open; sign in to eBay and the script will save the session automatically."
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _sleep_with_jitter(self) -> None:
        elapsed = time.time() - self._last_search_at
        target = self.min_delay + random.random() * self.max_jitter
        wait = target - elapsed
        if wait > 0:
            time.sleep(wait)

    def search(self, query: str, days: int = 365, condition: str = "Used") -> dict:
        """Run one Terapeak Research search. Returns {median, count, prices}.

        Defensive: tries multiple selector strategies, falls back to regex over
        rendered text. Logs the page HTML on parse failure so selectors can be
        updated.
        """
        self._sleep_with_jitter()
        context = self._new_context()
        page = context.new_page()
        try:
            return self._do_search(page, query, days=days, condition=condition)
        finally:
            self._last_search_at = time.time()
            try:
                context.close()
            except Exception:
                pass

    def _do_search(self, page: Page, query: str, days: int, condition: str) -> dict:
        page.goto(TERAPEAK_URL, wait_until="domcontentloaded", timeout=45000)
        # If we got bounced to signin, the saved session is dead.
        if "signin" in page.url:
            raise RuntimeError(
                f"Terapeak redirected to {page.url} — saved session expired. "
                f"Delete {self.state_path} and rerun to log in again."
            )

        # Best-effort: find the search input and submit the query.
        # Terapeak's input is typically a single text box near the top.
        submitted = False
        for sel in [
            'input[placeholder*="Search" i]',
            'input[type="search"]',
            'input[name="keywords"]',
            'input[aria-label*="Search" i]',
        ]:
            try:
                box = page.locator(sel).first
                box.wait_for(state="visible", timeout=8000)
                box.fill(query)
                box.press("Enter")
                submitted = True
                break
            except PWTimeoutError:
                continue
            except Exception:
                continue
        if not submitted:
            self._dump_debug(page, "no-search-box")
            return {"median": None, "max": None, "min": None, "count": 0, "prices": []}

        # Wait for results to render. Terapeak shows summary stats + a table.
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeoutError:
            pass

        prices = self._extract_prices(page)
        if not prices:
            # Try once more after a longer wait — Terapeak sometimes lazy-loads.
            time.sleep(3)
            prices = self._extract_prices(page)
        if not prices:
            self._dump_debug(page, f"no-prices-{_safe_slug(query)}")
            return {"median": None, "max": None, "min": None, "count": 0, "prices": []}

        return {
            "median": round(statistics.median(prices), 2),
            "max": round(max(prices), 2),
            "min": round(min(prices), 2),
            "count": len(prices),
            "prices": prices,
        }

    def _extract_prices(self, page: Page) -> list[float]:
        """Pull sold-price values from the results page.

        Strategy: walk anchor/cell elements that look like prices, normalize.
        eBay's Terapeak DOM uses unstable class names so we regex the visible
        text as a backstop.
        """
        # Strategy A: structured cells.
        candidates: list[str] = []
        for sel in [
            '[data-test-id*="price" i]',
            '[class*="price" i]',
            'td:has-text("$")',
            'span:has-text("$")',
        ]:
            try:
                for el in page.locator(sel).all()[:200]:
                    text = el.inner_text(timeout=500).strip()
                    if text:
                        candidates.append(text)
            except Exception:
                continue

        prices = _parse_usd_values(" \n ".join(candidates))

        # Strategy B: backstop on the full page text.
        if not prices:
            try:
                body = page.inner_text("body")
            except Exception:
                body = ""
            prices = _parse_usd_values(body)

        # Drop obviously-bogus extremes (eBay totals like "$0.99 - $10,000").
        return [p for p in prices if 0.10 <= p <= 100000.0]

    def _dump_debug(self, page: Page, tag: str) -> None:
        debug_dir = Path("output/cache/terapeak_debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        try:
            page.screenshot(path=str(debug_dir / f"{ts}-{tag}.png"), full_page=True)
        except Exception:
            pass
        try:
            (debug_dir / f"{ts}-{tag}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass


_USD_RE = re.compile(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")


def _parse_usd_values(text: str) -> list[float]:
    out: list[float] = []
    for match in _USD_RE.finditer(text or ""):
        try:
            out.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return out


def _safe_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower())[:40].strip("-") or "query"
