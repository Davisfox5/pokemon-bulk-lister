"""Connections: set up and check every outside service from the Scan page.

One place that answers "is X connected, and if not, what is the next step":

  eBay        operator keyset in .env, then the one-time consent flow run
              here in the browser (no terminal), then an account check that
              looks for the business policies a live listing needs
  Cloudinary  image hosting eBay listings require
  TCGplayer / Whatnot
              sign in once in a real browser window opened from here; the
              session is saved for the headless CSV upload. "Check" opens the
              import page with that session and confirms the upload field is
              there, without uploading anything
  Stripe      card payments on the marketplace; "Check" makes one read-only
              API call

Secrets stay in .env. Nothing here writes a key to disk or returns one: status
responses say whether a value is set, never what it is.

These routes sit with the scan bench: local, single operator, no login. The
same-origin guard in app.py keeps other websites from calling them.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, jsonify, request

ROOT = Path(__file__).resolve().parent.parent

connections_bp = Blueprint("connections", __name__)

PORTAL_CLASSES = {
    "tcgplayer": ("lib.tcgplayer_lister", "TCGPlayerLister"),
    "whatnot": ("lib.whatnot_lister", "WhatnotLister"),
}
# One live sign-in window per portal. The lock makes "is one open? then open
# one" a single step, so a double click cannot start two browsers.
_captures: dict[str, Any] = {}
_captures_lock = threading.Lock()


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def allowed_hosts() -> set[str]:
    """Host names this app answers to. ALLOWED_HOSTS (comma separated) adds
    to the local ones, for an install served under a real domain."""
    extra = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
    return LOCAL_HOSTS | extra


def same_origin_only():
    """before_request hook for the whole app. Two checks.

    1. Host allowlist, every request. The bench has no login and trusts
       whoever can reach it. A hostile page can point its own domain at
       127.0.0.1 (DNS rebinding) and then talk to the app as "same origin";
       the Host header gives that away, because it names the attacker's
       domain. Anything not on the allowlist is refused.
    2. Origin, state-changing requests. A page on another site can still make
       the operator's browser POST here. Browsers put an Origin header on
       such requests, so a POST whose Origin is not this app is turned away.
       Requests with no Origin (curl, the CLI scripts, Stripe's webhook) pass.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(f"//{request.host}").hostname or "").lower()
    if host not in allowed_hosts():
        return jsonify({"error": f"host {host!r} is not allowed; add it to ALLOWED_HOSTS"}), 403
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if not origin:
        return None
    if origin == "null" or urlsplit(origin).netloc != request.host:
        return jsonify({"error": "cross-site request refused"}), 403
    return None


# Bench routes anyone signed in or not may reach under a public host: card
# photos (the library and marketplace show them) and the health check.
_PUBLIC_BENCH_PREFIXES = ("/crops/", "/output/crops/", "/static/")
_PUBLIC_BENCH_PATHS = ("/healthz", "/api/session")


def operator_only_when_public():
    """before_request hook, after same_origin_only.

    On localhost the Scan page and Connections have no login, which is fine
    for a tool on your own machine. Served under a real domain they are on
    the internet, and they can publish listings, spend API credit and read
    connection status. So under any host that is not local, every bench
    route (the routes on the app itself, and Connections) needs a signed-in
    operator. The library, decks, marketplace and login pages keep their own
    rules.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(f"//{request.host}").hostname or "").lower()
    if host in LOCAL_HOSTS or request.blueprint not in (None, "connections"):
        return None
    path = request.path
    if path in _PUBLIC_BENCH_PATHS or path.startswith(_PUBLIC_BENCH_PREFIXES):
        return None

    from flask import redirect

    from webapp.auth import current_user_id, is_operator
    from webapp.models import get_session

    wants_json = path.startswith("/api/") or request.method != "GET"
    uid = current_user_id()
    if not uid:
        if wants_json:
            return jsonify({"error": "login required"}), 401
        return redirect(f"/login?next={path}")
    s = get_session()
    try:
        allowed = is_operator(s, uid)
    finally:
        s.close()
    if allowed:
        return None
    if wants_json:
        return jsonify({"error": "operator only"}), 403
    return redirect("/portfolio")


def _has(*names: str) -> bool:
    return all(bool(os.getenv(n)) for n in names)


def cloudinary_ready() -> bool:
    return _has("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")


# ----------------------------------------------------------------------
# eBay
# ----------------------------------------------------------------------

def _ebay_auth():
    from lib.ebay_oauth import EbayUserAuth

    token_path = Path(os.getenv("EBAY_USER_TOKEN_PATH", "output/cache/ebay_user_token.json"))
    if not token_path.is_absolute():
        token_path = ROOT / token_path
    return EbayUserAuth(token_path=str(token_path))


def _ebay_status() -> dict:
    missing = [n for n in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REDIRECT_URI")
               if not os.getenv(n)]
    env = (os.getenv("EBAY_ENV") or "production").lower()
    out: dict[str, Any] = {
        "env": env, "missing": missing, "configured": not missing, "authorized": False,
        "images": cloudinary_ready(),
    }
    if missing:
        out["next"] = ("Add " + ", ".join(missing) + " to .env and restart the app. Keys come "
                       "from developer.ebay.com (Application Keysets); EBAY_REDIRECT_URI is the "
                       "RuName under User Tokens, not a URL.")
    else:
        try:
            from lib.ebay_oauth import SALES_SCOPE
            auth = _ebay_auth()
            out["authorized"] = auth.is_authorized()
            out["can_read_sales"] = auth.has_scope(SALES_SCOPE)
        except Exception as exc:
            out["error"] = str(exc)
        if not out["authorized"]:
            out["next"] = "Connect your eBay account: open the consent link, approve, paste the URL you land on."
        elif not out["images"]:
            out["next"] = ("Connected. Listings need hosted photos: add the three CLOUDINARY_ "
                           "values to .env so photos upload automatically when you publish.")
        else:
            out["next"] = "Connected. Run the account check once before your first publish."
        if out["authorized"] and not out.get("can_read_sales"):
            out["next"] += (" This connection was made before sales could be read back: "
                            "disconnect and connect again to let the app see what sold.")
    out.setdefault("can_read_sales", False)
    out["ready"] = bool(out["configured"] and out["authorized"])
    return out


@connections_bp.route("/api/connections/ebay/consent-url", methods=["POST"])
def ebay_consent_url():
    from lib.ebay_oauth import EbayUserAuthError
    try:
        return jsonify({"url": _ebay_auth().build_consent_url()})
    except EbayUserAuthError as exc:
        return jsonify({"error": str(exc)}), 400


@connections_bp.route("/api/connections/ebay/authorize", methods=["POST"])
def ebay_authorize():
    """Body: {"redirect": "<the URL eBay sent the browser to, or the code>"}."""
    from lib.ebay_oauth import EbayUserAuth, EbayUserAuthError

    pasted = str((request.get_json(silent=True) or {}).get("redirect") or "").strip()
    if not pasted:
        return jsonify({"error": "paste the URL eBay redirected you to"}), 400
    try:
        auth = _ebay_auth()
        auth.exchange_code(EbayUserAuth.extract_code(pasted))
    except EbayUserAuthError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"could not reach eBay: {exc}"}), 502
    return jsonify(_ebay_status())


@connections_bp.route("/api/connections/ebay/disconnect", methods=["POST"])
def ebay_disconnect():
    """Forget the saved eBay authorization. Live listings are not touched."""
    try:
        _ebay_auth().token_path.unlink(missing_ok=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(_ebay_status())


@connections_bp.route("/api/connections/ebay/check", methods=["POST"])
def ebay_check():
    """Read-only look at the eBay account: can we get a token, and does the
    account have the three business policies a published offer must name?
    Creates and changes nothing on eBay."""
    from lib.ebay_lister import EbayLister, EbayListingError
    from lib.ebay_oauth import EbayUserNotAuthorized

    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    try:
        auth = _ebay_auth()
        lister = EbayLister(auth=auth)
        auth.access_token()
        step("Sign-in", True, f"token refreshed against eBay {_ebay_status()['env']}")
    except EbayUserNotAuthorized:
        step("Sign-in", False, "eBay is not connected yet. Use the consent link above.")
        return jsonify({"ok": False, "steps": steps})
    except Exception as exc:
        step("Sign-in", False, str(exc))
        return jsonify({"ok": False, "steps": steps})

    try:
        policies = lister._resolve_policies()  # noqa: SLF001  read-only GETs
        step("Business policies", True,
             "payment, return and shipping policies found: "
             + ", ".join(f"{k} {v}" for k, v in sorted(policies.items())))
    except EbayListingError as exc:
        step("Business policies", False, str(exc))
    except Exception as exc:
        step("Business policies", False, f"could not reach eBay: {exc}")

    try:
        resp = lister._request("GET", "/sell/inventory/v1/location")  # noqa: SLF001
        if resp.status_code == 200:
            locations = resp.json().get("locations") or []
            step("Inventory location", True,
                 f"using {locations[0].get('merchantLocationKey')}" if locations
                 else "none yet; one is created on the first publish from "
                      "EBAY_LOCATION_POSTAL_CODE and EBAY_LOCATION_COUNTRY")
        else:
            step("Inventory location", False, f"eBay answered {resp.status_code}")
    except Exception as exc:
        step("Inventory location", False, f"could not reach eBay: {exc}")

    step("Photo hosting", cloudinary_ready(),
         "Cloudinary is configured; photos upload when you publish" if cloudinary_ready()
         else "Cloudinary is not configured, so cards need an image URL before they can list")
    return jsonify({"ok": all(s["ok"] for s in steps), "steps": steps})


# ----------------------------------------------------------------------
# Stripe
# ----------------------------------------------------------------------

def _stripe_status() -> dict:
    key = os.getenv("STRIPE_SECRET_KEY") or ""
    try:
        import stripe  # noqa: F401
        package = True
    except ImportError:
        package = False
    mode = "test" if key.startswith(("sk_test_", "rk_test_")) else "live" if key else None
    out = {
        "configured": bool(key), "package": package, "mode": mode,
        "webhook": bool(os.getenv("STRIPE_WEBHOOK_SECRET")),
        "ready": bool(key) and package,
    }
    if not package:
        out["next"] = "Install the Stripe library: pip install -r requirements.txt"
    elif not key:
        out["next"] = ("Add STRIPE_SECRET_KEY to .env and restart the app. Use a test key "
                       "(sk_test_...) first. Until then, buyers and sellers settle directly.")
    elif mode == "test":
        out["next"] = ("Test mode: buyers can pay with Stripe's test cards and no money moves. "
                       "Swap in the live key when you are ready.")
    else:
        out["next"] = "Live mode: buyers pay by card into your Stripe account."
    return out


@connections_bp.route("/api/connections/stripe/check", methods=["POST"])
def stripe_check():
    """One read-only call (the account balance) to prove the key works."""
    status = _stripe_status()
    if not status["ready"]:
        return jsonify({"ok": False, "detail": status["next"]})
    import stripe

    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    try:
        balance = stripe.Balance.retrieve()
    except Exception as exc:
        return jsonify({"ok": False, "detail": f"Stripe rejected the key or could not be reached: {exc}"})
    live = bool(balance["livemode"])
    return jsonify({
        "ok": True,
        "detail": f"Stripe accepted the key ({'live' if live else 'test'} mode). "
                  + ("Signed webhooks are on." if status["webhook"]
                     else "No webhook secret set: orders settle when the buyer returns from "
                          "checkout, which is all a local install needs."),
    })


# ----------------------------------------------------------------------
# TCGplayer / Whatnot portal sessions
# ----------------------------------------------------------------------

def _portal_lib():
    from lib import portal_uploader
    return portal_uploader


def _portal_status(site: str) -> dict:
    try:
        lib = _portal_lib()
    except ImportError as exc:
        return {"session": False, "ready": False, "playwright": False, "login": {"state": "idle"},
                "next": f"Install the browser driver: pip install playwright && "
                        f"playwright install chromium ({exc})"}
    path = lib.portal_state_path(site, ROOT)
    capture = _captures.get(site)
    login = capture.status() if capture else {"state": "idle", "detail": ""}
    has = path.exists()
    out = {
        "session": has, "ready": has, "playwright": True, "login": login,
        "saved_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)) if has else None,
    }
    label = lib.PORTALS[site]["label"]
    if login["state"] in ("opening", "waiting"):
        out["next"] = f"Sign in to {label} in the browser window that opened, then click “I'm signed in”."
    elif has:
        out["next"] = f"Session saved {out['saved_at']}. Run the check to confirm it still works."
    else:
        out["next"] = f"Click “Sign in to {label}”. A browser window opens on this computer; sign in there."
    return out


def _site_or_404(site: str) -> Optional[tuple]:
    return None if site not in PORTAL_CLASSES else PORTAL_CLASSES[site]


@connections_bp.route("/api/connections/portal/<site>/login", methods=["POST"])
def portal_login(site: str):
    """Open a visible browser window at the portal's sign-in page."""
    if _site_or_404(site) is None:
        return jsonify({"error": "unknown site"}), 404
    try:
        lib = _portal_lib()
    except ImportError as exc:
        return jsonify({"error": str(exc)}), 400
    with _captures_lock:
        capture = _captures.get(site)
        if capture is not None and capture.active():
            return jsonify({"error": "a sign-in window is already open for this site"}), 409
        capture = lib.LoginCapture(
            site, lib.portal_state_path(site, ROOT),
            # Test hooks: a headless window and a stand-in login page.
            headless=os.getenv("PORTAL_LOGIN_HEADLESS") == "1",
            login_url=os.getenv(f"{site.upper()}_LOGIN_URL") or None,
        )
        _captures[site] = capture
        capture.start()
    return jsonify(_portal_status(site))


@connections_bp.route("/api/connections/portal/<site>/login/finish", methods=["POST"])
def portal_login_finish(site: str):
    capture = _captures.get(site)
    if capture is None:
        return jsonify({"error": "no sign-in window is open"}), 409
    lib = _portal_lib()
    try:
        result = capture.finish()
    except lib.PortalUploadError as exc:
        return jsonify({"error": str(exc), **_portal_status(site)}), 409
    if result["state"] != "saved":
        return jsonify({"error": result["detail"] or "the session was not saved",
                        **_portal_status(site)}), 500
    return jsonify(_portal_status(site))


@connections_bp.route("/api/connections/portal/<site>/login/cancel", methods=["POST"])
def portal_login_cancel(site: str):
    capture = _captures.get(site)
    if capture is not None:
        capture.cancel()
    return jsonify(_portal_status(site)) if _site_or_404(site) else (jsonify({"error": "unknown site"}), 404)


@connections_bp.route("/api/connections/portal/<site>/check", methods=["POST"])
def portal_check(site: str):
    """Headless: open the import page with the saved session. Uploads nothing."""
    cls_ref = _site_or_404(site)
    if cls_ref is None:
        return jsonify({"error": "unknown site"}), 404
    try:
        lib = _portal_lib()
        module = __import__(cls_ref[0], fromlist=[cls_ref[1]])
        lister_cls = getattr(module, cls_ref[1])
    except ImportError as exc:
        return jsonify({"ok": False, "detail": str(exc)})
    try:
        with lister_cls(state_path=str(lib.portal_state_path(site, ROOT))) as lister:
            return jsonify(lister.verify_session())
    except lib.PortalNotLoggedIn:
        return jsonify({"ok": False, "signed_in": False, "detail": "no saved session yet; sign in first"})
    except Exception as exc:
        print(f"[connections] {site} check: {exc}", file=sys.stderr)
        return jsonify({"ok": False, "detail": f"could not open {site}: {exc}"})


@connections_bp.route("/api/connections/portal/<site>/disconnect", methods=["POST"])
def portal_disconnect(site: str):
    if _site_or_404(site) is None:
        return jsonify({"error": "unknown site"}), 404
    try:
        _portal_lib().portal_state_path(site, ROOT).unlink(missing_ok=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(_portal_status(site))


# ----------------------------------------------------------------------
# Everything at once
# ----------------------------------------------------------------------

@connections_bp.route("/api/connections")
def connections_status():
    return jsonify({
        "ebay": _ebay_status(),
        "cloudinary": {
            "ready": cloudinary_ready(),
            "next": "Configured." if cloudinary_ready() else
                    "Add CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET "
                    "to .env and restart. eBay needs each card photo at a public URL.",
        },
        "identify": {
            "ready": _has("ANTHROPIC_API_KEY"),
            "next": "Configured." if _has("ANTHROPIC_API_KEY") else
                    "Add ANTHROPIC_API_KEY to .env and restart. Without it only the free "
                    "image match can identify cards.",
        },
        "tcgplayer": _portal_status("tcgplayer"),
        "whatnot": _portal_status("whatnot"),
        "stripe": _stripe_status(),
    })
