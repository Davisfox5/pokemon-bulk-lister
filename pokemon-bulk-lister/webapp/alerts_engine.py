"""Alert evaluation: runs after every scheduler refresh cycle.

Alert text follows the "marketplace listing" row in the df-writing skill
(.claude/skills/df-writing): state the number and what it means, no hype, no
advice the data does not support.

Rule kinds (alert_rules.kind):
  price_above   fire when latest price >= threshold ($)
  price_below   fire when latest price <= threshold ($)
  pct_change    fire when |Δ%| over window_days >= threshold (%)
  sell_signal   fire when delta% over window_days >= +threshold (%), the
                "you should consider selling" nudge every account gets.

catalog_card_id NULL means the rule applies to every card in the user's
inventory. Cooldown: a rule won't re-fire within its window (1 day for
above/below), tracked on rule.last_triggered_at.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from webapp.models import (
    Alert,
    AlertRule,
    CatalogCard,
    InventoryItem,
    price_at,
    utcnow,
)


def _cooldown(rule: AlertRule) -> timedelta:
    if rule.kind in ("price_above", "price_below"):
        return timedelta(days=1)
    return timedelta(days=max(1, rule.window_days))


def _card_label(cc: CatalogCard) -> str:
    bits = [cc.name]
    if cc.set_name:
        bits.append(f"({cc.set_name}")
        bits[-1] += f" #{cc.card_number})" if cc.card_number else ")"
    return " ".join(bits)


def _evaluate_rule(s: Session, rule: AlertRule, now) -> list[Alert]:
    if rule.catalog_card_id is not None:
        ccids = [rule.catalog_card_id]
    else:
        ccids = [
            row[0]
            for row in s.query(InventoryItem.catalog_card_id)
            .filter_by(user_id=rule.user_id)
            .distinct()
            .all()
        ]

    alerts: list[Alert] = []
    for ccid in ccids:
        cc = s.get(CatalogCard, ccid)
        if cc is None or cc.final_price is None:
            continue
        price = cc.final_price
        label = _card_label(cc)
        msg = None

        if rule.kind == "price_above" and price >= rule.threshold:
            msg = f"{label} is now ${price:.2f}, at or above your ${rule.threshold:.2f} target."
        elif rule.kind == "price_below" and price <= rule.threshold:
            msg = f"{label} dropped to ${price:.2f}, at or below your ${rule.threshold:.2f} floor."
        elif rule.kind in ("pct_change", "sell_signal"):
            base = price_at(s, ccid, now - timedelta(days=rule.window_days))
            if not base or base <= 0:
                continue
            pct = (price - base) / base * 100
            if rule.kind == "pct_change" and abs(pct) >= rule.threshold:
                direction = "up" if pct > 0 else "down"
                msg = (f"{label} is {direction} {abs(pct):.0f}% over the last "
                       f"{rule.window_days}d (${base:.2f} to ${price:.2f}).")
            elif rule.kind == "sell_signal" and pct >= rule.threshold:
                msg = (f"Sell signal: {label} is up {pct:.0f}% over the last "
                       f"{rule.window_days}d (${base:.2f} to ${price:.2f}).")

        if msg:
            alerts.append(Alert(
                user_id=rule.user_id,
                rule_id=rule.id,
                catalog_card_id=ccid,
                kind=rule.kind,
                message=msg,
            ))
    return alerts


def evaluate_all(s: Session) -> int:
    """Evaluate every active rule; create Alert rows. Returns alerts created."""
    now = utcnow()
    created = 0
    rules = s.query(AlertRule).filter_by(active=True).all()
    for rule in rules:
        if rule.last_triggered_at and now - rule.last_triggered_at < _cooldown(rule):
            continue
        fired = _evaluate_rule(s, rule, now)
        if fired:
            for a in fired:
                s.add(a)
            rule.last_triggered_at = now
            created += len(fired)
    return created
