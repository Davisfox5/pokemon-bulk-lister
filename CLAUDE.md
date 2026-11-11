# CLAUDE.md

Repo: **pokemon-bulk-lister** — Python 3.11 Flask 3 web app (`pokemon-bulk-lister/webapp/`) plus a 7-step CLI pipeline (`pokemon-bulk-lister/scripts/`) that turns binder-page photos into priced bulk card listings for TCGPlayer, Whatnot, and eBay. Business logic in `pokemon-bulk-lister/lib/`; SQLite state in `webapp/db.py`; tests in `pokemon-bulk-lister/tests/`.

Two data layers share one SQLite file. The scan bench (`webapp/db.py`, raw sqlite3, `cards` table) is single-user and unauthenticated. The multi-user layer (`webapp/models.py`, SQLAlchemy) holds users, the shared price catalog, inventory, the `library_events` ledger (every add, purchase and sale), decks and the marketplace. `models.py` has no column migrations (`create_all` only adds tables), so add tables there, not columns. An index on an existing table goes in `models._ensure_indexes`, which runs on every launch.

Accounts must confirm their email before listing, buying or payouts (`auth.verified_required`); the operator is a confirmed address in `ADMIN_EMAILS`. Sales made on other sites come back through `webapp/sales_sync.py` (eBay orders API, CSV exports) and are matched to cards by SKU, which is the crop file stem. Seller payouts are Stripe Connect transfers (`webapp/payouts.py`). Cards already listed on other sites are read into the library by `webapp/listing_import.py` (eBay Trading API, TCGplayer and Whatnot CSVs); it never guesses which card a listing is, and unmatched ones wait in `external_listings` for a person.

Outside services (eBay, Cloudinary, TCGplayer and Whatnot portal sessions, Stripe) are set up and checked from Scan → Connections (`webapp/connections.py`). Secrets stay in `.env`; no endpoint returns or stores one. The scan bench has no login, so `connections.same_origin_only` refuses cross-site POSTs; keep new bench routes behind it (it is a `before_request` hook on the app). A bench route that must stay reachable without the operator under a public host has to be listed in `connections._PUBLIC_BENCH_PREFIXES` / `_PUBLIC_BENCH_PATHS`.

Test command (matches CI): `pytest -q` run from `pokemon-bulk-lister/`. CI also runs `python -m compileall -q lib webapp scripts`. The app runs locally by default; no branch auto-deploys anywhere. `pokemon-bulk-lister/Dockerfile` serves it under a domain (gunicorn, one worker, data on `/data`). Under a non-local host `connections.operator_only_when_public` puts every bench route behind the operator login.

It also runs on Vercel (`app.py`, `vercel.json`), where there is no lasting disk and no process between requests. `webapp/runtime.py` is the switch (`SERVERLESS`): both data layers use Postgres (`webapp/db.py` has a psycopg backend behind the same functions), photos go to Vercel Blob (`runtime.save_file` / `ensure_local`), eBay tokens and job progress are rows in `app_state`, and bench jobs run in slices driven by the status endpoints (`_pump` in `webapp/app.py`) instead of threads. New bench code must not assume a file it wrote is still on disk in a later request (call `runtime.ensure_local`), must not keep job state only in memory, and must not start a thread it expects to outlive the request. `TEST_DATABASE_URL=postgresql://... pytest -q` runs the suite on Postgres; `tests/test_serverless.py` runs the bench the way Vercel does.

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

### Tokens (from webapp/static/style.css `:root`)
- Background `#fafafa`, panels white, foreground `#1a1a1a`, muted `#6b7280`
- Border `#e2e2e2`, radius 8px on panels
- Accent `#2563eb`, hover `#1e40af`, button surface `#f3f4f6`
- Flagged row `--row-flag #fff5f5` with `--row-flag-text #b00020`. This pair means
  "needs a human look" and is the only red in the system. Do not reuse it for
  decoration.
- System font stack, 14px base, `h1` 20px, `h2` 16px
- Numbers use `font-variant-numeric: tabular-nums` so columns line up

### Layout
- `header` with title left, stats right, one border-bottom rule
- `.panel` is the primary container. `.muted` and `.small` for secondary text.
- `.dropzone` for file input. `.hidden` toggles visibility.
- Screens: `index.html` (scan: intake, pricing, publish), `portfolio.html` (library:
  have / sold / purchased), `decks.html` (deck building), `market.html` (marketplace),
  plus `login.html` and `register.html`. All four share one nav: Scan, Library, Decks,
  Marketplace.
- `index.html` uses `style.css`. The other screens use `portfolio.css` and the helpers
  in `static/common.js` (`provenanceHtml` for price provenance, `askConfirm` for
  confirmations, `toast` for status). Use those rather than `alert()` or `prompt()`.

### Rules
- Density over polish. This screen gets used for hundreds of rows at a time, so
  favor compact tables, sticky headers, and keyboard flow over whitespace.
- Every price or valuation shows where it came from and how confident it is.
  A number with no provenance is a bug.
- Outlier and low-confidence rows are visually distinct at a glance, using the
  flag tokens above, and are sortable to the top.
- Anything that spends money or publishes a listing confirms first and says what
  it is about to do, in plain words, with counts.
- No new dependencies for styling. If a pattern needs a library, it probably
  needs to be simpler.

## Runtime model routing (the shipped tool)

The `.claude/agents/` tiering above is **build-time** and out of scope for a
runtime routing audit. This section covers what the tool calls when it runs.

- One runtime LLM touchpoint: `lib/vision_identify.py`. It calls Claude vision to
  identify cards, defaults to `claude-haiku-4-5`, and is overridable with
  `VISION_MODEL`.
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
