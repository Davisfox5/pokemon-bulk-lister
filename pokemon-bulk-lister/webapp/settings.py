"""Advanced settings: bring-your-own eBay API keys, per user.

Hidden behind the "Advanced" menu on /portfolio. eBay listing is OFF for
everyone by default; a user who stores their own eBay developer keyset
(developer.ebay.com → Application Keysets) and completes the one-time consent
flow unlocks list-on-eBay for THEIR eBay account only. The site operator's
pricing credentials (EBAY_CLIENT_ID in .env) are never shared with users.

Storage: keysets live in the `user_ebay_keys` table (plain text — same trust
level as the rest of the DB; move to a secret store before real multi-tenant
hosting). Refresh tokens live per user at
output/cache/ebay_user_tokens/user_<id>.json, the same location Instance A's
original wiring used.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, request
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lib.ebay_lister import EbayLister, EbayListingError
from lib.ebay_oauth import EbayUserAuth, EbayUserAuthError
from webapp.auth import current_user_id, login_required, verified_required
from webapp.models import Base, InventoryItem, Listing, get_session, utcnow

ROOT = Path(__file__).resolve().parent.parent
from webapp import runtime  # noqa: E402

TOKENS_DIR = runtime.DATA_ROOT / "output" / "cache" / "ebay_user_tokens"

settings_bp = Blueprint("settings", __name__)


class UserEbayKeys(Base):
    __tablename__ = "user_ebay_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret: Mapped[str] = mapped_column(Text, nullable=False)
    ru_name: Mapped[str] = mapped_column(Text, nullable=False)
    env: Mapped[str] = mapped_column(String(12), default="production")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


def ensure_settings_tables() -> None:
    """Create this module's tables; safe to call after init_models()."""
    s = get_session()
    try:
        Base.metadata.create_all(s.get_bind())
    finally:
        s.close()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _token_path(uid: int) -> Path:
    return TOKENS_DIR / f"user_{uid}.json"


def _get_keys(s, uid: int) -> Optional[UserEbayKeys]:
    return s.query(UserEbayKeys).filter_by(user_id=uid).one_or_none()


def _auth_for(uid: int, keys: UserEbayKeys) -> EbayUserAuth:
    # The refresh token is a file here and a sealed database row on a
    # serverless host (see runtime.make_ebay_auth).
    return runtime.make_ebay_auth(
        f"user-{uid}", _token_path(uid),
        client_id=keys.client_id,
        client_secret=keys.client_secret,
        redirect_uri=keys.ru_name,
        env=keys.env,
    )


def _hint(client_id: str) -> str:
    if len(client_id) <= 8:
        return "…"
    return f"{client_id[:4]}…{client_id[-4:]}"


def _status(uid: int) -> dict:
    s = get_session()
    try:
        keys = _get_keys(s, uid)
        if keys is None:
            return {"configured": False, "authorized": False}
        authorized = _auth_for(uid, keys).is_authorized()
        return {
            "configured": True,
            "authorized": authorized,
            "client_id_hint": _hint(keys.client_id),
            "env": keys.env,
        }
    finally:
        s.close()


# ----------------------------------------------------------------------
# Keyset CRUD
# ----------------------------------------------------------------------

@settings_bp.route("/api/settings/ebay")
@login_required
def ebay_status():
    return jsonify(_status(current_user_id()))


@settings_bp.route("/api/settings/ebay", methods=["POST"])
@verified_required
def ebay_save_keys():
    body = request.get_json(silent=True) or {}
    client_id = (body.get("client_id") or "").strip()
    client_secret = (body.get("client_secret") or "").strip()
    ru_name = (body.get("ru_name") or "").strip()
    env = (body.get("env") or "production").strip().lower()
    if not client_id or not client_secret or not ru_name:
        return jsonify({"error": "client_id, client_secret and ru_name are all required"}), 400
    if env not in ("production", "sandbox"):
        return jsonify({"error": "env must be production or sandbox"}), 400

    uid = current_user_id()
    s = get_session()
    try:
        keys = _get_keys(s, uid)
        changed_app = keys is not None and keys.client_id != client_id
        if keys is None:
            keys = UserEbayKeys(user_id=uid)
            s.add(keys)
        keys.client_id = client_id
        keys.client_secret = client_secret
        keys.ru_name = ru_name
        keys.env = env
        s.commit()
    finally:
        s.close()
    # A consent granted to a different app id is useless — drop it.
    if changed_app:
        runtime.forget_ebay_token(f"user-{uid}", _token_path(uid))
    return jsonify(_status(uid))


@settings_bp.route("/api/settings/ebay", methods=["DELETE"])
@login_required
def ebay_remove_keys():
    uid = current_user_id()
    s = get_session()
    try:
        keys = _get_keys(s, uid)
        if keys is not None:
            s.delete(keys)
            s.commit()
    finally:
        s.close()
    runtime.forget_ebay_token(f"user-{uid}", _token_path(uid))
    return jsonify(_status(uid))


# ----------------------------------------------------------------------
# Consent flow
# ----------------------------------------------------------------------

@settings_bp.route("/api/settings/ebay/consent-url")
@login_required
def ebay_consent_url():
    uid = current_user_id()
    s = get_session()
    try:
        keys = _get_keys(s, uid)
    finally:
        s.close()
    if keys is None:
        return jsonify({"error": "save your eBay API keys first"}), 400
    try:
        return jsonify({"url": _auth_for(uid, keys).build_consent_url()})
    except EbayUserAuthError as exc:
        return jsonify({"error": str(exc)}), 400


@settings_bp.route("/api/settings/ebay/authorize", methods=["POST"])
@verified_required
def ebay_authorize():
    body = request.get_json(silent=True) or {}
    pasted = (body.get("redirect") or "").strip()
    if not pasted:
        return jsonify({"error": "paste the URL eBay redirected you to (or the code)"}), 400
    uid = current_user_id()
    s = get_session()
    try:
        keys = _get_keys(s, uid)
    finally:
        s.close()
    if keys is None:
        return jsonify({"error": "save your eBay API keys first"}), 400
    auth = _auth_for(uid, keys)
    try:
        code = auth.extract_code(pasted)
        auth.exchange_code(code)
    except EbayUserAuthError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_status(uid))


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------

@settings_bp.route("/api/settings/ebay/list-item", methods=["POST"])
@verified_required
def ebay_list_item():
    body = request.get_json(silent=True) or {}
    uid = current_user_id()

    s = get_session()
    try:
        keys = _get_keys(s, uid)
        if keys is None:
            return jsonify({"error": "eBay keys not configured (Advanced settings)"}), 400
        item = (
            s.query(InventoryItem)
            .filter_by(id=int(body.get("item_id") or 0), user_id=uid)
            .one_or_none()
        )
        if item is None or item.quantity < 1:
            # quantity 0 is a card that has sold; it must not be listed again
            return jsonify({"error": "no such inventory item"}), 404
        item_id = item.id
        cc = item.catalog_card
        price = body.get("price")
        price = float(price) if price is not None else cc.final_price
        card = {
            "id": f"inv{item.id}",
            "name": cc.name,
            "set_name": cc.set_name,
            "card_number": cc.card_number,
            "rarity": cc.rarity,
            "is_holo": cc.is_holo,
            "condition_guess": item.condition or "NM",
            "crop_path": item.source_crop_path or "",
            "image_url": item.image_url or cc.image_url,
            "final_price": price,
        }
    finally:
        s.close()

    if not price or price <= 0:
        return jsonify({"error": "no price — refresh prices or pass one explicitly"}), 400
    if not card["image_url"]:
        return jsonify({"error": "item has no image URL — upload its photo first"}), 400

    auth = _auth_for(uid, keys)
    if not auth.is_authorized():
        return jsonify({"error": "eBay account not connected — run the consent step"}), 400

    lister = EbayLister(auth=auth, env=keys.env)
    s = get_session()
    try:
        rec = Listing(
            user_id=uid, inventory_item_id=item_id,
            marketplace="ebay", price=round(price, 2), status="pending",
        )
        s.add(rec)
        s.commit()
        try:
            result = lister.publish_card(card, dry_run=bool(body.get("dry_run")))
            rec.sku = result.get("sku")
            rec.offer_id = result.get("offer_id")
            rec.listing_id = result.get("listing_id")
            rec.status = "dry_run" if result.get("dry_run") else "live"
            s.commit()
            return jsonify({"listing": result, "status": rec.status})
        except (EbayListingError, EbayUserAuthError) as exc:
            rec.status = "error"
            rec.error = str(exc)
            s.commit()
            return jsonify({"error": str(exc)}), 502
    finally:
        s.close()
