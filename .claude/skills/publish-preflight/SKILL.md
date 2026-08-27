---
name: publish-preflight
description: Gate before scripts/07_publish_listings.py puts live listings on eBay. Checks the data, the credentials, the diff, and the blast radius, then reports GO or NO-GO with exact reasons. Use before any publish run, when the user says "publish preflight" or asks whether it is safe to list, or after touching a sensitive path.
allowed-tools: Read Grep Glob Bash(sqlite3 *) Bash(git *) Bash(python3 -c *)
---

# Publish preflight

`scripts/07_publish_listings.py` creates **live listings on a real marketplace**
with real prices. It is the one step in this pipeline that cannot be undone by
re-running it.

**This skill never publishes.** It reports GO or NO-GO. Running the publish step
is Davis's action, always, and this skill does not offer to do it.

## Working tree

Changed files:

!`git status --porcelain 2>/dev/null | head -40`

Sensitive paths touched in the diff:

!`git diff HEAD --name-only 2>/dev/null | grep -E "(lib/pricing\.py|lib/ebay_lister\.py|lib/ebay_oauth\.py|webapp/db\.py)" || echo "none"`

## Checks

Run all of them. Report every failure, not just the first.

### 1. Sensitive paths

Per CLAUDE.md, four files are sensitive:

- `pokemon-bulk-lister/lib/pricing.py` (sets real list prices)
- `pokemon-bulk-lister/lib/ebay_lister.py` (publishes live eBay listings)
- `pokemon-bulk-lister/lib/ebay_oauth.py` (client secret and disk-cached refresh token)
- `pokemon-bulk-lister/webapp/db.py` (schema and migrations run on every launch)

If any is modified and uncommitted, that is **NO-GO**. Publishing on top of an
unreviewed change to pricing or the lister is how a bad price reaches every
listing at once. Commit and review first.

### 2. Nothing unresolved in the data

```bash
sqlite3 output/db.sqlite "
  SELECT
    SUM(needs_review) AS needs_review,
    SUM(outlier_flag) AS outliers,
    SUM(pricing_confidence < 0.6) AS low_conf,
    SUM(final_price IS NULL) AS unpriced,
    SUM(id_confidence < 0.6) AS weak_id
  FROM cards WHERE ebay_listing_id IS NULL;"
```

Any non-zero count in `needs_review`, `outliers`, or `low_conf` is **NO-GO**
until `/price-sanity` has walked them. `unpriced` or `weak_id` above zero is
NO-GO for those rows specifically.

### 3. Price sanity in aggregate

```bash
sqlite3 -header -column output/db.sqlite "
  SELECT COUNT(*) AS n, ROUND(SUM(final_price),2) AS total,
         ROUND(MIN(final_price),2) AS min, ROUND(MAX(final_price),2) AS max,
         ROUND(AVG(final_price),2) AS avg
  FROM cards WHERE ebay_listing_id IS NULL AND final_price IS NOT NULL;"
```

Report the batch size and total value up front. Then flag:

- Any `final_price` under 0.25 or over 200 in a bulk batch. Both are usually
  mistakes in a bulk run and both should be confirmed by name.
- A max more than 50x the average. One card carrying the batch deserves a look.
- More than 200 listings in one run. Say the number plainly and ask for
  confirmation of the blast radius before proceeding.

### 4. No double-listing

```bash
sqlite3 output/db.sqlite "
  SELECT COUNT(*) FROM cards
  WHERE ebay_listing_id IS NOT NULL AND ebay_listing_status != 'ENDED';"
```

Report how many are already live. Confirm the publish run targets only rows with
a null `ebay_listing_id`. Re-listing an active card is a duplicate listing and a
policy problem, not just a mistake.

### 5. Credentials and environment

- `lib/ebay_oauth.py` resolves a client secret and a disk-cached refresh token.
  Confirm they resolve from the environment, and confirm the target is the
  intended account. Never print a token, a secret, or a refresh token, in full
  or truncated.
- Confirm sandbox versus production explicitly. Say which one out loud in the
  report. A production run that the operator thought was sandbox is the worst
  outcome this gate exists to prevent.

### 6. Listing text

Titles and descriptions come from `scripts/05_generate_csvs.py` and follow the
marketplace listing row in `/df-writing`. Spot-check ten:

- Card name, set, and number present and searchable in the title
- Condition stated and matching `condition_guess`
- No hype, no emoji, no claim the data does not support
- No em-dashes

## Output

```
PUBLISH PREFLIGHT: GO | NO-GO
Target: <sandbox | PRODUCTION>
Batch:  <n> listings, $<total> total value

BLOCKERS
- <one line each, with the exact count or file>

WARNINGS
- <one line each>

If GO: the publish command is yours to run. This skill does not run it.
```

Default to NO-GO. A check that could not be run is a blocker, not a pass.
