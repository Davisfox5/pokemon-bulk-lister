"""AI-written listing descriptions, for the cards whose owner asked for them.

The owner ticks "Write descriptions with AI" when publishing to eBay. The
listings go live straight away with the description built from the card's
facts; each card is queued here, and when its AI description arrives it
replaces the built one on the card and on the live listing.

Who writes it:
  eBay first. eBay's own writer (lib/ebay_mapping.py) takes 10 cards a
  request. A batch goes out as soon as 10 cards are waiting; fewer than 10
  wait for the twice-daily run (/api/cron/daily and /api/cron/descriptions,
  12 hours apart), which sends whatever is waiting. eBay returns results a
  few minutes later.
  Ours otherwise (lib/listing_ai.py), silently: once the owner has sent 20
  eBay batches today (200 cards), when eBay refuses for the day, when eBay
  is not connected or its token lacks the scope, or when eBay could not
  write a card or has not answered within a day.

Ours is shown descriptions eBay wrote for other cards, so it writes like
eBay. Only the card's facts go to either writer; the owner's notes are
added under the result, never sent.

Everything is a row in description_jobs and is moved along by pump(),
called after publishing, by the page while eBay is working, and by the two
scheduled runs. No thread outlives a request.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from lib.ebay_mapping import BATCH_SIZE, EbayMappingError, EbayMappingLimit
from lib.listing_ai import MAX_CARDS, card_facts
from webapp import db
from webapp.models import DescriptionJob, get_session, utcnow

EBAY_BATCHES_PER_DAY = 20                  # 200 cards a day, then ours
GIVE_EBAY = timedelta(hours=24)            # an eBay task unanswered this long: ours writes it
_CARD_KEYS = ("id", "crop_path", "name", "set_name", "set_code", "card_number", "rarity",
              "is_holo", "printing", "condition_guess", "image_url")


def _mine(q, owner: Optional[int]):
    return q.filter(DescriptionJob.owner_id.is_(None) if owner is None else DescriptionJob.owner_id == owner)


def _today_start():
    now = utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _limit_key(owner: Optional[int]) -> str:
    return f"descriptions:ebay-limit:{owner}:{_today_start():%Y-%m-%d}"


def _batches_key(owner: Optional[int]) -> str:
    return f"descriptions:ebay-batches:{owner}:{_today_start():%Y-%m-%d}"


def enqueue(cards: list[dict]) -> int:
    """Queue cards (bench rows shaped by app._cards_payload) for an AI
    description. A card already queued or written for the same facts is
    left as it is. Returns how many were queued."""
    owner = db.owner()
    s = get_session()
    queued = 0
    try:
        for card in cards:
            if not card.get("crop_path") or not card.get("name"):
                continue
            sku = Path(card["crop_path"]).stem
            facts = json.dumps(card_facts(card), sort_keys=True, ensure_ascii=False)
            job = _mine(s.query(DescriptionJob), owner).filter(DescriptionJob.sku == sku).first()
            if job is not None and job.facts == facts and job.status in ("waiting", "sent", "done"):
                continue
            if job is None:
                job = DescriptionJob(owner_id=owner, sku=sku)
                s.add(job)
            job.card_id = int(card["id"])
            job.facts = facts
            job.card = json.dumps({k: card.get(k) for k in _CARD_KEYS}, ensure_ascii=False)
            job.status, job.source, job.use_ours, job.task_id = "waiting", None, False, None
            job.description, job.error, job.applied = None, None, False
            job.queued_at, job.sent_at, job.done_at = utcnow(), None, None
            queued += 1
        s.commit()
    finally:
        s.close()
    return queued


def counts(owner: Optional[int] = None, use_current: bool = True) -> dict:
    """How this owner's descriptions stand: waiting, at eBay, written today."""
    owner = db.owner() if use_current else owner
    s = get_session()
    try:
        q = _mine(s.query(DescriptionJob), owner)
        return {
            "waiting": q.filter(DescriptionJob.status == "waiting").count(),
            "at_ebay": q.filter(DescriptionJob.status == "sent").count(),
            "written_today": q.filter(DescriptionJob.status == "done",
                                      DescriptionJob.done_at >= _today_start()).count(),
        }
    finally:
        s.close()


def status_for(sku: str) -> Optional[str]:
    owner = db.owner()
    s = get_session()
    try:
        job = _mine(s.query(DescriptionJob), owner).filter(DescriptionJob.sku == sku).first()
        return job.status if job else None
    finally:
        s.close()


def owners_with_work() -> list[Optional[int]]:
    s = get_session()
    try:
        rows = (s.query(DescriptionJob.owner_id)
                .filter((DescriptionJob.status.in_(("waiting", "sent")))
                        | ((DescriptionJob.status == "done") & (DescriptionJob.applied.is_(False))))
                .distinct().all())
        return [r[0] for r in rows]
    finally:
        s.close()


def _examples(s, n: int = 4) -> list[dict]:
    """Recent descriptions eBay wrote, with the facts they came from: what
    ours is asked to sound like."""
    rows = (s.query(DescriptionJob)
            .filter(DescriptionJob.source == "ebay", DescriptionJob.description.isnot(None))
            .order_by(DescriptionJob.done_at.desc()).limit(n).all())
    return [{"facts": json.loads(r.facts), "description": r.description} for r in rows]


def pump(*, flush: bool = False, mapping: Any = None, writer: Optional[Callable] = None,
         shape: Optional[Callable[[dict], dict]] = None, lister_factory: Optional[Callable] = None,
         seconds: float = 20.0) -> dict:
    """Move this owner's descriptions along as far as `seconds` allows.

    mapping: an EbayMapping, or None when eBay is not set up.
    writer:  lib.listing_ai.describe, or a stand-in.
    shape:   turns a bench row into the dict the eBay lister takes.
    lister_factory: makes an EbayLister, for updating live listings.
    flush:   also send a batch of fewer than 10 (the twice-daily runs)."""
    from lib import listing_ai
    from webapp import runtime

    writer = writer or listing_ai.describe
    owner = db.owner()
    end = time.monotonic() + seconds
    out = {"sent_to_ebay": 0, "from_ebay": 0, "written_by_us": 0, "applied": 0}
    s = get_session()
    try:
        now = utcnow()
        mine = lambda: _mine(s.query(DescriptionJob), owner)   # noqa: E731

        # 1. Collect what eBay has finished.
        tasks = [r[0] for r in mine().filter(DescriptionJob.status == "sent")
                 .with_entities(DescriptionJob.task_id).distinct().all() if r[0]]
        for task_id in tasks:
            if time.monotonic() > end:
                break
            jobs = mine().filter(DescriptionJob.status == "sent", DescriptionJob.task_id == task_id).all()
            result = None
            if mapping is not None:
                try:
                    result = mapping.result(task_id)
                except EbayMappingError as exc:
                    print(f"[descriptions] eBay task {task_id}: {exc}", file=sys.stderr)
            if result is None:
                if all(j.sent_at and now - j.sent_at > GIVE_EBAY for j in jobs):
                    for j in jobs:
                        j.status, j.use_ours, j.error = "waiting", True, "eBay did not answer within a day"
                continue
            for j in jobs:
                text = result["descriptions"].get(j.sku)
                if text:
                    j.status, j.source, j.description, j.done_at = "done", "ebay", text, now
                    out["from_ebay"] += 1
                else:
                    j.status, j.use_ours, j.error = "waiting", True, f"eBay wrote none ({result['status']})"
        s.commit()

        # 2. Waiting cards go to eBay ten at a time while today's batches last.
        waiting = mine().filter(DescriptionJob.status == "waiting").order_by(DescriptionJob.queued_at).all()
        to_ebay = [j for j in waiting if not j.use_ours]
        ours = [j for j in waiting if j.use_ours]
        sent_today = int((runtime.state_get(_batches_key(owner)) or {}).get("n", 0))
        ebay_ok = (mapping is not None and mapping.ready()
                   and not runtime.state_get(_limit_key(owner)))
        if ebay_ok:
            while to_ebay and sent_today < EBAY_BATCHES_PER_DAY and time.monotonic() < end:
                if len(to_ebay) < BATCH_SIZE and not flush:
                    break                          # fewer than 10: they wait for the next scheduled run
                batch, to_ebay = to_ebay[:BATCH_SIZE], to_ebay[BATCH_SIZE:]
                try:
                    task_id = mapping.start([json.loads(j.card) for j in batch])
                except EbayMappingLimit:
                    runtime.state_set(_limit_key(owner), {"at": now.isoformat()})
                    ours += batch + to_ebay
                    to_ebay = []
                    break
                except EbayMappingError as exc:
                    for j in batch:
                        j.use_ours, j.error = True, str(exc)[:300]
                    ours += batch
                    continue
                for j in batch:
                    j.status, j.task_id, j.sent_at = "sent", task_id, now
                sent_today += 1
                runtime.state_set(_batches_key(owner), {"n": sent_today})
                out["sent_to_ebay"] += len(batch)
                s.commit()
            if sent_today >= EBAY_BATCHES_PER_DAY:
                ours += to_ebay                    # past 200 a day: ours, without a word
        else:
            ours += to_ebay

        # 3. Ours writes the rest now.
        examples = _examples(s) if ours else []
        while ours and time.monotonic() < end:
            chunk, ours = ours[:MAX_CARDS], ours[MAX_CARDS:]
            try:
                texts = writer([{"sku": j.sku, "facts": json.loads(j.facts)} for j in chunk], examples)
            except Exception as exc:               # try again on the next run
                print(f"[descriptions] our writer: {exc}", file=sys.stderr)
                for j in chunk:
                    j.error = f"our writer: {exc}"[:300]
                break
            for j in chunk:
                if texts.get(j.sku):
                    j.status, j.source, j.description, j.done_at = "done", "ours", texts[j.sku], now
                    out["written_by_us"] += 1
            s.commit()
        s.commit()

        # 4. Put finished descriptions on their cards, and on live listings.
        lister = None
        for j in mine().filter(DescriptionJob.status == "done", DescriptionJob.applied.is_(False)).all():
            if time.monotonic() > end:
                break
            with db.connect(_bench_path()) as conn:
                row = db.get_card(conn, j.card_id)
                if row is not None and Path(row["crop_path"]).stem == j.sku:
                    row = db.update_card(conn, j.card_id, {"ai_description": j.description,
                                                           "description_source": j.source})
                else:
                    row = None                     # deleted since: nothing to put it on
            j.applied = True
            if row and row.get("ebay_listing_id") and row.get("ebay_listing_status") == "listed" and shape:
                try:
                    lister = lister or (lister_factory() if lister_factory else None)
                    if lister is not None:
                        lister.revise_card({**shape(row), "ebay_offer_id": row.get("ebay_offer_id")})
                        out["applied"] += 1
                except Exception as exc:
                    j.error = f"updating the eBay listing: {exc}"[:300]
                    print(f"[descriptions] revise {j.sku}: {exc}", file=sys.stderr)
            s.commit()
    finally:
        s.close()
    return {**out, **counts(owner, use_current=False)}


_bench_path_fn: Optional[Callable[[], str]] = None


def use_bench(path_fn: Callable[[], str]) -> None:
    """Where the scan bench lives (app.DB_PATH), set by the app at start."""
    global _bench_path_fn
    _bench_path_fn = path_fn


def _bench_path() -> str:
    if _bench_path_fn is None:
        raise RuntimeError("descriptions.use_bench was not called")
    return _bench_path_fn()
