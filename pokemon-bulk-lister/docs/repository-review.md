# Repository, import and usability review

Reviewed the active site branch from PR #5 at `ce385bb76b16da763b353a17665bd121e0c64e48`.
This follow-up changes that branch's implementation; it should be reviewed against
PR #5, not against the older application on main.

## Correctness fixes

- Catalog matching preserves gender and printing marks (ex, V, VMAX, stars and
  delta). Fuzzy spelling corrections cannot cross those product boundaries.
- A name and number shared by several possible sets no longer verifies whichever
  set happens to be newest. Printed set information constrains the candidates;
  ambiguity or an unavailable competing set leaves the card for review.
- Every bench update path invalidates old source prices, sold counts and product
  links when identity, printing or condition changes. Existing listing records
  remain, so correcting a card does not make an existing listing disappear.
- Pricing cannot clear a doubtful identification's review flag. Identification
  and collector-number fallback warnings also stay in price provenance, including
  prices seeded into the library.
- The learned image cache stores the catalog-corrected result only after a
  successful check. A rejected cached identity falls through to identification.
- An unreadable photo is reported and removed while the other uploaded photos
  continue. The UI identifies skipped filenames instead of treating them as an
  unsupported extension alone.
- Empty or filtered views clear hidden selections. Old network responses cannot
  overwrite a newer filter result. Invalid or blocked preference storage no
  longer prevents the scan page from initializing.
- CSV upload chunks respect quoted multiline records and escaped quotes and count
  UTF-8 bytes in their JSON representation. Oversized individual rows produce a
  useful error instead of a broken file.
- Corrected the README's CI badge, which still linked to a different repository.

## Identification speed and accuracy

The pipeline still uses free image matching before page identification and single
crop retries. It does not introduce another model or raise the runtime model tier.

First use seeds the set cache from the bundled catalog rather than waiting for
network retries. Card lists try the catalog's static data mirror before its
pricing API; both retain the existing persistent caches. The daily set refresh
still obtains updates.

AI sheets contain only unresolved crops, with at most nine cards per call. A
17-card input is covered by two legible sheets rather than one increasingly tall
sheet. The serverless queue likewise handles at most nine IDs per identification
unit. Existing splitter crops are resized to the contact sheet's expected size;
no original page file needs to survive between requests.

A high model confidence no longer suppresses a crop retry when the catalog rejects
the page answer. Retry selection uses checked confidence. When the catalog is
offline, another image call cannot repair it: a legible answer stays flagged and
is not learned, without a needless retry.

These are structural improvements and regression-tested behaviors, not a measured
live accuracy or throughput claim. No paid identification requests were made.

## Human-facing design changes

- One product name and active navigation indication across Scan, Library, Decks
  and Marketplace, with short page introductions explaining each screen's job.
- Scan follows add photos, review cards, save or list. Card review precedes
  publishing and sales tools. Less frequent operations live under More tools;
  sales imports are collapsed by default.
- Search by name, set or number. Source comparison columns are optional; the
  estimate retains price confidence and source count. Card ID confidence has its
  own labeled status instead of sharing the ambiguous Conf. column.
- Phone review uses stacked card rows instead of making basic review depend on
  horizontal scrolling. Full comparison remains available as a table.
- Useful empty states, photo-taking advice and full condition labels. The screen
  explicitly asks the owner to inspect condition; identification is not grading.
- Visible keyboard focus, scan dialog focus containment and return, a skip link,
  and keyboard navigation and selected state on library tabs. Improved secondary
  text contrast on library, deck and marketplace screens.

## Validation and limits

Local validation: 264 passed, one skipped (the Postgres-only run).
Chromium checks passed at both widths with no JavaScript errors.

Run `pytest -q` and `python -m compileall -q lib webapp scripts app.py` from the app
folder. New regressions cover catalog ambiguity and variant names, cache behavior,
stale-price invalidation, mixed valid/invalid uploads, bounded sheets,
catalog-aware retries, offline handling and CSV record splitting. The existing
serverless smoke test also exercises recycled instances and ownership boundaries.

Browser checks use Chromium against a throwaway local database, at 1440px and
390px. They exercise upload, edit/save, search, selection after empty results,
price-source toggling, mobile review, keyboard focus, library tabs and the four
main screens. No live payments, sales, listing publication or production data
were changed.

Further work needs separate evidence:

- Benchmark a labeled collection of real photos, including glare, sleeves,
  touching cards, rotations and Japanese printings, before quoting accuracy or
  latency gains. The detector itself is unchanged by this review.
- Japanese names can be checked against English companion sets, but this does not
  verify a Japanese collector number or variant. English prices remain estimates
  requiring review. Foil and condition still require human inspection.
- Hosted jobs advance through status requests while the page is open. Bounded
  batches reduce request work but do not impose a hard deadline on external API
  retries; a durable worker queue is the next structural improvement.
- Concurrent hosted uploads can still race on filename allocation. Imports and
  several background operations still use locks local to an instance. A database
  uniqueness/retry design would address these without trusting a process lock.
- Real eBay, Stripe, Google Drive and seller-portal behavior requires service
  credentials and dedicated integration checks. Local browser checks and mocked
  service tests do not establish production service correctness.
