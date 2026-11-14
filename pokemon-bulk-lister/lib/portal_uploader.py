"""Shared headless-browser base for seller-portal CSV uploads.

TCGPlayer and Whatnot have no public listing API for the typical seller, so the
only way to automate inventory is to drive their Seller Portal in a browser the
way a person would: log in once (session saved to disk), then attach the bulk
CSV this app already generates and submit it.

⚠️ This is browser automation of sites whose Terms generally prohibit it. It is
fragile (their DOM changes without notice) and use is at your own risk — keep it
to a personal/secondary account. The official, supported alternative is to just
upload the CSVs by hand; this module only saves the clicks.

Design mirrors ``lib/terapeak_client.py``: same stealth flags, the same
storage_state session reuse, and the same defensive "try several selectors then
dump a screenshot + HTML on failure" parsing so selectors can be tuned without
re-running the whole pipeline. Subclasses supply the portal URL and the selector
candidates for the file input and submit control.
"""
from __future__ import annotations

import os
import threading
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
        "Portal CSV upload requires playwright. Install with:\n"
        "  pip install playwright && playwright install chromium"
    ) from exc


_STEALTH_INIT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});"
    "window.chrome = {runtime: {}};"
)


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.7632.6 Safari/537.36"
)
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-default-browser-check",
    "--no-first-run",
]

# Where each portal's sign-in starts, and where its session file lives.
PORTALS = {
    "tcgplayer": {
        "label": "TCGplayer",
        "login_url": "https://store.tcgplayer.com/login",
        "default_state": "output/cache/tcgplayer_state.json",
        "state_env": "TCGPLAYER_STATE_PATH",
    },
    "whatnot": {
        "label": "Whatnot",
        "login_url": "https://www.whatnot.com/login",
        "default_state": "output/cache/whatnot_state.json",
        "state_env": "WHATNOT_STATE_PATH",
    },
}


def portal_state_path(site: str, root: Optional[Path] = None) -> Path:
    cfg = PORTALS[site]
    path = Path(os.getenv(cfg["state_env"], cfg["default_state"]))
    return path if path.is_absolute() or root is None else root / path


def _launch_kwargs(headless: bool) -> dict:
    kwargs: dict = {
        "headless": headless,
        "args": _LAUNCH_ARGS,
        "ignore_default_args": ["--enable-automation"],
    }
    # For machines whose Chromium is not the build Playwright expects.
    exe = os.getenv("PLAYWRIGHT_CHROMIUM_PATH")
    if exe:
        kwargs["executable_path"] = exe
    return kwargs


class LoginCapture:
    """Sign in to a seller portal in a real browser window and keep the session.

    `start()` opens a visible Chromium window at the portal's login page on a
    worker thread and returns at once. The person signs in there (password,
    2FA, captcha: all theirs to do, nothing is typed for them). `finish()`
    saves the browser's session to the portal's state file and closes the
    window; `cancel()` closes it and saves nothing. The headless uploader then
    reuses that session.

    One capture per site at a time. `status()` is safe to poll from any
    thread; the browser itself is only ever touched by the worker thread,
    which is what Playwright's sync API requires.

    state: idle -> opening -> waiting -> saved | cancelled | error
    """

    TIMEOUT_SECONDS = 15 * 60

    def __init__(self, site: str, state_path: Path, headless: bool = False,
                 login_url: Optional[str] = None) -> None:
        self.site = site
        self.state_path = Path(state_path)
        self.headless = headless
        self.login_url = login_url or PORTALS[site]["login_url"]
        self._lock = threading.Lock()
        self._decision = threading.Event()
        self._save = False
        self._thread: Optional[threading.Thread] = None
        self._state = "idle"
        self._detail = ""

    def status(self) -> dict:
        with self._lock:
            return {"state": self._state, "detail": self._detail}

    def _set(self, state: str, detail: str = "") -> None:
        with self._lock:
            self._state, self._detail = state, detail

    def active(self) -> bool:
        return self.status()["state"] in ("opening", "waiting")

    def start(self) -> None:
        if self.active():
            raise PortalUploadError(f"a {self.site} sign-in window is already open")
        self._decision.clear()
        self._save = False
        self._set("opening", "opening a browser window")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"{self.site}-login")
        self._thread.start()

    def finish(self, timeout: float = 30) -> dict:
        """Save the session. Blocks until the worker has written the file."""
        if self.status()["state"] != "waiting":
            raise PortalUploadError("no sign-in window is waiting to be saved")
        self._save = True
        self._decision.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                # The browser is not answering. Stop treating this capture as
                # live, or the site could never be signed into again without
                # restarting the app.
                self._set("error", "the browser did not respond while saving; "
                                   "close its window and sign in again")
        return self.status()

    def cancel(self, timeout: float = 15) -> dict:
        if self.active():
            self._save = False
            self._decision.set()
            if self._thread is not None:
                self._thread.join(timeout)
        return self.status()

    def _run(self) -> None:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**_launch_kwargs(self.headless))
                try:
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 900},
                        user_agent=_USER_AGENT, locale="en-US",
                        timezone_id="America/Los_Angeles",
                    )
                    context.add_init_script(_STEALTH_INIT)
                    page = context.new_page()
                    try:
                        page.goto(self.login_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:
                        # The window is open; the person can navigate by hand.
                        self._set("waiting", f"could not load the login page ({exc}); "
                                             "navigate to it in the window")
                    else:
                        self._set("waiting", "sign in in the browser window, then come back here")

                    deadline = time.time() + self.TIMEOUT_SECONDS
                    while not self._decision.is_set():
                        if time.time() > deadline:
                            self._set("cancelled", "timed out waiting for sign-in; nothing saved")
                            return
                        try:
                            # Also pumps browser events, so a closed window is noticed.
                            page.wait_for_timeout(400)
                        except Exception:
                            # That tab is gone. Sign-in may have moved to a
                            # popup or a second tab; carry on with it.
                            still_open = [pg for pg in getattr(context, "pages", [])
                                          if not pg.is_closed()]
                            if not still_open:
                                self._set("cancelled", "the browser window was closed; nothing saved")
                                return
                            page = still_open[0]

                    if not self._save:
                        self._set("cancelled", "cancelled; nothing saved")
                        return
                    if self.status()["state"] != "waiting":
                        return   # finish() gave up on us; do not save late
                    self.state_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.state_path.with_suffix(".tmp")
                    # Owner-only before any cookie is written to it.
                    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
                    try:
                        context.storage_state(path=str(tmp))
                        os.chmod(tmp, 0o600)
                        tmp.replace(self.state_path)
                    except Exception:
                        tmp.unlink(missing_ok=True)   # no half-written cookies left behind
                        raise
                    self._set("saved", f"session saved to {self.state_path}")
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as exc:
            self._set("error", f"could not open a browser window: {exc}")


class PortalNotLoggedIn(RuntimeError):
    """Raised when no saved session exists for a portal."""


class PortalUploadError(RuntimeError):
    pass


class PortalUploader:
    """Base class. Subclass and set the class attributes below."""

    site_name: str = "portal"
    upload_url: str = ""
    # Selector candidates tried in order until one matches.
    file_input_selectors: tuple[str, ...] = ('input[type="file"]',)
    submit_selectors: tuple[str, ...] = (
        'button:has-text("Upload")',
        'button:has-text("Import")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    )
    # A substring that, if present in the URL after navigation, means the saved
    # session is dead and we got bounced to a login page.
    login_redirect_token: str = "signin"
    # A substring expected somewhere in the page text on a successful upload.
    success_text: tuple[str, ...] = ("success", "uploaded", "imported", "complete")

    def __init__(
        self,
        state_path: Optional[str] = None,
        headless: Optional[bool] = None,
    ) -> None:
        self.state_path = Path(state_path or self._default_state_path())
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        env_headless = os.getenv(f"{self.site_name.upper()}_HEADLESS", "1")
        self.headless = headless if headless is not None else env_headless == "1"
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._ensure_logged_in()

    @classmethod
    def _default_state_path(cls) -> str:
        return os.getenv(
            f"{cls.site_name.upper()}_STATE_PATH",
            f"output/cache/{cls.site_name}_state.json",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_logged_in(self) -> None:
        if self.state_path.exists():
            return
        raise PortalNotLoggedIn(
            f"No {self.site_name} session at {self.state_path}. Run from your terminal:\n"
            f"    python -m webapp.setup_portal --site {self.site_name}\n"
            "A Chromium window will open; sign in and the session will be saved."
        )

    def _start_browser(self) -> None:
        if self._pw is not None:
            return
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**_launch_kwargs(self.headless))

    def _new_context(self) -> BrowserContext:
        self._start_browser()
        assert self._browser is not None
        ctx = self._browser.new_context(
            storage_state=str(self.state_path),
            user_agent=_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Los_Angeles",
            accept_downloads=True,
        )
        ctx.add_init_script(_STEALTH_INIT)
        return ctx

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

    def __enter__(self) -> "PortalUploader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_csv(self, csv_path: str) -> dict:
        """Attach ``csv_path`` to the portal's bulk-import form and submit.

        Returns ``{"ok": bool, "detail": str}``. Subclasses can override
        ``_after_attach`` to handle portal-specific confirmation steps.
        """
        csv_file = Path(csv_path)
        if not csv_file.exists():
            raise PortalUploadError(f"CSV not found: {csv_path}")

        context = self._new_context()
        page = context.new_page()
        try:
            return self._do_upload(page, csv_file)
        finally:
            try:
                context.close()
            except Exception:
                pass

    def verify_session(self) -> dict:
        """Open the import page with the saved session and report what is
        there, without uploading anything.

        Returns {"ok", "signed_in", "detail"}. ok means a real upload has
        what it needs: the session is still signed in and the page has a
        file input to attach the CSV to.
        """
        context = self._new_context()
        page = context.new_page()
        try:
            page.goto(self.upload_url, wait_until="domcontentloaded", timeout=60000)
            if self.login_redirect_token and self.login_redirect_token in page.url:
                return {"ok": False, "signed_in": False,
                        "detail": f"the saved session has expired ({self.site} sent us to "
                                  f"its login page). Sign in again."}
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeoutError:
                pass
            if self._find_first(page, self.file_input_selectors, want_visible=False) is None:
                self._dump_debug(page, "verify-no-file-input")
                return {"ok": False, "signed_in": True,
                        "detail": f"signed in, but no file upload field was found at "
                                  f"{self.upload_url}. The page layout may have changed: set "
                                  f"{self.site.upper()}_UPLOAD_URL, or upload the CSV by hand. "
                                  f"A screenshot is in output/cache/{self.site}_debug/."}
            return {"ok": True, "signed_in": True,
                    "detail": "signed in and the import page has an upload field"}
        finally:
            try:
                context.close()
            except Exception:
                pass

    def _do_upload(self, page: Page, csv_file: Path) -> dict:
        page.goto(self.upload_url, wait_until="domcontentloaded", timeout=60000)
        if self.login_redirect_token and self.login_redirect_token in page.url:
            raise PortalUploadError(
                f"{self.site_name} bounced to {page.url} — saved session expired. "
                f"Delete {self.state_path} and rerun python -m webapp.setup_portal "
                f"--site {self.site_name}."
            )

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeoutError:
            pass

        file_input = self._find_first(page, self.file_input_selectors, want_visible=False)
        if file_input is None:
            self._dump_debug(page, "no-file-input")
            raise PortalUploadError(
                f"could not find a file input on {self.site_name}'s upload page. "
                f"Debug screenshot+HTML written to output/cache/{self.site_name}_debug/. "
                "The portal's DOM likely changed — update the selectors."
            )

        file_input.set_input_files(str(csv_file))
        self._after_attach(page)

        submit = self._find_first(page, self.submit_selectors, want_visible=True)
        if submit is not None:
            try:
                submit.click()
            except Exception:
                pass

        # Give the upload a chance to process.
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PWTimeoutError:
            pass
        time.sleep(2)

        body = ""
        try:
            body = page.inner_text("body").lower()
        except Exception:
            pass
        ok = any(tok in body for tok in self.success_text)
        if not ok:
            self._dump_debug(page, "post-submit")
        return {
            "ok": ok,
            "detail": (
                "submitted; success text detected"
                if ok
                else f"submitted but no success confirmation found — see output/cache/{self.site_name}_debug/"
            ),
        }

    def _after_attach(self, page: Page) -> None:
        """Hook for portal-specific steps between attaching the file and submit."""
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_first(self, page: Page, selectors: tuple[str, ...], want_visible: bool):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if want_visible:
                    loc.wait_for(state="visible", timeout=6000)
                else:
                    loc.wait_for(state="attached", timeout=6000)
                return loc
            except PWTimeoutError:
                continue
            except Exception:
                continue
        return None

    def _dump_debug(self, page: Page, tag: str) -> None:
        debug_dir = Path(f"output/cache/{self.site_name}_debug")
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
