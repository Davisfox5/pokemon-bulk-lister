# CLAUDE.md

## User instruction: keep the approved design

The user approved the ivory/forest redesign on 16 November 2026 and explicitly
asked Claude not to revert it. On 17 November they asked for a livelier green and
more dynamic, card-game styling on top of it; that is the palette below. Preserve `design.css`, shared navigation, compact
workflows, responsive layouts, and accessibility described below unless the user
asks for a new visual direction. Keep functional improvements, including stable
Scan photos and automatic library saving. A deployment of this branch before it
contained the redesign caused a regression; do not repeat that stale release.

Follow [AGENTS.md](AGENTS.md) for releases: integrate and push to
`claude/dreamy-archimedes-7c38oi`, run
`python pokemon-bulk-lister/scripts/prepare_release.py`, then deploy from the
printed staging root. The preflight requires the latest shared commit and the
approved design together. Do not deploy directly from an old or dirty checkout.

Repo: **pokemon-bulk-lister** — Python 3.11 Flask 3 web app (`pokemon-bulk-lister/webapp/`) plus a 7-step CLI pipeline (`pokemon-bulk-lister/scripts/`) that turns binder-page photos into priced bulk card listings for TCGPlayer, Whatnot, and eBay. Business logic in `pokemon-bulk-lister/lib/`; SQLite state in `webapp/db.py`; tests in `pokemon-bulk-lister/tests/`.

Two data layers share one SQLite file. The scan bench (`webapp/db.py`, raw sqlite3, `cards` table) is one unauthenticated bench on localhost; under a public host each signed-in account has its own (`owner_id`, set per request by `db.set_owner`, so use the `db` helpers instead of raw SQL on `cards`/`grids`, and wrap anything run on another thread in `_as_owner`). The multi-user layer (`webapp/models.py`, SQLAlchemy) holds users, the shared price catalog, inventory, the `library_events` ledger (every add, purchase and sale), decks and the marketplace. `models.py` has no column migrations (`create_all` only adds tables), so add tables there, not columns. An index on an existing table goes in `models._ensure_indexes`, which runs on every launch.

A photo may hold any number of cards in any layout. `lib/card_finder.py` finds them without a model call (outlines, what contains what, one size per photo), crops and squares each up with a small margin round the found outline (`CROP_MARGIN`, so an outline that sits a little inside the card does not cut the card off); `webapp/app.py` `_record_photo` files them and saves a contact sheet (cards three across) that page-level identification reads, so card i is always row i // 3, col i % 3. When there is no clean background or closed outline (a card held in a hand), it goes by the sharp, detailed patch of the photo plus rectangles built from straight edges (`_held_card`). A binder page (dark pockets, bright cards) is read by `_pocket_cards`: each card is a bright island with dark round it, which gives the card's own edge and not the pocket's; a homography fitted to the found cards places any that glare hid (`_complete_grid`), and `_snap_sides` moves each side onto the real card edge. It is used when it accounts for every card the outlines found. The fixed 3x3 split in `scripts/01_split_grids.py` is the fallback for a binder page that fills the frame.

Accounts must confirm their email before listing, buying or payouts (`auth.verified_required`); the operator is a confirmed address in `ADMIN_EMAILS`. Sales made on other sites come back through `webapp/sales_sync.py` (eBay orders API, CSV exports) and are matched to cards by SKU, which is the crop file stem. Seller payouts are Stripe Connect transfers (`webapp/payouts.py`). Cards already listed on other sites are read into the library by `webapp/listing_import.py` (eBay Trading API, TCGplayer and Whatnot CSVs); it never guesses which card a listing is, and unmatched ones wait in `external_listings` for a person. Each account can connect its own Google Drive, read-only (`webapp/drive.py`, `lib/google_drive.py`): photos come in through the bench route `/api/drive/import/photo`, spreadsheets through the same importers as an uploaded CSV (`listing_import.import_text`), and the refresh token is sealed in `app_state`. Files and folders are chosen in Google's own picker (`static/drive.js` loads `apis.google.com/js/api.js`; this is the one third-party script in the app, at the operator's request); `/api/drive/picker` hands the signed-in owner's browser a short-lived read-only access token for it, never the refresh token.

Outside services (eBay, Cloudinary, TCGplayer and Whatnot portal sessions, Stripe) are set up and checked from Scan → Connections (`webapp/connections.py`). Secrets stay in `.env`; no endpoint returns or stores one. The scan bench has no login, so `connections.same_origin_only` refuses cross-site POSTs; keep new bench routes behind it (it is a `before_request` hook on the app). A bench route that must stay reachable without the operator under a public host has to be listed in `connections._PUBLIC_BENCH_PREFIXES` / `_PUBLIC_BENCH_PATHS`.

Test command (matches CI): `pytest -q` run from `pokemon-bulk-lister/`. CI also runs `python -m compileall -q lib webapp scripts`. The app runs locally by default; no branch auto-deploys anywhere. `pokemon-bulk-lister/Dockerfile` serves it under a domain (gunicorn, one worker, data on `/data`). Under a non-local host `connections.operator_only_when_public` puts every bench route behind a login, and the routes that publish or use the install's outside accounts (`connections._operator_route`: publish, Connections, Terapeak, TCGplayer catalog upload, Cloudinary upload) behind the operator. The operator can do everything any account can.

It also runs on Vercel (`app.py`, `vercel.json`), where there is no lasting disk and no process between requests. `webapp/runtime.py` is the switch (`SERVERLESS`): both data layers use Postgres (`webapp/db.py` has a psycopg backend behind the same functions), photos go to Vercel Blob (`runtime.save_file` / `ensure_local`), eBay tokens and job progress are rows in `app_state`, and bench jobs run in slices driven by the status endpoints (`_pump` in `webapp/app.py`) instead of threads. New bench code must not assume a file it wrote is still on disk in a later request (call `runtime.ensure_local`), must not keep job state only in memory, and must not start a thread it expects to outlive the request. `TEST_DATABASE_URL=postgresql://... pytest -q` runs the suite on Postgres; `tests/test_serverless.py` runs the bench the way Vercel does.

Keeping the hosted bill down: builds are most of this project's Vercel usage, so the project skips preview builds (its Ignored Build Step is `[ "$VERCEL_ENV" != "production" ]`) and a release is one build, `vercel deploy --prod` from the clean staging repository root produced by `scripts/prepare_release.py` (not a preview promoted afterwards, which builds twice). Lists that carry a `crop_path` also carry `photo_url` (`runtime.add_photo_urls`, one lookup per list) so the page loads photos straight from storage; `/crops/<file>` still works but costs a function call and a database read per photo. Pages do not poll (see below): every request wakes the database, which is billed by the time it is awake.

Finish and private estimates: the bench has nullable `printing` (legacy null
means normal/holo according to `is_holo`). `lib/printing.py` normalizes normal,
holofoil, reverseHolofoil and unknown. `CatalogPrinting` stores explicit finish
metadata in a new table; legacy normal/holo variant keys remain valid. Never
substitute another finish's price. `InventoryValuation` holds owner-specific
spreadsheet estimates; they must never enter `CatalogCard` market prices or
`PriceSnapshot`. Use `catalog.item_valuation` for an owner's effective valuation.
Startup quarantines older shared spreadsheet values without assigning an owner.

## Model routing (dev)

Cost-aware, top-down routing for Claude Code subagents (`.claude/agents/`). The highest tier (fable) is the default for judgment-heavy work and delegates DOWN to cheaper tiers for mechanical work. No agent ever self-assesses its own capability and escalates upward; every escalation trigger is external (a fixed path rule, a failing test).

| Agent | Model | Invoke when |
|---|---|---|
| `codebase-analyst` | fable | Explaining architecture, tracing data/control flow (pricing flow, OAuth lifecycle). NOT for pure lookups — see scout-first rule. |
| `code-reviewer` | fable | Reviewing diffs/PRs; includes sensitive-path and migration-safety checklists; runs `pytest -q`. |
| `planner` | fable | Refactor strategies, roadmaps, rollout sequencing. Writes to `docs/` only. |
| `bug-hunter` | fable | Reproducing/localizing bugs; runs tests; proposes fixes but never writes them. |
| `security-reviewer` | fable | Auditing secrets/token handling, Playwright session state, Flask/SQL surface, dependency risk. |
| `spec-writer` | opus | Turning a fable-tier plan/diagnosis into an implementation spec in `docs/specs/`. |
| `code-writer` | sonnet | Implementing changes strictly against a spec; runs `pytest -q` and shows real output; stops rather than improvises. |
| `researcher` | sonnet | Gathering external API/library docs (eBay, pokemontcg.io, Cloudinary, anthropic SDK, Playwright); output is always unverified claims. |
| `code-scout` | haiku | Pure lookups: "where is X", "list call sites", "which files reference Y". file:line output only. |

### Fixed rules

1. **Sensitive-path rule (external trigger, never a judgment call).** Specs and edits touching these four files are authored at the fable tier directly; `spec-writer` and `code-writer` refuse them and report back:
   - `pokemon-bulk-lister/lib/pricing.py` (sets real list prices)
   - `pokemon-bulk-lister/lib/ebay_lister.py` (publishes live eBay listings)
   - `pokemon-bulk-lister/lib/ebay_oauth.py` (client secret + disk-cached refresh token)
   - `pokemon-bulk-lister/webapp/db.py` (schema + migrations run on every launch)

   *Revisit caveat:* this rule trades a small fable increase (4 files) for the large scout/writer reduction elsewhere. If sensitive-path work ever dominates the workload, revisit this rule — it would then be costing more than it saves.
2. **Scout-first rule.** Any pure search/lookup goes to `code-scout` (haiku), never to `codebase-analyst` (fable). Analyst is only for questions requiring interpretation.
3. **Researcher output is unverified.** All `researcher` findings are documentation claims, not verified behavior. Fable-tier consumers must treat them as unverified input and confirm against pinned versions (`requirements.txt`) and real behavior before relying on them. The researcher's own prompt enforces the labeling; this rule enforces the consumer side.

### Enforcement layers

- **Mechanically enforced** (applied by the harness whenever an agent is invoked): each agent's `model:` and `tools:` frontmatter. Read-only agents genuinely lack edit/write tools.
- **Advisory / prompt-enforced**: whether to delegate at all, and all path restrictions (planner→`docs/`, spec-writer→`docs/specs/`, the sensitive-path refusals). Frontmatter cannot scope file paths, so these live in agent prompts and this file. CLAUDE.md steers; frontmatter binds.

## Design

Stack: Flask with Jinja templates in `webapp/templates/`, vanilla CSS and JS in
`webapp/static/`. No framework, no build step, no UI library. Keep it that way.
This is a working tool for one operator, not a product surface.

### Theme and layout
- Shared colors, typography, controls, and responsive navigation live in
  `webapp/static/design.css`, loaded after each page's structural stylesheet.
  Use its tokens rather than adding hardcoded colors. It includes a matching
  dark palette and reduced-motion support.
- Light palette (brightened 17 November 2026 at the user's ask: the forest
  green read as dingy and hard to read): ivory `#f8f6ef`, paper `#fffefa`,
  near-black ink `#15231c`, grass-green accent `#12814a`, gold `#f5c542`.
  The five energy colours (`--grass`, `--fire`, `--water`, `--electric`,
  `--psychic`, together `--energy`) carry the decoration: the bar under the
  header, the title underline and sparks, the foil sheen on card photos. Keep
  decoration to generic shapes; no game artwork, characters or logos.
  Red/clay surfaces are reserved for actionable review warnings.
- System fonts, 14px body, responsive 28–36px page titles. Prices and counts use
  tabular numerals; card photos carry the visual weight.
- `_navigation.html` is shared by Scan, Library, Decks, and Marketplace. It has
  visible labels, current-page state, and a skip link to `#main-content`.
- `index.html` uses `style.css`; collection screens use `portfolio.css` plus
  `static/common.js`. All pages, including authentication, load `design.css`.
- Scan collapses its upload area once cards exist. Selection actions appear
  only while cards are selected. Preserve both empty and populated layouts.
- Library tabs: Unsold, Needs review, Sold, Purchased, Insights, Activity.
  Insights is worked out in the page from the ledger already loaded (no extra
  request). The wording is "Needs review", never "needs a look".
- Library keeps cards ahead of secondary dashboards. Price details and less
  frequent tools use native disclosures; keep their summary controls accessible.
- On narrow screens, Decks switches between the list and the selected deck,
  with Back, deep links, and browser history. Desktop retains the split view.
- Source and actionable uncertainty remain visible beside each price; numeric
  confidence and full comparisons live in keyboard/touch-accessible details.
  Use `provenanceHtml`, `askConfirm`, and `toast` on collection screens.

### Rules
- Density over polish. This screen gets used for hundreds of rows at a time, so
  favor compact tables, sticky headers, and keyboard flow over whitespace.
- Every price or valuation shows where it came from and how confident it is.
  A number with no provenance is a bug. Do not replace specific warnings with a generic success badge.
- Price notes are short plain sentences from `lib/price_notes.py` ("Prices vary:
  TCGplayer $1.06, Cardmarket $7.06.", "Set not confirmed. Check the set before listing.").
  Never store or show source codes, set ids, ratios, match levels, URLs or dashes in a
  note; technical detail goes to the log. Screens show at most two sentences, through
  `price_notes.plain()`, which also rewrites notes saved by older versions.
- Sources that are far apart always read exactly one way, built by
  `price_notes.disagreement()` from the card's own source prices at display time:
  "Prices vary: TCGplayer $1.06, Cardmarket $7.06. Using $4.06." Sources in the
  fixed order TCGplayer, Cardmarket, eBay (the year of sales when there is one), each
  to the cent, then the price used. Every older wording ("Prices disagree", ...) maps
  to it; do not write another.
- The price is always the median of the sources (the average of two); nothing is
  dropped (`pricing.aggregate`). Prices within `pricing.MIN_DISAGREEMENT_USD` ($2) of
  each other never vary, whatever their ratio; every check goes through
  `pricing.far_apart(a, b, ratio)` and the screens show "Range $0.07 to $0.30".
- "Prices vary" and "Single pricing source." (one source only) are a small yellow
  note beside the price (`.caution-note`, `provenance.caution`), not a flag: the card
  does not need review and is not held back from listing. The red flag is only for
  something a person has to resolve: which card it is, its finish or set, no price.
- A person resolves a flag by saving. Scan: Save in the editor (or a pick in Choose)
  sends `checked`; `api_patch_card` marks the identity verified and `db.update_card`
  clears `needs_review` for any card that is checked or has a price of the owner's
  own. It stays clear through later pricing; a change to
  the card that nobody saved (a retry from the photo) resets `cards.checked`. Library:
  a flagged row has Resolve (`resolveItem`), which saves a price or keeps the market
  one and records `inventory_checks` (`InventoryCheck`); the two pages carry the check
  to each other (`catalog.sync_checked`, `_checked_on_scan`). Cancel changes nothing.
- Outlier and low-confidence rows are visually distinct at a glance, using the
  flag tokens above, and are sortable to the top.
- Anything that spends money or publishes a listing confirms first and says what
  it is about to do, in plain words, with counts.
- No new dependencies for styling. If a pattern needs a library, it probably
  needs to be simpler.

## Runtime model routing (the shipped tool)

The `.claude/agents/` tiering above is **build-time** and out of scope for a
runtime routing audit. This section covers what the tool calls when it runs.

- Sign-in to the Claude API is `lib/anthropic_auth.py`: `ANTHROPIC_API_KEY` when set, otherwise keyless (Workload Identity Federation) with the host's identity token, which on Vercel arrives in the `x-vercel-oidc-token` request header.
- Two runtime LLM touchpoints. `lib/listing_ai.py` writes opt-in listing
  descriptions when eBay's own writer is not to be had: Haiku
  (`claude-haiku-5-5`, falling back to `claude-haiku-4-5`), overridable with
  `DESCRIPTION_MODEL`.
- `lib/vision_identify.py`. It calls Claude vision to
  identify cards, defaults to `claude-haiku-5-5` (falling back to
  `claude-haiku-4-5` when that is refused), and is overridable with
  `VISION_MODEL`. The prompt carries the newest sets from the catalog, and the
  model is told to read what is printed, not to name a set from memory.
- What the model read is never trusted on its own. `lib/card_catalog.py` checks
  it against the real catalog with no model call (name + "023/128" pin the card
  and its set) and `_checked_identity` in `webapp/app.py` sets the confidence
  from that: verified, probable, or not a real card. `POST /api/identify/verify`
  (Scan, "Check IDs") runs the same check over cards already on the bench. The
  catalog cache (sets, each set's card list) lives in `app_state` under
  `catalog:*`; `data/sets.json` is the bundled fallback. Tests run with
  `CARD_CATALOG_OFFLINE=1`.
- A set's card list falls back to the catalog's data repository on GitHub when
  its API is down. Japanese cards are named from a table with no model call
  (`data/pokemon_names_ja.json`, printed name to English name), filed with
  "(Japanese)" on the set name and no `set_code`, and checked by name against the
  English set that reprints them (`data/ja_sets.json`; numbers differ, so the
  printed number is kept). Their price is the English printing's, always flagged.
- What was read off a card (printed name, the total after the slash, the printed
  set code) is kept with it in `cards.id_read`, so a later check has it. A card
  the catalog could not settle gets a Choose button on Scan: `GET
  /api/cards/<id>/choices` lists the real cards it could be (no model call), or
  the person types it in or has the photo read again.
  When the sets that fit what was read hold no card of that name (or no set
  was read), the choices are every printing of the name (`card_catalog.printings`,
  one catalog search per name, cached). Two real names a letter apart (Latias,
  Latios) never match each other, whatever the number (`_same_name`).
- A card with no set is never priced by its name: `_price_card` makes no lookup
  and notes "Choose the set to price this card." (`price_notes.SET_NEEDED`).
- The Scan table keeps itself current with no timer: rows update from a job's
  progress and from each price as it lands, and the list is read once when the
  tab comes back into view. An idle open tab must make no requests (each one
  wakes the database), so do not add a polling interval. A named card with no price and no pricing note is priced in the
  background by the page (`priceWaitingCards`); changing which card it is clears
  both (`db.update_card`), which is how an edit gets priced again.
- A scanned card goes into the signed-in account's library by itself once it
  has a price and a confirmed name (`addPricedToLibrary` in `app.js` posts
  `/api/inventory/promote` with `auto: true`; `catalog.AUTO_ADD_ABOVE` is the
  identification confidence it must be above). A card still marked "Check ID"
  is held for a person. `/api/cards` marks rows already added (`in_library`).
- Listing text is built from the card's facts by default: `ebay_lister.card_title`
  and `card_description`, then the owner's own `cards.listing_notes` (shipping,
  condition details). Nothing is stated that the owner did not say or check.
  `GET /api/cards/<id>/listing` previews it. The title is always built.
- AI descriptions are opt-in: an unticked "Write descriptions with AI" box in
  the eBay publish dialog. Listings go live with the built description; each
  card is queued in `description_jobs` (`webapp/descriptions.py`) and its AI
  text replaces the built one on the card (`cards.ai_description`,
  `description_source`) and on the live listing when ready. eBay's own writer
  first (`lib/ebay_mapping.py`, the Inventory Mapping GraphQL API, scope
  `sell.inventory.mapping`): a batch of 10 goes as soon as 10 are waiting;
  fewer wait for the twice-daily runs (`/api/cron/daily` at 09:17 UTC and
  `/api/cron/descriptions` at 21:17 UTC) which send whatever is waiting. After
  20 eBay batches in a UTC day (200 cards), or when eBay refuses for the day,
  is not connected, lacks the scope, cannot write a card or has not answered in
  24 hours, our own writer (`lib/listing_ai.py`) writes it, silently. Ours is
  shown recent descriptions eBay wrote, with their facts, so it reads like
  eBay's. Only card facts go to either writer; notes are added under the
  result, never sent. Changing which card it is drops the AI text and queues a
  new one. The page asks the server to move them along only while eBay is
  working on a batch.
- The Library's "List on eBay" (the account's own eBay keys, `webapp/settings.py`)
  has the same unticked box. Those cards have no bench row: the job's `card_id`
  is 0 and its card JSON carries `inventory_item_id`, `user_id` and the offer;
  the text goes onto the listing only while its `Listing` is live. eBay's writer
  runs on the account's own eBay connection; the install's eBay is used only for
  the operator's (or a local) bench, so one account never spends the operator's
  daily allowance.
- `cards.list_price` is the owner's own price and overrides `final_price`
  wherever a card is listed or exported (`_cards_payload`,
  `ebay_lister.listing_price`); a card with one is not held back as flagged.
  `POST /api/cards/listing` sets price and notes on many cards at once; the
  Library's bulk "List for sale" takes one `price` for all the same way.
- The owner's price follows the card into the Library as `inventory_prices`
  (`InventoryPrice`, `catalog.your_prices` / `set_your_price`): carried on promote,
  kept in step both ways (`catalog.sync_your_price` from Scan, `_price_on_scan` from the
  Library's "Set your price"). The Library shows it as "Your price" with the market
  price beside it; the collection is still valued at market. It is the default price
  for a marketplace or eBay listing. The Library's selection bar lists the ticked
  cards on the account's own eBay (`listSelectedOnEbay`), one confirmation with counts.
- Editing a card that is live on eBay offers to update the listing
  (`POST /api/publish/ebay/revise`, `EbayLister.revise_card`: the inventory item
  and the offer are replaced in place). A listing only changes after a yes.
- A price is for the card's finish (`lib/printing.py`). Old sets list theirs as
  `unlimited`; a card printed in one ordinary finish takes that one; a reverse
  holo or 1st Edition price never stands in for another card
  (`tcgplayer_client._variant_key`).
- That is the correct tier and it should stay there. Identification is constrained
  extraction with a JSON schema through `output_config.format`, which is exactly
  Haiku's job, and it runs at binder-page volume where a higher tier would be
  expensive for no accuracy gain.
- `identify_page()` amortizes prompt overhead across a whole page. `identify_crop()`
  is the per-cell retry for cards the page pass could not read. Keep that shape.
- Runtime uses only Haiku, Sonnet, or Opus. **Fable is never called at runtime.**
- If a future path needs more judgment than identification (grading, condition
  assessment, dispute handling), give it its own constant rather than raising
  `DEFAULT_MODEL`.

### Design canvas

- Marketplace pass on `webapp/templates/market.html`, plus the buy confirmation
  the Design block requires:
  https://claude.ai/code/artifact/c50363e5-6ee1-437d-ad67-49f2b96e5ff9
  Adds controls (search, filters, sort), price provenance on every card, and
  surfaces flagged pricing instead of hiding it. Structure and nav unchanged.
