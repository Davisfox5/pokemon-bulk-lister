/* Marketplace: browse, filter, buy. Helpers come from common.js. */
"use strict";

const mk = { listings: [], payments: "offline" };

async function load() {
  const data = await api("/api/market/listings");
  mk.listings = data.listings;
  mk.payments = data.payments;
  $("#market-meta").textContent =
    `seller pays a ${data.fee_pct}% platform fee · payments: ${
      data.payments === "stripe" ? "card (Stripe)" : "settled directly with the seller"}`;

  const setSel = $("#mk-set");
  const current = setSel.value;
  const sets = [...new Set(mk.listings.map((l) => l.set_name).filter(Boolean))].sort();
  setSel.innerHTML = '<option value="">All sets</option>' +
    sets.map((s) => `<option${s === current ? " selected" : ""}>${esc(s)}</option>`).join("");
  render();
}

function visible() {
  const q = $("#mk-search").value.trim().toLowerCase();
  const set = $("#mk-set").value, cond = $("#mk-cond").value;
  const below = $("#mk-below").checked, hideMine = $("#mk-hide-mine").checked;
  const rows = mk.listings.filter((l) => {
    const hay = [l.name, l.set_name, l.set_code, l.card_number, l.rarity].filter(Boolean).join(" ").toLowerCase();
    return (!q || q.split(/\s+/).every((w) => hay.includes(w)))
      && (!set || l.set_name === set)
      && (!cond || l.condition === cond)
      && (!below || (l.vs_market_pct != null && l.vs_market_pct <= 0))
      && (!hideMine || !l.mine);
  });
  const deal = (l) => (l.vs_market_pct == null ? Infinity : l.vs_market_pct);
  const sorters = {
    newest: (a, b) => b.listing_id - a.listing_id,
    price_asc: (a, b) => a.price - b.price,
    price_desc: (a, b) => b.price - a.price,
    deal: (a, b) => deal(a) - deal(b),
    flagged: (a, b) => (b.provenance.flagged - a.provenance.flagged) || b.listing_id - a.listing_id,
  };
  return rows.sort(sorters[$("#mk-sort").value]);
}

function render() {
  const grid = $("#market-grid");
  const rows = visible();
  $("#mk-count").textContent = mk.listings.length ? `${rows.length} of ${mk.listings.length} listings` : "";
  if (!mk.listings.length) {
    grid.innerHTML = '<p class="muted">Nothing is for sale right now. List a card from your <a href="/portfolio">library</a>.</p>';
    return;
  }
  if (!rows.length) {
    grid.innerHTML = '<p class="muted">No listings match these filters.</p>';
    return;
  }
  grid.innerHTML = rows.map((l) => {
    const vs = l.vs_market_pct == null ? "" :
      `<span class="chg ${l.vs_market_pct <= 0 ? "up" : "down"}">${l.vs_market_pct > 0 ? "+" : ""}${l.vs_market_pct}% vs market</span>`;
    const action = l.mine
      ? '<span class="muted">your listing</span>'
      : l.pending
        ? '<span class="pill">sale pending</span>'
        : `<button class="btn btn-primary mc-buy" data-id="${l.listing_id}">Buy</button>`;
    return `<article class="market-card ${l.provenance.flagged ? "flag" : ""}">
      ${thumbHtml(l, "mc-img")}
      <div class="mc-body">
        ${cardLine(l)}
        <div class="inv-set">${esc(l.condition)}${l.rarity ? " · " + esc(l.rarity) : ""}</div>
        <div class="mc-price"><strong>${fmtUsd(l.price)}</strong>${vs}</div>
        <div>${l.market_price != null ? `<span class="prov">market ${fmtUsd(l.market_price)}</span>` : ""}${provenanceHtml(l.provenance)}</div>
        <div class="mc-foot"><span class="inv-set">seller ${esc(l.seller)} · ${ago(l.listed_at)}</span>${action}</div>
      </div>
    </article>`;
  }).join("");
}

$("#market-grid").addEventListener("click", async (e) => {
  const btn = e.target.closest(".mc-buy");
  if (!btn) return;
  const listing = mk.listings.find((l) => l.listing_id === Number(btn.dataset.id));
  btn.disabled = true;
  try {
    if (await buyListing(listing, mk.payments)) await load();
  } catch (err) {
    toast(err.message, "err");
    load().catch(() => {});
  } finally {
    btn.disabled = false;
  }
});

["#mk-search", "#mk-set", "#mk-cond", "#mk-sort", "#mk-below", "#mk-hide-mine"].forEach((sel) =>
  $(sel).addEventListener("input", render));

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !e.target.closest("input, textarea, select") && !document.querySelector("dialog[open]")) {
    e.preventDefault();
    $("#mk-search").focus();
  }
});

// Deep link from the deck page: /market?q=Dragapult+ex
const preset = new URLSearchParams(location.search).get("q");
if (preset) $("#mk-search").value = preset;

load().catch((err) => toast(err.message, "err"));
setInterval(() => { if (!document.querySelector("dialog[open]")) load().catch(() => {}); }, 60_000);
