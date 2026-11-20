"""Our own AI-written listing descriptions, used when eBay's writer is not
(no eBay connection, the scope not granted, or the day's 20 eBay batches
already sent).

To read like eBay's, the model is shown recent descriptions eBay itself
wrote for other cards, next to the facts they were written from, and asked
to write the same way. Until eBay has written any, a short brief stands in.

Runtime model: Haiku, like identification. Writing a few sentences from a
handful of facts is light work, and it runs per batch of cards.
DESCRIPTION_MODEL overrides it.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

DEFAULT_MODEL = "claude-haiku-5-5"
FALLBACK_MODEL = "claude-haiku-4-5"
MAX_CARDS = 20             # per request

_BRIEF = (
    "You write the item description for eBay listings of single Pokémon trading cards. "
    "Each description is a few short sentences in plain, friendly selling language: what the card "
    "is (name, set, number), its rarity and finish, its condition, and the language it is printed in. "
    "Use only the facts given for that card. Never add anything that is not given: no grading, "
    "centering, edges, authenticity claims, shipping, returns, payment or seller policies, prices, "
    "or play value. If a fact is missing, leave it out. No headings, no bullet points, no emoji, "
    "no exclamation marks."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "descriptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "description": {"type": "string"}},
                "required": ["sku", "description"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["descriptions"],
    "additionalProperties": False,
}


def card_facts(card: dict) -> dict:
    """The facts a description may use, and nothing else."""
    from lib.ebay_lister import _condition_full, card_finish, card_language

    facts = {
        "name": card.get("name") or "",
        "set": (card.get("set_name") or "").replace("(Japanese)", "").strip(),
        "number": card.get("card_number") or "",
        "rarity": card.get("rarity") or "",
        "finish": card_finish(card),
        "condition": _condition_full(card["condition_guess"]) if card.get("condition_guess") else "",
        "language": card_language(card),
    }
    return {k: v for k, v in facts.items() if v}


def _system(examples: list[dict]) -> str:
    if not examples:
        return _BRIEF
    shown = "\n\n".join(
        f"Facts: {json.dumps(e['facts'], ensure_ascii=False)}\nDescription eBay wrote: {e['description']}"
        for e in examples)
    return (_BRIEF + "\n\nBelow are descriptions eBay's own writer produced for other cards. Match their "
            "length, structure, tone and wording as closely as you can, so yours read as if eBay wrote them. "
            "Take only the style from them, never facts about other cards.\n\n" + shown)


def _client():
    from lib import anthropic_auth

    return anthropic_auth.client()


def describe(cards: list[dict], examples: Optional[list[dict]] = None, model: Optional[str] = None) -> dict[str, str]:
    """{sku: description} for up to MAX_CARDS cards. Each card dict carries
    "sku" and "facts" (from card_facts). Cards the model skipped are left
    out of the result."""
    import anthropic

    if not cards:
        return {}
    if len(cards) > MAX_CARDS:
        raise ValueError(f"at most {MAX_CARDS} cards per request")
    client = _client()
    chosen = model or os.getenv("DESCRIPTION_MODEL", DEFAULT_MODEL)
    listing = "\n".join(f"- sku {c['sku']}: {json.dumps(c['facts'], ensure_ascii=False)}" for c in cards)
    prompt = f"Write one description for each of these {len(cards)} cards. Return each with its sku.\n{listing}"

    def ask(name: str) -> Any:
        output: dict[str, Any] = {"format": {"type": "json_schema", "schema": _SCHEMA}}
        budget = 220 * len(cards) + 200
        if name != FALLBACK_MODEL:
            output["effort"] = "low"      # thinking counts toward max_tokens; leave room
            budget *= 2
        return client.messages.create(model=name, max_tokens=budget, system=_system(examples or []),
                                      output_config=output,
                                      messages=[{"role": "user", "content": prompt}])

    try:
        response = ask(chosen)
    except (anthropic.BadRequestError, anthropic.NotFoundError, anthropic.PermissionDeniedError):
        if chosen == FALLBACK_MODEL:
            raise
        response = ask(FALLBACK_MODEL)
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined to write these descriptions")
    text = next(b.text for b in response.content if b.type == "text")
    wanted = {c["sku"] for c in cards}
    out: dict[str, str] = {}
    for row in json.loads(text).get("descriptions") or []:
        sku, body = str(row.get("sku") or ""), (row.get("description") or "").strip()
        if sku in wanted and body:
            out[sku] = body
    return out
