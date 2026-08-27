---
name: price-sanity
description: Walk the priced cards that need a human look. Pulls outlier_flag and low-confidence rows from the SQLite DB, groups them by why they were flagged, and works through them one at a time. Use after running 03_enrich_pricing.py, before generating CSVs or publishing, or when the user says "price sanity" or asks what needs review.
allowed-tools: Read Grep Glob Bash(sqlite3 *) Bash(python3 *)
---

# Price sanity walk

Pricing is where this pipeline costs real money. A card priced at 100x its value
does not sell; a card priced at 1/100th sells instantly and is gone. Both are
recoverable only before publish, which is what this skill is for.

## The data

State lives in `output/db.sqlite` (`webapp/db.DEFAULT_DB_PATH`). The columns that
matter on `cards`:

| Column | Meaning |
|---|---|
| `outlier_flag` | One source disagreed with the others past `OUTLIER_MULTIPLIER` (2.5x) in `lib/pricing.py` |
| `pricing_confidence` | 0 to 1. Below `LOW_CONFIDENCE_THRESHOLD` (0.6) is low |
| `needs_review` | Set by a human or by an earlier pass. Sticky |
| `pricing_notes` | Why the pricer decided what it decided |
| `id_confidence` | Identification confidence. A bad price often starts as a bad ID |
| `final_price` | What will be listed |
| `ebay_sold_count_30d`, `terapeak_sold_count_365d` | Thin comp counts. Low counts make any median unreliable |

## Procedure

1. Count the work before starting, so the size is known:

   ```bash
   sqlite3 output/db.sqlite "
     SELECT
       SUM(outlier_flag) AS outliers,
       SUM(pricing_confidence < 0.6) AS low_conf,
       SUM(needs_review) AS flagged,
       COUNT(*) AS total
     FROM cards WHERE final_price IS NOT NULL;"
   ```

2. Pull the queue, worst first. Money at risk is `final_price`, so order by it
   rather than by confidence alone: a low-confidence 15 cent common is not worth
   a human minute.

   ```bash
   sqlite3 -header -column output/db.sqlite "
     SELECT id, name, set_code, card_number, condition_guess,
            final_price, pricing_confidence, id_confidence,
            outlier_flag, ebay_sold_count_30d, terapeak_sold_count_365d,
            pricing_notes
     FROM cards
     WHERE final_price IS NOT NULL
       AND (outlier_flag = 1 OR pricing_confidence < 0.6 OR needs_review = 1)
     ORDER BY final_price DESC
     LIMIT 40;"
   ```

3. Group by cause before walking rows. The causes repeat and fixing a cause
   clears many rows at once:

   - **Bad identification.** `id_confidence` low, or `set_code` empty. The price
     is for the wrong card. Fix the ID, then re-price. Check the crop.
   - **Thin comps.** Both sold counts near zero. The median is one sale. The
     price is a guess and should be treated as one.
   - **Source disagreement.** `outlier_flag` set with healthy comps. Usually one
     source priced a different printing, condition, or language.
   - **Condition mismatch.** `condition_guess` disagrees with what the comps
     assume. Most comps are NM.
   - **Genuinely valuable.** High price, high confidence, flagged only because
     the absolute spread is large. These are fine and should be confirmed fast.

4. Walk rows one at a time. For each, state: what the card is, what the sources
   said, which one is wrong and why, and the price to use. Then move on. Do not
   batch-approve.

5. Record the outcome. Clear the flag only when the price is settled:

   ```bash
   sqlite3 output/db.sqlite "
     UPDATE cards
     SET final_price = <price>, needs_review = 0,
         pricing_notes = pricing_notes || ' | sanity: <one line why>'
     WHERE id = <id>;"
   ```

   A row that stays uncertain keeps `needs_review = 1` and gets a note saying
   what is unresolved. Leaving it flagged is a real answer.

## Rules

- Never clear `needs_review` without changing something or writing why it was
  fine. A cleared flag with no note is indistinguishable from a missed row.
- Never raise a price because a card "should" be worth more. Comps or nothing.
- A card whose identification is uncertain does not get priced. Fix the ID first.
- `lib/pricing.py` is a sensitive path. Adjusting one card's price is this
  skill's job; changing the pricing algorithm is not, and needs its own review.
- This skill never publishes anything. It ends at the database.
