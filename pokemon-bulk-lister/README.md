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

### Accounts, email confirmation and the operator

Registering sends a link to confirm the email address. Until it is opened the
account can keep a library and build decks, but cannot list, buy, settle a
sale, connect eBay, or set up payouts. The link works once, lasts 24 hours,
and only works in a browser that is logged in to that account (a logged-out
visitor is sent to log in first). **Send the link again** on the Library page
issues a new one. Accounts created before this change have to confirm too.

**Forgot your password?** on the login page mails a link to choose a new one
(printed in the console when SMTP is not set). It works once and lasts an
hour. Changing a password signs every other browser out of that account.
Because the link proves the mailbox, using it also confirms the address. The
page gives the same answer whether or not an account exists.

- With `SMTP_HOST` set (plus `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `SMTP_FROM`), the link is emailed.
- With no SMTP settings, the link is printed in the console window the app
  runs in. That suits a local install: whoever can read the console runs it.

The **operator** sees every seller's balance, triggers payouts, reads sales
back from other sites and sees refunds that need doing by hand. An account is the operator when its address
is listed in `ADMIN_EMAILS` (default `davisfox5@gmail.com`) **and** that
address has been confirmed. Registering first, or registering a listed
address without access to its mailbox, grants nothing.

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
and waits for a yes. The eBay one leaves out cards whose price is flagged
for review unless you tick the box. Set the sites up under **Connections**
(see below).

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
under **Listings & open orders**.

A fourth tab, **Activity**, lists every event in order. The ✕ button and
quantity edits are recorded there as removed or added, so cards in minus
cards out always equals what you hold. They leave it by selling on the marketplace or by **Sold
elsewhere**, which records an off-site sale (eBay, a card show) with its
price, fees and date. The ✕ button takes a card out with no sale (it shows
as removed under Activity).

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

## Connections: eBay, TCGplayer, Whatnot, Stripe

Everything outside the app is set up and checked in one place: **Scan →
Connections**. Each service shows whether it is connected and what the next
step is. Keys go in `.env` (restart the app after editing it); the panel never
shows or stores a key.

The Scan page has no login, so the app only answers requests addressed to
`localhost` / `127.0.0.1` and refuses POSTs that come from another website.
If you serve it under a real hostname (needed for the Stripe webhook), list
that hostname in `ALLOWED_HOSTS`.

### eBay (official Sell API)

Each priced card becomes a live fixed-price listing through the Inventory API
(`inventory_item` → `offer` → `publishOffer`). No scraping.

1. At developer.ebay.com create a keyset, and under *User Tokens → Get a Token
   from eBay via Your Application* create a **RuName** with an accepted URL.
   Put `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` and `EBAY_REDIRECT_URI` (the
   RuName, not a URL) in `.env`. Set `EBAY_ENV=sandbox` to rehearse against
   eBay's sandbox first.
2. In Connections, click **Open eBay consent page**, approve, copy the address
   of the page eBay sends you to, paste it, click **Connect**. The refresh
   token is saved at `EBAY_USER_TOKEN_PATH` and lasts about 18 months.
3. Click **Check account**. It reads (and changes nothing): that a token can
   be minted, that the account has payment, return and shipping **business
   policies**, the inventory location, and photo hosting. Opt into Business
   Policies in eBay account settings if the check says none exist. Pin
   specific ones with `EBAY_*_POLICY_ID` / `EBAY_MERCHANT_LOCATION_KEY` if you
   have several.
4. Add the three `CLOUDINARY_` values so card photos are hosted. With those
   set, **List on eBay** uploads any photo that is not hosted yet as part of
   the publish; there is no per-card step.

**List on eBay** then says how many cards and how much, leaves out flagged
prices unless you tick the box, and reports how many listed and the first
error if any failed. Listing ids are written back to the database, so a
second run updates offers instead of duplicating them.

`python -m webapp.setup_ebay` still does step 2 from a terminal.

### TCGplayer and Whatnot (CSV upload through your seller portal)

Neither site has a listing API for regular sellers. The supported route is
the bulk CSV this app generates (**Export CSVs**), uploaded by hand. The
automated route drives the seller portal in a browser to submit that CSV for
you. **That is against their terms and breaks when they change the page; use
it at your own risk, or upload by hand.**

1. `pip install playwright && playwright install chromium` (once).
2. In Connections, click **Sign in to Whatnot** (or TCGplayer). A Chromium
   window opens on the computer running the app. Sign in there yourself,
   including any 2FA.
3. Back in the app, click **I'm signed in, save it**. The session is saved to
   `output/cache/<site>_state.json`. Closing the window or clicking Cancel
   saves nothing.
4. Click **Check session**. It opens the import page in the background with
   the saved session and confirms you are still signed in and the upload field
   is there. It uploads nothing.

**TCGplayer Ids.** TCGplayer's upload identifies each row by a TCGplayer Id,
a number it assigns to one card in one condition and printing. Only
TCGplayer has those. In the seller portal, export your pricing CSV (Pricing,
Export), then in Connections, under TCGplayer, click **Upload export**. The
app looks each scanned card up in that file by name, number, set, condition
and printing, and writes the real Id. All five must agree exactly (a card scanned as
non-holo never takes a Holofoil row, and a card with no set name matches
nothing). A card with no match, or more than one possible match, is left out
and counted. Without an export the TCGplayer CSV
is empty. (Earlier versions wrote the pokemontcg.io card id into that column,
which TCGplayer does not recognise.) From the command line, save the export
as `output/cache/tcgplayer_catalog.csv`.

**Upload to Whatnot / TCGplayer** then confirms the row count and total first,
and for TCGplayer says how many priced cards are left out. If an upload fails, a screenshot and the
page HTML are written to `output/cache/<site>_debug/`; the import URL can be
changed with `WHATNOT_UPLOAD_URL` / `TCGPLAYER_UPLOAD_URL`.

The sign-in, session check and upload were tested end to end against a
stand-in portal page, not against the real sites, which need an account. The
real import URLs and form fields are the part most likely to need adjusting
on first use.

`python -m webapp.setup_portal --site whatnot` still does steps 2 and 3 from a
terminal.

### Sales read back (eBay, Whatnot)

When a card sells on another site, the app records it so the library, the
marketplace and the Scan page all agree the card is gone. The **Sales** panel
on the Scan page does this. It changes libraries, so unlike the rest of the
Scan page it needs the operator to be logged in.

Every listing this app creates carries a SKU: the card photo's file name
without its extension (`page01_r0c2`). That SKU is how a sale finds its card.

- **eBay**: read over eBay's Fulfillment API (`getOrders`), every 30 minutes
  (`EBAY_SALES_SYNC_MINUTES`, 0 turns it off) and on **Sync eBay sales**. It
  covers your account and any user who connected their own. It needs the
  read-only orders permission, which the consent page now asks for. **A
  connection made before this change does not have it: disconnect and connect
  eBay again under Connections.** Until then publishing keeps working and the
  panel says sales cannot be read.
- **Whatnot**: no orders API for regular sellers. Export your sales there as
  a CSV and use **Import Whatnot sales CSV…**. The file needs a SKU column
  and a price column; order id, quantity, fees and date are used when
  present. The app shows what it found and which columns it read before
  recording anything.
- **TCGplayer**: not read back. Its listings carry TCGplayer's own ids, not
  this app's SKU, so a sale there cannot be tied to one scanned card. Use
  **Sold elsewhere** in the library.

A sale only ever touches the library of the account it came from. Your eBay
account and your CSV imports match your own cards; a user's connected eBay
account matches that user's. If a SKU could mean cards in two accounts,
nothing is recorded and a person decides.

For each sale: the sold copies leave the library, the card comes off this
marketplace (an unpaid order for it is cancelled), a row with price and fees
goes in the ledger under **Sold**, and the Scan page tags the card **sold**
and stops including it in publishing and exports. Reading the same order or
file again records nothing twice. Unpaid orders are picked up once paid. If
eBay later reports a recorded sale as cancelled or fully refunded, the card
is put back and can be listed again.

A sale whose SKU matches no card is listed under the Sales panel with the
reason. Nothing is guessed from the title. Record it by hand with **Sold
elsewhere**, then **Dismiss** it.

A partial refund at eBay lowers the recorded sale to what you kept.

Limit: Whatnot's real export column names have not been seen, only inferred,
so the first import may report a missing column.

### Import listings you already have (eBay, TCGplayer, Whatnot)

**Library, Import listings** adds the cards you already list on other sites
to your library. Nothing changes on the other site, and nothing is put up for
sale on this app's marketplace. Each import shows a count first and waits for
a yes. Reading the same listings again adds nothing.

- **eBay**: read straight from the connected account (Trading API,
  `GetMyeBaySelling`), including listings made on eBay's own site.
- **TCGplayer**: upload the pricing export from the seller portal (Pricing,
  Export). It is the same file that supplies TCGplayer Ids. Rows with a
  quantity above zero are your listings; the rest of the file is ignored.
  TCGplayer has no API for sellers, so the file is the only way in.
- **Whatnot**: upload a CSV of your listings with Title, Quantity, Price and
  SKU columns (the layout of Whatnot's bulk upload template). Whatnot has no
  public seller API.

How a listing becomes a card:

- A listing whose SKU is a card this app scanned and listed is already in the
  library. It is linked, not added again.
- A TCGplayer row states its set, name, number, condition and printing, so it
  is added as that card.
- An eBay or Whatnot listing only has a title. The title must contain a
  collector number written as number/total (`130/167`). The card is the one
  with that number, in a set of that size, whose name appears in the title
  (looked up on pokemontcg.io). Condition is read from the title and is NM
  when the title does not say.
- Anything else (no number, a lot, several possible cards, a sealed product,
  a listing with variations) goes under **Needs a look**, where you pick the
  card or dismiss it. Lookups are rationed per import; **Try matching again**
  continues.

Imported cards carry a tag in the Have tab showing the site and asking price.
A card you had already added by hand is not recognised and will show twice.
Later changes on the other site (sold out, new quantity) do not update the
library; sales come back through the sales read-back above.

Not checked against the real sites: that eBay accepts this app's OAuth token
for the Trading call, and the column names of a real Whatnot listings file.

### Stripe (card payments on the marketplace)

Without Stripe, a buyer and seller settle directly and the seller clicks
**Mark paid**. With it, **Buy** sends the buyer to Stripe Checkout and the
card moves when the payment clears.

1. `pip install -r requirements.txt` (includes `stripe`).
2. Put `STRIPE_SECRET_KEY` in `.env`. Start with a test key (`sk_test_...`):
   buyers can then pay with the test card `4242 4242 4242 4242`, any future
   expiry, any CVC, and no money moves.
3. In Connections, click **Check key**.

How an order gets settled, whichever comes first (settling twice is harmless):

- The buyer's browser returns from Checkout and the app asks Stripe whether
  that session was paid. This needs no public URL, so it works on a local
  install.
- A signed webhook, if the app is reachable from the internet: point Stripe at
  `/api/market/stripe-webhook` for the `checkout.session.*` events and put the
  signing secret in `STRIPE_WEBHOOK_SECRET`. Without the secret the endpoint
  is off; unsigned events are never accepted.
- A sweep when the listing or the seller's orders are next looked at, for a
  buyer who paid and closed the tab.

An unfinished Checkout expires after `STRIPE_CHECKOUT_MINUTES` (31, Stripe's
minimum is 30) and the card goes back on sale. Backing out of Checkout frees
it at once. Either side can cancel an unpaid order.

A buyer is never left charged with nothing to show for it. A payment is
refunded automatically when it lands on an order that was cancelled, when the
card is no longer there, or when the amount or currency Stripe charged is not
what the order asked for (for example if tax or a promotion code is switched
on in the Stripe dashboard). If Stripe refuses a refund, the order is marked
"refund pending" and the operator sees it at the top of **Seller balances**
with the session id to refund by hand.

**Where the money goes.** Payments land in the Stripe account whose key is in
`.env`. The platform fee (`PLATFORM_FEE_PCT`, default 10) stays there and the
rest belongs to the seller.

**Automatic payouts (Stripe Connect).** A seller clicks **Set up payouts**
under **Listings & open orders** and is sent to Stripe to enter their details
and bank account; this app never sees them. From then on, when one of their
card sales settles, their share is sent to their Stripe account by transfer,
and Stripe pays it out to their bank on its schedule. Anything already owed
is sent the moment setup finishes. Each order is paid at most once, and money
the operator already paid by hand is not sent again. The app records its
intent to pay before asking Stripe, so a crash mid-payout cannot lead to a
second transfer: next time it asks Stripe what happened and carries on.

**Refunds and chargebacks after a sale.** If you refund a charge in the Stripe
dashboard, or a buyer disputes one with their bank, the app brings its books
in line:

- A full refund marks the order refunded, takes the seller's transfer back,
  and stops counting the sale in anyone's totals. The operator sees the order
  under **Seller balances** with **Return card to seller**, which moves the
  card back once the buyer has returned it.
- A partial refund recomputes the fee and the seller's share on what the
  buyer ended up paying and takes back the difference.
- A dispute puts the order on hold and takes the transfer back while the bank
  decides. If you win, the seller is paid again. If you lose, it ends as a
  refund.
- If Stripe cannot take the money back (the seller already withdrew it), the
  seller's balance goes negative and the panel says how much they owe you.
  Every later check tries the reversal again until it goes through.

The app learns of these through the signed webhook (add `charge.refunded`,
`charge.dispute.created` and `charge.dispute.closed` to the events Stripe
sends) or, on a local install, the operator's **Check Stripe for refunds**
button, which asks Stripe about each card order from the last 130 days.

This needs **Connect switched on for your Stripe account** (Stripe dashboard,
Connect). Without it, "Set up payouts" reports Stripe's error.

A seller who has not set up payouts keeps a balance. The operator sees it in
**Seller balances**, can pay it some other way and note it with **Record
payout**, and can retry failed transfers with **Pay out now**.

Checkout takes cards only. Payment methods that can fail days later would let
a seller be paid for a charge that then bounces.

Card payments and payouts were tested with the real Stripe library's objects
and signature check against an in-memory stand-in for Stripe's API, not
against Stripe itself. The calls follow Stripe's published docs. In test
mode, do one purchase and one payout setup before going live.

## Hosting it under a real domain

The app is built to run on your own machine. It can also be served under a
domain. The `Dockerfile` in this directory runs it with gunicorn.

What the host needs:

- **A container host with a persistent disk mounted at `/data`.** Everything
  the app writes lives there (the SQLite file, card photos, the eBay token,
  the session key). Without the disk every deploy starts empty.
- **One instance.** The app keeps job state and locks in memory and uses
  SQLite, so it cannot be scaled to several copies.
- **HTTPS in front**, which every hosting platform provides. The image sets
  `TRUST_PROXY=1` so links and cookies use https.
- **Environment variables**: `ALLOWED_HOSTS=your.domain` (requests for any
  other host are refused), `SECRET_KEY` (optional; one is generated on the
  disk), `ADMIN_EMAILS`, `SMTP_*` so confirmation and reset links are mailed
  (without SMTP they are only printed in the server log), plus whichever of
  `ANTHROPIC_API_KEY`, `POKEMONTCG_API_KEY`, `CLOUDINARY_*`, `EBAY_*` and
  `STRIPE_*` you use.
- **DNS**: point the domain at the host as the host's instructions say.
- Health check path: `/healthz`.

What changes under a domain:

- **Scan and Connections need the operator.** On localhost they have no
  login. Under any other host they answer only to a signed-in, confirmed
  `ADMIN_EMAILS` account. Everyone else can use the Library, Decks and
  Marketplace, add cards by hand and import listings, but cannot scan:
  the scan bench is a single shared workspace.
- **TCGplayer and Whatnot sign-in does not work on a server.** It opens a
  Chromium window on the machine the app runs on. Uploading the TCGplayer
  export and downloading the CSVs still works.
- Set the Stripe webhook and the eBay redirect to the domain.

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
│  ├─ ebay_orders.py           # eBay Fulfillment API: read sold orders
│  ├─ ebay_listings.py         # eBay Trading API: read active listings
│  ├─ tcgplayer_catalog.py     # real TCGplayer Ids from the seller's export
│  ├─ mailer.py                # SMTP, or the console when SMTP is not set
│  └─ cloudinary_client.py
├─ webapp/
│  ├─ app.py                   # Scan bench: upload, identify, price, publish
│  ├─ db.py                    # bench SQLite schema + migrations
│  ├─ models.py                # users, catalog, inventory, ledger, decks, marketplace
│  ├─ catalog.py               # inventory CRUD + import from the bench
│  ├─ library.py               # sold / purchased ledger, record a sale, add a card
│  ├─ decks.py                 # deck endpoints
│  ├─ marketplace.py           # list, buy, orders, Stripe checkout, payouts
│  ├─ connections.py           # setup + checks for eBay, portals, Stripe
│  ├─ sales_sync.py            # sales read back: eBay orders, CSV exports
│  ├─ listing_import.py        # listings on other sites read into the library
│  ├─ payouts.py               # Stripe Connect onboarding + automatic transfers
│  ├─ refunds.py               # refunds and disputes after a sale settled
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
