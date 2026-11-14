"""Claude-vision card identification.

Two granularities:
  - identify_page(): one API call for a whole binder-page photo — identifies
    every card in the grid at once. ~9x cheaper than per-crop calls because the
    prompt overhead is amortized across the page.
  - identify_crop(): one call for a single card crop. Better small-text
    legibility (set symbol, card number), used as a retry for cells the
    page-level pass couldn't read confidently.

Uses structured outputs (output_config.format) so the response is
schema-validated JSON — no prose to parse. Model defaults to Haiku
(identification is constrained extraction; Haiku handles the overwhelming
majority of cards) and can be overridden with VISION_MODEL.

Credentials come from the standard Anthropic SDK resolution (ANTHROPIC_API_KEY
env var, auth token, or an `ant auth login` profile).
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Optional

import cv2

DEFAULT_MODEL = "claude-haiku-4-5"

# Haiku's max useful image size is 1568px on the long edge — larger inputs are
# downscaled server-side anyway, so resizing client-side just saves tokens.
PAGE_MAX_EDGE = 1568
CROP_MAX_EDGE = 1100
JPEG_QUALITY = 85

_CARD_PROPS = {
    "row": {"type": "integer", "description": "0-indexed row in the grid, top to bottom"},
    "col": {"type": "integer", "description": "0-indexed column in the grid, left to right"},
    "name": {"type": "string", "description": "Card name as printed, e.g. 'Pikachu ex'. Empty string if unreadable."},
    "set_name": {"type": "string", "description": "Set name, e.g. 'Surging Sparks'. Empty string if unknown."},
    "set_code": {
        "type": "string",
        "description": "pokemontcg.io set id, e.g. 'base1', 'swsh12', 'sv8'. Empty string if unsure — do NOT guess.",
    },
    "card_number": {"type": "string", "description": "Printed collector number numerator only, e.g. '057' from '057/191'. Empty string if unreadable."},
    "rarity": {"type": "string", "description": "e.g. 'Common', 'Rare Holo', 'Double Rare'. Empty string if unknown."},
    "is_holo": {"type": "boolean", "description": "True if the card has a holographic treatment"},
    "confidence": {"type": "number", "description": "0.0-1.0 confidence in this identification overall"},
}

_CARD_SCHEMA = {
    "type": "object",
    "properties": _CARD_PROPS,
    "required": list(_CARD_PROPS.keys()),
    "additionalProperties": False,
}

_PAGE_SCHEMA = {
    "type": "object",
    "properties": {"cards": {"type": "array", "items": _CARD_SCHEMA}},
    "required": ["cards"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You identify Pokémon TCG cards from photos for a bulk-listing tool. "
    "Read the card name, set symbol/name, collector number, and rarity as printed. "
    "For set_code use the pokemontcg.io set id (examples: base1, neo4, xy7, sm115, "
    "swsh12, sv3pt5, sv8). Accuracy matters more than completeness: leave a field "
    "as an empty string rather than guessing, and reflect any uncertainty in the "
    "confidence score. Glare, sleeves, and partial occlusion are common — do your best."
)


def _client():
    from lib import anthropic_auth

    return anthropic_auth.client()


def _encode_image(image_path: str, max_edge: int) -> str:
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"could not read image: {image_path}")
    h, w = img.shape[:2]
    scale = max_edge / max(h, w)
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise ValueError(f"could not encode image: {image_path}")
    return base64.standard_b64encode(buf.tobytes()).decode()


def _vision_request(image_b64: str, prompt: str, schema: dict, max_tokens: int, model: Optional[str]) -> Any:
    client = _client()
    response = client.messages.create(
        model=model or os.getenv("VISION_MODEL", DEFAULT_MODEL),
        max_tokens=max_tokens,
        system=_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to process the image")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def identify_page(image_path: str, rows: int = 3, cols: int = 3, model: Optional[str] = None) -> list[dict]:
    """Identify every card on a binder-page photo. Returns a list of card dicts
    with 0-indexed row/col matching the splitter's grid positions."""
    image_b64 = _encode_image(image_path, PAGE_MAX_EDGE)
    prompt = (
        f"This photo shows a binder page holding Pokémon cards in a {rows}x{cols} grid "
        f"(up to {rows * cols} pockets; some may be empty). Identify each card that is "
        "present. Use 0-indexed row (top to bottom) and col (left to right). "
        "Skip empty pockets entirely."
    )
    result = _vision_request(image_b64, prompt, _PAGE_SCHEMA, max_tokens=4096, model=model)
    return result.get("cards", [])


def identify_crop(image_path: str, model: Optional[str] = None) -> dict:
    """Identify a single card crop. Returns one card dict (row/col are -1)."""
    image_b64 = _encode_image(image_path, CROP_MAX_EDGE)
    prompt = (
        "Identify this single Pokémon card. Set row and col to -1 (this is a "
        "standalone crop, not a grid)."
    )
    return _vision_request(image_b64, prompt, _CARD_SCHEMA, max_tokens=1024, model=model)
