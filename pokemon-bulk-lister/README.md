# pokemon-bulk-lister

[![CI](https://github.com/Davisfox5/test-7/actions/workflows/ci.yml/badge.svg)](https://github.com/Davisfox5/test-7/actions/workflows/ci.yml)

A seven-step pipeline that turns photos of binder pages into ready-to-upload
bulk CSVs for **TCGPlayer**, **Whatnot**, and **eBay** — then publishes them
(eBay live via the Sell API; TCGPlayer/Whatnot via CSV upload).

```
input/grids/*.jpg
        │
        ▼  scripts/01_split_grids.py
output/crops/*.jpg
        │
        ▼  scripts/02_identify_cards.py  ◄── you fill this in via Claude Code vision
output/cards.json
        │
        ▼  scripts/03_enrich_pricing.py
output/cards_priced.json
        │
        ▼  scripts/04_upload_images.py
output/cards_priced.json  (with image_url)
        │
        ▼  scripts/05_generate_csvs.py
output/csvs/{tcgplayer,whatnot,ebay}_*.csv
        │
        ▼  scripts/06_review_report.py
output/review.html
        │
        ▼  scripts/07_publish_listings.py
eBay live listings (Sell API) + TCGPlayer/Whatnot CSV uploads
```

## Setup

```bash
cd pokemon-bulk-lister
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env, fill in the credentials below
```

## Web UI (recommended)

A Flask app on top of a local SQLite DB, with four screens.

```bash
python -m webapp.app
# http://localhost:5050
```

| Screen | URL | What it is for |
|---|---|---|
| Scan | `/` | Photos in, identified and priced cards out. Publish to eBay, TCGplayer, Whatnot. |
| Library | `/portfolio` | Every card you have, have sold and have purchased. Sell from here. |
| Decks | `/decks` | Popular decks ranked by how much of each you already own, with what is missing and where to buy it. |
| Marketplace | `/market` | Cards other users on this install have listed. |

Scan works without an account. Library, Decks and Marketplace need a login
(`/register`).

### Scan

1. Drop photos on the drop zone, or use **Take a photo** on a phone. Pick
   **Binder pages** (each photo is split into up to 9 cards) or **Single
   cards** (one card per photo).
2. With **Identify and price automatically** ticked, each upload starts one
   background job: identify the new cards, then price them. The **Identify +
   price new cards** button runs the same job by hand. It never re-identifies
   or re-prices a card that is already done, so running it again is free.
3. Rows that need a human look (low confidence, sources that disagree, a
   name-only match) are tinted and sorted to the top, with the reason under
   the price. Fix them with **Edit**.
4. **Add to library** moves priced cards (the selection, or all of them) into
   your library. From there you can sell them or check them against decks.

Identification needs `ANTHROPIC_API_KEY` for anything the free image match
cannot place (model set by `VISION_MODEL`, default Haiku, roughly a cent per
binder page). **Identify only** and **Re-price all** are still there for
running one half on its own, and **Use Terapeak** adds the 365-day eBay comps
to a re-price.

Every publish button says what it is about to do, with counts and a total,
and waits for a yes. The eBay one lets you leave out cards whose price is
flagged for review.

Data lives at `output/db.sqlite` (auto-created; auto-imports any existing
`output/cards.json` / `output/cards_priced.json` on first launch). The CLI
scripts below still work. They read and write the same `output/` files, so
you can mix and match.

### Library

Three tabs over one ledger:

- **Have**: current holdings with market price, 7-day change and value. Each
  price shows its sources (TCGplayer, Cardmarket, eBay sold), its confidence
  and its age. Search with `/`, filter to prices that need review, tick rows
  and **List selected** to put many cards on the marketplace at once.
- **Sold**: every sale, on this marketplace or anywhere else, with fees, net
  and realized gain. Realized gain only counts cards with a price paid on
  file, and says how many that is.
- **Purchased**: everything bought, with what you paid and what it is worth
  now.

Cards get into the library three ways: **Import scanned cards** (from Scan),
buying on the marketplace, or **Add a card** for anything bought elsewhere or
already owned. An unpaid marketplace order can be cancelled by either side
under **Listings & open orders**. They leave it by selling on the marketplace or by **Sold
elsewhere**, which records an off-site sale (eBay, a card show) with its
price, fees and date. The ✕ button removes a card without recording a sale.

The ledger is the `library_events` table. A sale on the marketplace writes a
row for both the seller and the buyer, so history survives the card changing
hands.

### Decks

**Popular right now** lists the most played archetypes from the largest
recent [Limitless TCG](https://play.limitlesstcg.com) tournaments. Each one
shows how many of its 60 cards your library already covers, and the list is
sorted so the deck you are closest to finishing comes first. The list shown
for an archetype is the best-placing player's actual 60 cards, and the page
names the player, placing, tournament and date.

Open a deck to see it card by card:

- **Have / Missing** per card. Any printing of a Trainer or Energy counts. A
  Pokémon counts only when the set and number match. A same-named Pokémon in
  another printing is shown as "check the printing" and is not counted,
  because same name does not always mean same card (sets often print two
  different Basics of one Pokémon).
- **Price each** from the catalog, with the same provenance line as the
  library. **Price the unpriced missing cards** looks up the rest in the
  background.
- **Estimated cost to finish**, which states how many of the missing cards it
  covers. Basic energy is left out.
- **Where to get it**: the cheapest listing on this marketplace with a Buy
  button, plus search links to TCGplayer and eBay. **Copy missing list**
  copies the shopping list as a decklist.

**Import a decklist** takes any list in Pokémon TCG Live export format (one
card per line: `4 Dragapult ex TWM 130`). **Save deck** keeps a popular list
as it is today.

Popular decks ship as a snapshot in `data/popular_decks.json`, so the page
works offline. **Refresh** pulls current lists from the Limitless API (4
requests, cached at `output/cache/decks/popular.json`). `data/set_codes.json`
maps Pokémon TCG Live set codes (TWM) to pokemontcg.io set ids (sv6).

### Marketplace

Search, filter by set and condition, sort by price or by distance from market
price. Every listing shows the market price it is being compared to and where
that number came from. Listings whose market price needs review are tinted
and say why. **Buy** confirms first and says how payment works.

### Free image identification (cuts AI cost ~50%+)

Before any AI call, each crop is first identified by image alone, at zero cost:

1. **Learned cache** — crops the app has already identified (AI or otherwise)
   are remembered by a visual fingerprint at
   `output/cache/art_index/learned.jsonl`. Duplicate cards and re-uploads
   never pay for AI again.
2. **Official-art match** — the crop is compared against the official
   pokemontcg.io card images, the way commercial scanner apps work. Build the
   index once (resumable; ~20-40 min for the full catalog, seconds per set):

   ```bash
   python scripts/08_build_art_index.py                 # full catalog
   python scripts/08_build_art_index.py --sets sv7,sv8  # or just your sets
   ```

Both layers accept a match only when it's unambiguous (validated on 153 real
through-sleeve binder photos: 42% matched with zero wrong answers — it even
corrected 8 AI misidentifications). Everything else falls through to
the AI, so quality never drops; `ART_MATCH=0` turns the layers off. Two known
limits: reverse-holo vs. regular foiling can't be told apart from art alone,
and same-art reprints across sets are deliberately left to the AI (it reads
the collector number).

### Advanced: list on your own eBay account

`/portfolio` → **⚙ Advanced** lets a signed-in user store their *own* eBay
developer keyset (Client ID / Cert ID / RuName from
[developer.ebay.com](https://developer.ebay.com/)) and run the one-time
consent flow. Once connected, an **eBay** button appears on each card in the
library's Have tab. It confirms the card and price, then publishes a live
listing via the official Sell API under *their* account. Users without keys
never see any of it.

## Credentials

### Cloudinary
Reuse the First XI Fitness account. Copy `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
`CLOUDINARY_API_SECRET` from that project's `.env` into this one.

### pokemontcg.io (TCGPlayer prices)
Free tier doesn't require a key for low volume, but signing up at
<https://dev.pokemontcg.io/> gets you a higher rate limit. Drop the key into
`POKEMONTCG_API_KEY` in `.env`.

### eBay developer API
The eBay client uses **OAuth client-credentials**, the Browse API, and the
Marketplace Insights API (sold-listing data).

1. Sign up at <https://developer.ebay.com/> (free).
2. Go to **My Account → Application Keysets** and create a *Production*
   keyset. Copy:
   - **App ID (Client ID)** → `EBAY_CLIENT_ID`
   - **Cert ID (Client Secret)** → `EBAY_CLIENT_SECRET`
3. **Marketplace Insights API is gated.** Apply for access via the same
   developer portal (under *User Access Tokens* / *API release notes →
   Marketplace Insights*). Approval is usually a few business days.
4. Leave `EBAY_MARKETPLACE_ID=EBAY_US` and `EBAY_ENV=production` unless you
   have a reason to change them.

If the script logs `403 — your app probably isn't approved for the
buy.marketplace.insights scope`, you're still waiting on access.

## Running the pipeline

```bash
# 1. Photograph binder pages, drop them in input/grids/
python scripts/01_split_grids.py

# 2. Open output/cards.json in this IDE; with Claude Code, view each crop
#    and fill in name / set_name / set_code / card_number / rarity / is_holo
#    / condition_guess / confidence. Re-run the script anytime to merge in
#    new crops without losing your edits.
python scripts/02_identify_cards.py

# 3. Look up prices and apply the aggregation rule
python scripts/03_enrich_pricing.py
# Optionally enable Terapeak headless scraping for 365-day eBay sold history:
python scripts/03_enrich_pricing.py --terapeak

# 4. Push crops to Cloudinary
python scripts/04_upload_images.py

# 5. Generate the three marketplace CSVs
python scripts/05_generate_csvs.py

# 6. Open output/review.html to triage low-confidence cards before upload
python scripts/06_review_report.py

# 7. Publish. eBay goes live via the official Sell API; TCGPlayer + Whatnot
#    upload the generated CSVs through the seller portal (headless browser).
python scripts/07_publish_listings.py --site ebay
python scripts/07_publish_listings.py --site tcgplayer --site whatnot
# Dry-run first to build payloads/CSVs without pushing anything:
python scripts/07_publish_listings.py --all --dry-run
```

## Publishing listings

The three sites are not equal — only eBay exposes a usable listing API for a
typical seller, so the publish step uses two mechanisms:

### eBay — official Sell API (recommended)
Each priced + image-uploaded card becomes a live fixed-price listing via the
Inventory API (`inventory_item` → `offer` → `publishOffer`). No scraping.

One-time setup:
1. Create a **RuName** under developer.ebay.com → *User Tokens → Get a Token
   from eBay via Your Application*; set its accepted redirect URL. Put the
   RuName in `EBAY_REDIRECT_URI`.
2. Authorize once:
   ```bash
   python -m webapp.setup_ebay
   ```
   Sign in, approve the `sell.inventory` / `sell.account` scopes, and paste the
   redirected URL back. The refresh token is cached at `EBAY_USER_TOKEN_PATH`
   and listing then runs headlessly until it expires (~18 months).
3. Publishing needs payment/return/fulfillment **business policies** and an
   inventory location. We auto-pick the account's first of each; pin specific
   IDs with `EBAY_*_POLICY_ID` / `EBAY_MERCHANT_LOCATION_KEY` if you have
   several. Opt into Business Policies in eBay account settings if you haven't.

Listing IDs are written back to `cards_priced.json` (CLI) and the SQLite DB
(web UI), so re-runs update existing offers rather than duplicating them.

### TCGPlayer & Whatnot — headless CSV upload (⚠️ TOS-grey)
Neither offers a public listing API to typical sellers. The supported path is
the bulk-inventory CSV this app already generates; this step just drives the
seller portal in a headless browser to submit it for you — the same Playwright
pattern as the Terapeak scraper. **This is against their Terms, is fragile (DOM
changes break it), and use is at your own risk** — keep it to a
personal/secondary account, or just upload the CSVs by hand.

```bash
pip install playwright && playwright install chromium
python -m webapp.setup_portal --site tcgplayer   # sign in once; session saved
python -m webapp.setup_portal --site whatnot
```

On parse failure the uploader dumps a screenshot + HTML to
`output/cache/<site>_debug/` and the upload URL/selectors can be overridden via
`TCGPLAYER_UPLOAD_URL` / `WHATNOT_UPLOAD_URL` in `.env`.

All of this is also wired into the web UI's **Publish** panel.

## Pricing aggregation

For each card we collect up to five numbers:

| source | how |
|---|---|
| `tcgplayer_market`     | pokemontcg.io `tcgplayer.prices.<variant>.market` (USD) |
| `cardmarket_trend_usd` | pokemontcg.io `cardmarket.prices.trendPrice` (EUR → USD via `EUR_USD_RATE`) |
| `ebay_median_30d`      | median of eBay sold listings (last 30d, NM, US, English) |
| `ebay_max_30d`         | max of same set |
| `terapeak_median_usd`  | optional — median of Terapeak Research sold listings (last 365d) via Playwright headless scrape |

Rule:

```
prices = [tcg, cardmarket_usd, ebay_median_30d, terapeak_median_usd]
# ebay_max_30d is displayed but NOT aggregated — it's the same comp set as
# ebay_median_30d and double-counting eBay biased prices high.
valid  = non_null(prices)

if len(valid) >= 3 and max(valid) > 2.5 * median(valid):
    final = median(valid minus max)          # drop outlier, flag for review
elif len(valid) == 2 and max > 2.5 * min and (max - min) > $2:
    final = min(valid)                       # conservative: don't over-list
else:
    final = median(valid)
```

`confidence` is `1.0` if the non-null prices are all within 20% of each
other, then decays linearly to `0.0` at 120% spread. Single-source falls
back to `0.5`. Anything below `0.6` (or any outlier) is marked
`needs_review = true` and bubbles to the top of `review.html`.

Two cost/robustness refinements in the webapp pipeline:

- **Cardmarket daily price-guide fallback** — when pokemontcg.io has no data
  (its catalog lags new sets), a cached daily download of Cardmarket's free,
  TOS-blessed price-guide JSON supplies a guarded, name-matched trend price
  (`CARDMARKET_FALLBACK=1`, cache under `output/cache/cardmarket/`).
- **Terapeak value gate** — "Run pricing (all)" skips the Terapeak scrape for
  cards confidently priced under `TERAPEAK_MIN_VALUE` (default $3); a 365-day
  comp adds nothing to a bulk common and each scrape costs ~5s + account risk.

## Repo layout

```
pokemon-bulk-lister/
├─ scripts/
│  ├─ 01_split_grids.py
│  ├─ 02_identify_cards.py     # schema-only stub
│  ├─ 03_enrich_pricing.py
│  ├─ 04_upload_images.py
│  ├─ 05_generate_csvs.py
│  ├─ 06_review_report.py
│  └─ 07_publish_listings.py   # eBay Sell API + TCGPlayer/Whatnot CSV upload
├─ lib/
│  ├─ pricing.py               # aggregation + confidence
│  ├─ tcgplayer_client.py      # pokemontcg.io wrapper
│  ├─ ebay_client.py           # OAuth client-credentials + Browse + Marketplace Insights
│  ├─ ebay_oauth.py            # user-token OAuth (authorization-code + refresh)
│  ├─ ebay_lister.py           # Sell Inventory API: create + publish listings
│  ├─ portal_uploader.py       # headless-browser CSV upload base
│  ├─ tcgplayer_lister.py      # TCGPlayer seller-portal CSV upload
│  ├─ whatnot_lister.py        # Whatnot Seller Hub CSV upload
│  ├─ decks.py                 # decklist parser, library matching, Limitless popular decks
│  └─ cloudinary_client.py
├─ webapp/
│  ├─ app.py                   # Scan bench: upload, identify, price, publish
│  ├─ db.py                    # bench SQLite schema + migrations
│  ├─ models.py                # users, catalog, inventory, ledger, decks, marketplace
│  ├─ catalog.py               # inventory CRUD + import from the bench
│  ├─ library.py               # sold / purchased ledger, record a sale, add a card
│  ├─ decks.py                 # deck endpoints
│  ├─ marketplace.py           # list, buy, orders
│  ├─ portfolio.py             # valuation, price history, alerts
│  └─ templates/, static/      # index, portfolio, decks, market, login, register
├─ data/
│  ├─ popular_decks.json       # bundled Limitless snapshot (offline fallback)
│  └─ set_codes.json           # Pokémon TCG Live code -> pokemontcg.io set id
├─ tests/
├─ input/grids/
├─ output/
│  ├─ crops/
│  └─ csvs/
├─ .env.example
├─ requirements.txt
└─ README.md
```

## Terapeak headless scraping (optional)

eBay's Marketplace Insights API is closed to new applicants and capped at 90 days.
The Terapeak Research UI inside Seller Hub has the same data going back ~365 days
and is free for any Seller Hub seller — but Terapeak's Subscription Terms ban
automated access. This is a personal one-off, so we use it anyway.

```bash
pip install playwright
playwright install chromium
# First run is headful — log in to eBay once; the session is saved.
python scripts/03_enrich_pricing.py --terapeak
```

Tunables in `.env`:
- `TERAPEAK_HEADLESS=1` — flip to `0` if you want to watch.
- `TERAPEAK_MIN_DELAY` / `TERAPEAK_MAX_JITTER` — pacing between searches (default 4s + 0–4s).
- `TERAPEAK_PROXIES` — comma-separated list. Round-robined; a new browser context is created per search.
- `TERAPEAK_STATE_PATH` — where the logged-in session is cached (default `output/cache/terapeak_state.json`). Delete it to re-login.

The scraper writes screenshots + HTML to `output/cache/terapeak_debug/` whenever parsing fails so selectors can be tuned without re-running the full pipeline.

## Out of scope (for now)

- PriceCharting and Pokedata.io paid tiers (data-license / commercial-use restrictions)
- PWCC / Fanatics Collect, Goldin, Heritage, REA scraping (TOS-prohibited)
- Condition assessment beyond a rough NM/LP/MP guess
- Direct *API* push for TCGPlayer & Whatnot — no public seller listing API, so
  those remain CSV-based (optionally auto-submitted via headless browser, see
  "Publishing listings"). eBay does publish directly via its Sell API.
