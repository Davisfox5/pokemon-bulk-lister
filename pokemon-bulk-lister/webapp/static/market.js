/* Marketplace: browse, filter, buy. Helpers come from common.js. */
"use strict";

const mk = { listings: [], payments: "offline" };

async function load() {
  const data = await api("/api/market/listings");
  mk.listings = data.listings;
  mk.payments = data.payments;
  $("#market-payment").textContent = data.payments === "stripe" ? "Pay by card" : "Pay the seller directly";
  $("#market-meta").textContent =
    `Sellers pay a ${data.fee_pct}% platform fee. ${
      data.payments === "stripe" ? "Card payments are handled by Stripe." : "Arrange payment directly with the seller. Cards enter your library after the seller confirms payment."}`;

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
  const filterCount = [$("#mk-set").value, $("#mk-cond").value, $("#mk-below").checked, $("#mk-hide-mine").checked].filter(Boolean).length;
  $("#mk-filter-count").textContent = filterCount ? String(filterCount) : "";
  $("#mk-count").textContent = mk.listings.length
    ? rows.length === mk.listings.length ? `${rows.length} listing${rows.length === 1 ? "" : "s"}` : `${rows.length} of ${mk.listings.length} listings`
    : "";
  if (!mk.listings.length) {
    grid.innerHTML = '<div class="market-empty"><span class="empty-card-mark" aria-hidden="true">▱</span><h2>No cards listed yet</h2><a class="btn" href="/portfolio">Choose from your library</a></div>';
    return;
  }
  if (!rows.length) {
    grid.innerHTML = '<div class="market-empty"><h2>No matching cards</h2><p class="muted">Try a different search or clear your filters.</p><button type="button" class="btn" data-clear-market>Clear search &amp; filters</button></div>';
    return;
  }
  grid.innerHTML = rows.map((l) => {
    const vs = l.vs_market_pct == null ? "" :
      `<span class="chg ${l.vs_market_pct <= 0 ? "up" : "down"}">${l.vs_market_pct > 0 ? "+" : ""}${l.vs_market_pct}% vs market</span>`;
    const action = l.mine
      ? '<span class="muted">your listing</span>'
      : l.pending
        ? '<span class="pill">sale pending</span>'
        : `<button class="btn btn-primary mc-buy" data-id="${l.listing_id}">Buy card</button>`;
    return `<article class="market-card ${l.provenance.flagged ? "flag" : ""}">
      ${thumbHtml(l, "mc-img")}
      <div class="mc-body">
        ${cardLine(l)}
        <div class="inv-set">${esc(conditionName(l.condition))}${l.rarity ? " · " + esc(l.rarity) : ""}</div>
        <div class="mc-price"><strong>${fmtUsd(l.price)}</strong>${vs}</div>
        <div>${l.market_price != null ? `<span class="prov">market ${fmtUsd(l.market_price)}</span>` : ""}${provenanceHtml(l.provenance)}</div>
        <div class="mc-foot"><span class="inv-set">seller ${esc(l.seller)} · ${ago(l.listed_at)}</span>${action}</div>
      </div>
    </article>`;
  }).join("");
}

$("#market-grid").addEventListener("click", async (e) => {
  if (e.target.closest("[data-clear-market]")) { clearFilters(true); return; }
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

function conditionName(value) {
  return ({ NM: "Near mint", LP: "Lightly played", MP: "Moderately played", HP: "Heavily played", DMG: "Damaged" })[value] || value;
}

function clearFilters(clearSearch = false) {
  $("#mk-set").value = "";
  $("#mk-cond").value = "";
  $("#mk-below").checked = false;
  $("#mk-hide-mine").checked = false;
  if (clearSearch) $("#mk-search").value = "";
  render();
  if (clearSearch) $("#mk-search").focus();
}

$("#mk-clear").addEventListener("click", () => clearFilters());

["#mk-search", "#mk-set", "#mk-cond", "#mk-sort", "#mk-below", "#mk-hide-mine"].forEach((sel) =>
  $(sel).addEventListener("input", render));

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !e.target.closest("input, textarea, select") && !document.querySelector("dialog[open]")) {
    e.preventDefault();
    $("#mk-search").focus();
  }
});

// Sent back here by Stripe when the buyer backs out of checkout. The server
// route only redirects; the order is cancelled from here, same-origin, so a
// link on another site cannot cancel anyone's checkout.
(function abandonedCheckout() {
  const params = new URLSearchParams(location.search);
  if (params.get("payment") !== "cancelled") return;
  const order = Number(params.get("order"));
  history.replaceState(null, "", location.pathname);
  if (!order) return;
  postJson(`/api/market/orders/${order}/cancel`)
    .then(() => toast("Checkout was cancelled. You were not charged, and the card is for sale again."))
    .catch((err) => toast(/already been paid/.test(err.message)
      ? "That payment had already gone through. The card is in your library."
      : "Checkout was left unfinished. The card is released when the checkout expires.", "err"))
    .finally(() => load().catch(() => {}));
})();

// Deep link from the deck page: /market?q=Dragapult+ex
const preset = new URLSearchParams(location.search).get("q");
if (preset) $("#mk-search").value = preset;

load().catch((err) => toast(err.message, "err"));
// No timer: an idle open tab asks for nothing (every request wakes the
// database). Coming back to the tab reads the listings once.
let lastLook = Date.now();
const lookAgain = () => {
  if (document.hidden || document.querySelector("dialog[open]") || Date.now() - lastLook < 60_000) return;
  lastLook = Date.now();
  load().catch(() => {});
};
document.addEventListener("visibilitychange", lookAgain);
window.addEventListener("focus", lookAgain);
