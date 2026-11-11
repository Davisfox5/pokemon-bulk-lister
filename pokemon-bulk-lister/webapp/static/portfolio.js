/* Library dashboard: tiles, value chart, alerts, and the three library tabs
   (have / sold / purchased). Helpers come from common.js. */
"use strict";

const lib = {
  items: [],            // current holdings
  events: { sold: null, purchased: null, activity: null },
  totals: null,         // ledger totals
  tab: "have",
  selected: new Set(),  // inventory ids ticked for bulk listing
  listings: {},         // inventory_item_id -> active listing
  market: null,
};

/* ---------------- summary + tiles ---------------- */

async function loadSummary() {
  const data = await api("/api/portfolio/summary");
  const t = data.totals;
  $("#t-value").textContent = fmtUsd(t.value);
  $("#t-cards").textContent = t.card_count;
  $("#t-cards-sub").textContent = `${t.priced_count} of ${t.item_count} entries priced`;
  $("#t-cost").textContent = fmtUsd(t.cost_basis);
  const costSub = $("#t-cost-sub");
  if (t.cost_basis && t.value != null) {
    const gain = t.value - t.cost_basis;
    costSub.textContent = `${gain >= 0 ? "+" : ""}${fmtUsd(gain)} unrealized`;
    costSub.className = "tile-sub " + (gain >= 0 ? "up" : "down");
  } else {
    costSub.textContent = "from cards with a price paid";
    costSub.className = "tile-sub";
  }
  $("#alerts-unread").textContent = t.unread_alerts ? `(${t.unread_alerts} unread)` : "";
  const sched = data.scheduler || {};
  $("#t-value-sub").textContent = sched.last_cycle_at
    ? `prices checked ${ago(sched.last_cycle_at)}`
    : "from the latest catalog prices";

  lib.items = data.items;
  for (const id of [...lib.selected]) if (!lib.items.some((i) => i.id === id)) lib.selected.delete(id);
  $("#n-have").textContent = t.card_count;
  renderLibrary();

  const seen = new Set();
  const opts = [];
  for (const it of data.items) {
    if (!seen.has(it.catalog_card_id)) {
      seen.add(it.catalog_card_id);
      opts.push(`<option value="${it.catalog_card_id}">${esc(it.name)}</option>`);
    }
  }
  $('#rule-form select[name="catalog_card_id"]').innerHTML =
    '<option value="">All my cards</option>' + opts.join("");
}

async function loadLedger() {
  const all = await api("/api/library/events");
  lib.events.activity = all.events;
  lib.events.sold = all.events.filter((e) => e.kind === "sold");
  lib.events.purchased = all.events.filter((e) => e.kind === "purchased");
  lib.totals = all.totals;
  const t = lib.totals;
  $("#n-sold").textContent = t.sold_cards;
  $("#n-purchased").textContent = t.purchased_cards;
  $("#t-balance").textContent = fmtUsd(t.sold_net);
  $("#t-balance-sub").textContent = t.sold_cards
    ? `${t.sold_cards} sold, after ${fmtUsd(t.sold_fees)} in fees`
    : "nothing sold yet";
  if (lib.tab !== "have") renderLibrary();
}

/* ---------------- library tabs ---------------- */

const CHANNELS = {
  scan: "Scan", manual: "Entered", market: "Marketplace", ebay: "eBay",
  tcgplayer: "TCGplayer", whatnot: "Whatnot", in_person: "In person", other: "Other",
};

function matches(row, q) {
  if (!q) return true;
  const hay = [row.name, row.set_name, row.set_code, row.card_number, row.condition]
    .filter(Boolean).join(" ").toLowerCase();
  return q.split(/\s+/).every((w) => hay.includes(w));
}

function visibleItems() {
  const q = $("#lib-search").value.trim().toLowerCase();
  const flaggedOnly = $("#lib-flagged").checked;
  const unlistedOnly = $("#lib-unlisted").checked;
  const rows = lib.items.filter((it) =>
    matches(it, q)
    && (!flaggedOnly || it.provenance.flagged)
    && (!unlistedOnly || !lib.listings[it.id]));
  const by = $("#lib-sort").value;
  const val = (it) => (it.value == null ? -1 : it.value);
  const sorters = {
    value: (a, b) => val(b) - val(a),
    flagged: (a, b) => (b.provenance.flagged - a.provenance.flagged) || val(b) - val(a),
    change: (a, b) => Math.abs(b.change_7d_pct ?? 0) - Math.abs(a.change_7d_pct ?? 0),
    name: (a, b) => a.name.localeCompare(b.name),
    newest: (a, b) => b.id - a.id,
  };
  return rows.sort(sorters[by] || sorters.value);
}

function renderLibrary() {
  const have = lib.tab === "have";
  $("#have-table").classList.toggle("hidden", !have);
  $("#have-controls").classList.toggle("hidden", !have);
  $("#ledger-table").classList.toggle("hidden", have);
  $("#ledger-totals").classList.toggle("hidden", have);
  if (have) renderHave(); else renderLedger(lib.tab);
}

function renderHave() {
  const body = $("#inv-body");
  const rows = visibleItems();
  $("#lib-count").textContent = lib.items.length
    ? `${rows.length} of ${lib.items.length} entries` : "";
  if (!lib.items.length) {
    body.innerHTML = `<tr><td colspan="9" class="empty">Your library is empty.
      <a href="/">Scan binder pages</a> and click “Import scanned cards”, or use “Add a card”.</td></tr>`;
  } else if (!rows.length) {
    body.innerHTML = '<tr><td colspan="9" class="empty">No cards match these filters.</td></tr>';
  } else {
    body.innerHTML = rows.map(haveRow).join("");
  }
  syncSelection(rows);
}

function haveRow(it) {
  const chg = it.change_7d_pct;
  const chgTxt = chg == null ? "—" : `${chg > 0 ? "+" : ""}${chg}%`;
  const chgCls = chg == null ? "" : chg >= 0 ? "up" : "down";
  const listing = lib.listings[it.id];
  const sellCell = listing
    ? `<span class="listed">Listed ${fmtUsd(listing.price)}</span>
       <button class="btn btn-ghost" data-act="reprice" title="Change the asking price">Reprice</button>
       <button class="btn btn-ghost" data-act="unlist" title="Take the listing down">Unlist</button>`
    : `<button class="btn" data-act="sell" title="List on the marketplace">Sell</button>`;
  return `<tr data-id="${it.id}" class="${it.provenance.flagged ? "flag" : ""}">
    <td class="sel"><input type="checkbox" data-act="select" ${lib.selected.has(it.id) ? "checked" : ""}
        ${listing ? "disabled" : ""} aria-label="Select ${esc(it.name)}"></td>
    <td>${thumbHtml(it)}</td>
    <td>${cardLine(it)}</td>
    <td>${esc(it.condition)}</td>
    <td class="num">${it.quantity}</td>
    <td class="num price-cell">${fmtUsd(it.price)}${provenanceHtml(it.provenance)}</td>
    <td class="num"><span class="chg ${chgCls}">${chgTxt}</span></td>
    <td class="num">${fmtUsd(it.value)}</td>
    <td>
      <div class="row-actions">
        ${sellCell}
        <button class="btn btn-ghost" data-act="sold" title="Record a sale made somewhere else">Sold elsewhere</button>
        ${ebayStatus.authorized
          ? '<button class="btn btn-ghost" data-act="ebay" title="List on your eBay account">eBay</button>' : ""}
        <button class="btn btn-ghost" data-act="remove" title="Remove from library (not a sale)" aria-label="Remove">✕</button>
      </div>
    </td>
  </tr>`;
}

function syncSelection(rows = visibleItems()) {
  const pickable = rows.filter((it) => !lib.listings[it.id]);
  const picked = pickable.filter((it) => lib.selected.has(it.id)).length;
  const all = $("#lib-select-all");
  all.checked = pickable.length > 0 && picked === pickable.length;
  all.indeterminate = picked > 0 && picked < pickable.length;
  const btn = $("#btn-bulk-list");
  btn.textContent = `List selected (${lib.selected.size})`;
  btn.disabled = lib.selected.size === 0;
}

function renderLedger(kind) {
  const events = lib.events[kind];
  const head = $("#ledger-head"), body = $("#ledger-body"), strip = $("#ledger-totals");
  if (events == null) { body.innerHTML = '<tr><td class="muted">Loading…</td></tr>'; return; }
  const q = $("#lib-search").value.trim().toLowerCase();
  const rows = events.filter((e) => matches(e, q));
  $("#lib-count").textContent = events.length ? `${rows.length} of ${events.length} records` : "";
  const t = lib.totals || {};

  if (kind === "activity") {
    const WHAT = { added: "Added", purchased: "Purchased", sold: "Sold", removed: "Removed" };
    strip.innerHTML = `
      <span><strong>${t.added_cards}</strong> added</span>
      <span><strong>${t.purchased_cards}</strong> purchased</span>
      <span><strong>${t.sold_cards}</strong> sold</span>
      <span><strong>${t.removed_cards}</strong> removed without a sale</span>`;
    head.innerHTML = `<tr><th>Date</th><th></th><th>Card</th><th>What</th><th>Where</th>
      <th class="num">Qty</th><th class="num">Each</th></tr>`;
    body.innerHTML = rows.map((e) => `<tr>
      <td>${shortDate(e.occurred_at)}</td><td>${thumbHtml(e)}</td><td>${cardLine(e)}${noteHtml(e)}</td>
      <td>${WHAT[e.kind] || esc(e.kind)}</td><td>${CHANNELS[e.channel] || esc(e.channel)}</td>
      <td class="num">${e.quantity}</td><td class="num">${fmtUsd(e.unit_price)}</td>
    </tr>`).join("") || emptyRow(7, events.length ? "Nothing matches this search." : "Nothing has happened yet.");
    return;
  }

  if (kind === "sold") {
    const gainNote = t.sold_with_cost < t.sold_cards
      ? ` (covers ${t.sold_with_cost} of ${t.sold_cards} cards; the rest have no price paid on file)` : "";
    strip.innerHTML = `
      <span><strong>${t.sold_cards}</strong> cards sold</span>
      <span>Gross <strong>${fmtUsd(t.sold_gross)}</strong></span>
      <span>Fees <strong>${fmtUsd(t.sold_fees)}</strong></span>
      <span>Net <strong>${fmtUsd(t.sold_net)}</strong></span>
      <span>Realized gain <strong>${t.sold_with_cost ? fmtUsd(t.realized_gain) : "—"}</strong>${gainNote}</span>`;
    head.innerHTML = `<tr><th>Date</th><th></th><th>Card</th><th>Cond.</th><th>Where</th>
      <th class="num">Qty</th><th class="num">Each</th><th class="num">Fees</th>
      <th class="num">Net</th><th class="num">Gain</th></tr>`;
    body.innerHTML = rows.map((e) => `<tr>
      <td>${shortDate(e.occurred_at)}</td><td>${thumbHtml(e)}</td><td>${cardLine(e)}${noteHtml(e)}</td>
      <td>${esc(e.condition || "")}</td><td>${CHANNELS[e.channel] || esc(e.channel)}</td>
      <td class="num">${e.quantity}</td><td class="num">${fmtUsd(e.unit_price)}</td>
      <td class="num">${e.fees ? fmtUsd(e.fees) : "—"}</td><td class="num">${fmtUsd(e.net)}</td>
      <td class="num">${e.gain == null ? '<span class="muted" title="No price paid on file">—</span>'
        : `<span class="chg ${e.gain >= 0 ? "up" : "down"}">${e.gain >= 0 ? "+" : ""}${fmtUsd(e.gain)}</span>`}</td>
    </tr>`).join("") || emptyRow(10, events.length
      ? "No sales match this search."
      : "No sales yet. Use “Sell” to list a card here, or “Sold elsewhere” to record a sale made off this site.");
  } else {
    const unpriced = t.purchased_unpriced
      ? ` (${t.purchased_unpriced} cards have no price paid on file)` : "";
    strip.innerHTML = `
      <span><strong>${t.purchased_cards}</strong> cards purchased</span>
      <span>Spent <strong>${fmtUsd(t.purchased_spend)}</strong>${unpriced}</span>`;
    head.innerHTML = `<tr><th>Date</th><th></th><th>Card</th><th>Cond.</th><th>Where</th>
      <th class="num">Qty</th><th class="num">Paid each</th><th class="num">Total</th>
      <th class="num">Market now, each</th></tr>`;
    body.innerHTML = rows.map((e) => `<tr>
      <td>${shortDate(e.occurred_at)}</td><td>${thumbHtml(e)}</td><td>${cardLine(e)}${noteHtml(e)}</td>
      <td>${esc(e.condition || "")}</td><td>${CHANNELS[e.channel] || esc(e.channel)}</td>
      <td class="num">${e.quantity}</td><td class="num">${fmtUsd(e.unit_price)}</td>
      <td class="num">${fmtUsd(e.total)}</td><td class="num">${fmtUsd(e.market_price_now)}</td>
    </tr>`).join("") || emptyRow(9, events.length
      ? "No purchases match this search."
      : "No purchases yet. Cards you buy on the marketplace land here; use “Add a card” with a price paid for anything bought elsewhere.");
  }
}

const emptyRow = (n, text) => `<tr><td colspan="${n}" class="empty">${text}</td></tr>`;
const noteHtml = (e) => (e.note ? `<div class="inv-set">${esc(e.note)}</div>` : "");

$$("#library .tab").forEach((tab) => tab.addEventListener("click", () => {
  $$("#library .tab").forEach((t) => t.classList.toggle("active", t === tab));
  lib.tab = tab.dataset.tab;
  renderLibrary();
}));
["#lib-search", "#lib-sort", "#lib-flagged", "#lib-unlisted"].forEach((sel) =>
  $(sel).addEventListener("input", renderLibrary));

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !e.target.closest("input, textarea, select") && !document.querySelector("dialog[open]")) {
    e.preventDefault();
    $("#lib-search").focus();
  }
});

/* ---------------- row actions ---------------- */

const itemById = (id) => lib.items.find((i) => i.id === Number(id));

$("#inv-body").addEventListener("click", async (e) => {
  const el = e.target.closest("[data-act]");
  const row = e.target.closest("tr[data-id]");
  if (!el || !row) return;
  const item = itemById(row.dataset.id);
  if (!item) return;
  const act = el.dataset.act;
  try {
    if (act === "select") {
      if (el.checked) lib.selected.add(item.id); else lib.selected.delete(item.id);
      syncSelection();
    } else if (act === "sell") {
      openSell(item, null);
    } else if (act === "reprice") {
      openSell(item, lib.listings[item.id]);
    } else if (act === "unlist") {
      await postJson(`/api/market/listings/${lib.listings[item.id].listing_id}/cancel`);
      toast(`${item.name} is no longer listed.`);
      await loadMarket();
    } else if (act === "sold") {
      openSale(item);
    } else if (act === "ebay") {
      await listOnEbay(item, el);
    } else if (act === "remove") {
      const { ok } = await askConfirm({
        title: "Remove from library?",
        html: `<p><strong>${esc(item.name)}</strong> (${item.quantity}) leaves your library with no sale recorded.
               Any active listing for it is taken down.</p>
               <p class="muted">If you sold it, use “Sold elsewhere” so it shows in your sales.</p>`,
        confirm: "Remove",
      });
      if (!ok) return;
      await api(`/api/inventory/${item.id}`, { method: "DELETE" });
      toast(`Removed ${item.name}.`);
      refreshAll();
    }
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#lib-select-all").addEventListener("change", (e) => {
  for (const it of visibleItems()) {
    if (lib.listings[it.id]) continue;
    if (e.target.checked) lib.selected.add(it.id); else lib.selected.delete(it.id);
  }
  renderHave();
});

/* ---------------- sell / reprice dialog ---------------- */

let sellCtx = null;

function openSell(item, listing) {
  sellCtx = { item, listing };
  const form = $("#sell-form");
  $("#sell-title").textContent = listing ? `Reprice ${item.name}` : `List ${item.name} for sale`;
  $("#sell-submit").textContent = listing ? "Update price" : "List for sale";
  $("#sell-prov").innerHTML = `
    <p>Market price ${fmtUsd(item.price)}<br>${provenanceHtml(item.provenance)}</p>
    ${item.quantity > 1 ? `<p class="muted">You have ${item.quantity}. A listing sells one copy.</p>` : ""}`;
  form.price.value = (listing ? listing.price : item.price) ?? "";
  updateSellNet();
  openDialog($("#sell-dialog"));
  form.price.select();
}

function updateSellNet() {
  const price = parseFloat($("#sell-form").price.value);
  const pct = lib.market ? lib.market.fee_pct : 0;
  if (!(price > 0)) { $("#sell-net").textContent = ""; return; }
  const fee = Math.round(price * pct) / 100;
  const market = sellCtx.item.price;
  const vs = market ? ` · ${Math.round((price - market) / market * 100)}% vs market` : "";
  $("#sell-net").textContent =
    `Platform fee ${pct}% = ${fmtUsd(fee)}. You get ${fmtUsd(price - fee)} when it sells${vs}.`;
}
$("#sell-form").price.addEventListener("input", updateSellNet);

$("#sell-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const { item, listing } = sellCtx;
  const price = parseFloat(e.target.price.value);
  try {
    if (listing) {
      await postJson(`/api/market/listings/${listing.listing_id}`, { price }, "PATCH");
      toast(`${item.name} now listed at ${fmtUsd(price)}.`);
    } else {
      const res = await postJson(`/api/inventory/${item.id}/list-for-sale`, { price });
      toast(`Listed ${item.name} at ${fmtUsd(res.price)}. Fee on sale: ${fmtUsd(res.fee_on_sale)}.`);
    }
    $("#sell-dialog").close();
    await loadMarket();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- bulk list ---------------- */

$("#btn-bulk-list").addEventListener("click", async () => {
  const item_ids = [...lib.selected];
  try {
    const p = await postJson("/api/inventory/list-for-sale", { item_ids, dry_run: true });
    const skips = [
      p.skipped_no_price ? `${p.skipped_no_price} with no market price` : "",
      p.skipped_already_listed ? `${p.skipped_already_listed} already listed` : "",
    ].filter(Boolean);
    if (!p.to_list && !p.skipped_flagged) {
      toast(`Nothing to list: ${skips.join(", ") || "no eligible cards"}.`, "err");
      return;
    }
    const { ok, checked } = await askConfirm({
      title: `List ${p.to_list} card${p.to_list === 1 ? "" : "s"} for sale?`,
      html: `<p>${p.to_list} card${p.to_list === 1 ? "" : "s"} go on the marketplace at their current
             market price, ${fmtUsd(p.total)} in total. The platform fee is ${p.fee_pct}% of each sale
             (${fmtUsd(p.fee_on_sale)} if they all sell).</p>
             ${skips.length ? `<p class="muted">Skipping ${skips.join(" and ")}.</p>` : ""}
             ${p.skipped_flagged ? `<p class="flag-note">${p.skipped_flagged} selected
               card${p.skipped_flagged === 1 ? " has a price that needs" : "s have prices that need"} review and
               will not be listed unless you tick the box.</p>` : ""}`,
      confirm: "List them",
      checkbox: p.skipped_flagged ? `Also list the ${p.skipped_flagged} with prices that need review` : null,
    });
    if (!ok) return;
    const res = await postJson("/api/inventory/list-for-sale", { item_ids, include_flagged: checked });
    toast(`Listed ${res.listed} card${res.listed === 1 ? "" : "s"} for ${fmtUsd(res.total)}.`);
    lib.selected.clear();
    await loadMarket();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- record a sale made elsewhere ---------------- */

let saleItem = null;

function openSale(item) {
  saleItem = item;
  const form = $("#sale-form");
  form.reset();
  $("#sale-title").textContent = `Record a sale: ${item.name}`;
  form.quantity.max = item.quantity;
  form.price.value = item.price ?? "";
  form.sold_on.value = new Date().toISOString().slice(0, 10);
  openDialog($("#sale-dialog"));
}

$("#sale-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  try {
    const res = await postJson(`/api/inventory/${saleItem.id}/record-sale`, f);
    $("#sale-dialog").close();
    toast(`Recorded: ${res.event.quantity} × ${saleItem.name} for ${fmtUsd(res.event.net)} net.`);
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- add a card by hand ---------------- */

$("#btn-add").addEventListener("click", () => {
  $("#add-form").reset();
  openDialog($("#add-dialog"));
});

let suggestTimer = null;
let suggestions = [];
$("#add-form").name.addEventListener("input", (e) => {
  const q = e.target.value.trim();
  // Picking a suggestion fills in the printing details.
  const hit = suggestions.find((c) => suggestionLabel(c) === q);
  if (hit) {
    const form = $("#add-form");
    form.name.value = hit.name;
    form.set_name.value = hit.set_name || "";
    form.set_code.value = hit.set_code || "";
    form.card_number.value = hit.card_number || "";
    form.is_holo.checked = !!hit.is_holo;
    return;
  }
  clearTimeout(suggestTimer);
  if (q.length < 2) return;
  suggestTimer = setTimeout(async () => {
    try {
      suggestions = (await api(`/api/catalog/search?q=${encodeURIComponent(q)}`)).cards;
      $("#add-suggestions").innerHTML = suggestions
        .map((c) => `<option value="${esc(suggestionLabel(c))}">`).join("");
    } catch { /* suggestions are optional */ }
  }, 200);
});
const suggestionLabel = (c) =>
  [c.name, c.set_name || c.set_code, c.card_number ? "#" + c.card_number : "", c.is_holo ? "holo" : ""]
    .filter(Boolean).join(" · ");

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  f.is_holo = e.target.is_holo.checked;
  f.purchased = f.channel !== "manual";
  try {
    const item = await postJson("/api/inventory/add", f);
    $("#add-dialog").close();
    toast(item.needs_price
      ? `Added ${item.name}. No price on file yet; “Refresh prices” looks it up.`
      : `Added ${item.name} at ${fmtUsd(item.price)} market price.`);
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- import from the scan bench ---------------- */

$("#btn-promote").addEventListener("click", async () => {
  const btn = $("#btn-promote");
  btn.disabled = true;
  try {
    const res = await postJson("/api/inventory/promote", { all_priced: true });
    toast(`Imported ${res.promoted} scanned card${res.promoted === 1 ? "" : "s"}` +
      (res.skipped_duplicates ? ` (${res.skipped_duplicates} were already imported)` : "") + ".");
    refreshAll();
  } catch (err) {
    toast(err.message === "no matching bench cards"
      ? "Nothing to import: no scanned cards have a price yet. Scan and price them first."
      : err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

$("#btn-refresh").addEventListener("click", async () => {
  const btn = $("#btn-refresh");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  try {
    const res = await postJson("/api/portfolio/refresh-prices");
    toast(`Looking up prices for ${res.cards} cards in the background.`);
    setTimeout(refreshAll, 6000);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = "Refresh prices"; }, 6000);
  }
});

/* ---------------- chart ---------------- */

let chartDays = 90;

async function loadChart() {
  const data = await api(`/api/portfolio/history?days=${chartDays}`);
  renderChart(data.series.filter((p) => p.value != null));
}

function renderChart(series) {
  const host = $("#chart");
  if (series.length < 2) {
    host.innerHTML = '<p class="muted">Not enough price history yet — the chart fills in as the scheduler collects snapshots.</p>';
    return;
  }
  const css = getComputedStyle(document.documentElement);
  const col = (name) => css.getPropertyValue(name).trim();

  const W = 960, H = 260, padL = 56, padR = 12, padT = 12, padB = 26;
  const xs = series.map((_, i) => i);
  const ys = series.map((p) => p.value);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const span = yMax - yMin || yMax || 1;
  const lo = Math.max(0, yMin - span * 0.08), hi = yMax + span * 0.08;
  const X = (i) => padL + (i / (xs.length - 1)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

  // ~4 horizontal gridlines at round values.
  const ticks = [];
  const step = niceStep((hi - lo) / 4);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) ticks.push(v);

  const path = series.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p.value).toFixed(1)}`).join("");

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="portfolio value over the last ${chartDays} days">
      ${ticks.map((v) => `
        <line x1="${padL}" x2="${W - padR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"
              stroke="${col("--grid")}" stroke-width="1"/>
        <text x="${padL - 8}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end"
              font-size="11" fill="${col("--muted")}" style="font-variant-numeric:tabular-nums">${shortUsd(v)}</text>`).join("")}
      <line x1="${padL}" x2="${W - padR}" y1="${H - padB}" y2="${H - padB}" stroke="${col("--baseline")}" stroke-width="1"/>
      <text x="${padL}" y="${H - 8}" font-size="11" fill="${col("--muted")}">${series[0].date}</text>
      <text x="${W - padR}" y="${H - 8}" text-anchor="end" font-size="11" fill="${col("--muted")}">${series[series.length - 1].date}</text>
      <path d="${path}" fill="none" stroke="${col("--series-1")}" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
      <line id="ch-cross" y1="${padT}" y2="${H - padB}" stroke="${col("--baseline")}" stroke-width="1" style="display:none"/>
      <circle id="ch-dot" r="4" fill="${col("--series-1")}" stroke="${col("--surface-1")}" stroke-width="2" style="display:none"/>
      <rect x="${padL}" y="${padT}" width="${W - padL - padR}" height="${H - padT - padB}" fill="transparent" id="ch-hit"/>
    </svg>
    <div class="chart-tip" id="ch-tip"><span class="tip-date"></span><br><span class="tip-val"></span></div>`;

  const svg = host.querySelector("svg");
  const hit = $("#ch-hit"), cross = $("#ch-cross"), dot = $("#ch-dot"), tip = $("#ch-tip");
  const toIdx = (evt) => {
    const r = svg.getBoundingClientRect();
    const px = ((evt.clientX - r.left) / r.width) * W;
    return Math.max(0, Math.min(xs.length - 1, Math.round(((px - padL) / (W - padL - padR)) * (xs.length - 1))));
  };
  hit.addEventListener("mousemove", (evt) => {
    const i = toIdx(evt);
    const x = X(i), y = Y(series[i].value);
    cross.style.display = dot.style.display = "block";
    cross.setAttribute("x1", x); cross.setAttribute("x2", x);
    dot.setAttribute("cx", x); dot.setAttribute("cy", y);
    tip.querySelector(".tip-date").textContent = series[i].date;
    tip.querySelector(".tip-val").textContent = fmtUsd(series[i].value);
    const r = svg.getBoundingClientRect();
    const left = (x / W) * r.width;
    tip.style.display = "block";
    tip.style.left = Math.min(r.width - 130, Math.max(0, left + 10)) + "px";
    tip.style.top = (y / H) * r.height - 48 + "px";
  });
  hit.addEventListener("mouseleave", () => {
    cross.style.display = dot.style.display = tip.style.display = "none";
  });
}

function niceStep(raw) {
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  for (const m of [1, 2, 5, 10]) if (raw <= m * mag) return m * mag;
  return 10 * mag;
}
function shortUsd(v) {
  if (v >= 1000) return "$" + (v / 1000).toFixed(v >= 10000 ? 0 : 1) + "k";
  return "$" + Math.round(v);
}

document.querySelectorAll(".range-btn").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".range-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    chartDays = +b.dataset.days;
    loadChart();
  }));

/* ---------------- alerts + rules ---------------- */

async function loadAlerts() {
  const data = await api("/api/alerts");
  const ul = $("#alerts-list");
  if (!data.alerts.length) {
    ul.innerHTML = '<li class="muted">No alerts yet. They appear here when your rules fire.</li>';
    return;
  }
  ul.innerHTML = data.alerts.map((a) => `
    <li class="${a.is_read ? "" : "unread"}">
      <span>${esc(a.message)}</span>
      <span class="alert-time">${shortDate(a.created_at)}</span>
    </li>`).join("");
}

$("#btn-read-all").addEventListener("click", async () => {
  await api("/api/alerts/read-all", { method: "POST" });
  loadAlerts();
  loadSummary();
});

const RULE_LABELS = {
  sell_signal: (r) => `Sell signal: up ≥${r.threshold}% in ${r.window_days}d`,
  pct_change: (r) => `Move ±${r.threshold}% in ${r.window_days}d`,
  price_above: (r) => `Price ≥ ${fmtUsd(r.threshold)}`,
  price_below: (r) => `Price ≤ ${fmtUsd(r.threshold)}`,
};

async function loadRules() {
  const data = await api("/api/alert-rules");
  const ul = $("#rules-list");
  ul.innerHTML = data.rules.map((r) => `
    <li>
      <span>${RULE_LABELS[r.kind] ? RULE_LABELS[r.kind](r) : esc(r.kind)}
        <span class="muted"> — ${r.card_name ? esc(r.card_name) : "all cards"}</span></span>
      <button class="btn btn-ghost rule-del" data-id="${r.id}">✕</button>
    </li>`).join("") || '<li class="muted">No rules.</li>';
}

$("#rules-list").addEventListener("click", async (e) => {
  const btn = e.target.closest(".rule-del");
  if (!btn) return;
  await api(`/api/alert-rules/${btn.dataset.id}`, { method: "DELETE" });
  loadRules();
});

$("#rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/alert-rules", {
      method: "POST",
      body: JSON.stringify({
        kind: f.get("kind"),
        threshold: parseFloat(f.get("threshold")),
        window_days: parseInt(f.get("window_days") || "30", 10),
        catalog_card_id: f.get("catalog_card_id") || null,
      }),
    });
    e.target.reset();
    loadRules();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- listings & open orders ---------------- */

const ORDER_STATUS = {
  pending_payment: '<span class="pill">awaiting payment</span>',
  paid: '<span class="pill ok">paid</span>',
  completed: '<span class="pill ok">completed</span>',
  cancelled: '<span class="pill muted">cancelled</span>',
  refund_due: '<span class="pill warn">refund pending</span>',
};

async function loadMarket() {
  const data = await api("/api/market/my");
  lib.market = data;
  $("#fee-note").textContent = `platform fee ${data.fee_pct}% of each sale · payments: ${
    data.payments === "stripe" ? "card (Stripe)" : "settled directly with the other person"}`;

  const money = $("#money-line");
  const hasMoney = data.card_proceeds || data.paid_out || data.fees_owed;
  money.classList.toggle("hidden", !hasMoney);
  if (hasMoney) {
    money.innerHTML = `
      <span>Your share of card sales <strong>${fmtUsd(data.card_proceeds)}</strong></span>
      <span>Paid out to you <strong>${fmtUsd(data.paid_out)}</strong></span>
      <span>Still owed to you <strong>${fmtUsd(data.balance)}</strong></span>
      ${data.fees_owed ? `<span>Fees you owe on sales settled directly <strong>${fmtUsd(data.fees_owed)}</strong></span>` : ""}`;
  }
  $("#operator-panel").classList.toggle("hidden", !data.is_operator);
  if (data.is_operator) loadOperator().catch(console.error);

  lib.listings = {};
  for (const l of data.active_listings) lib.listings[l.inventory_item_id] = l;
  for (const id of [...lib.selected]) if (lib.listings[id]) lib.selected.delete(id);
  if (lib.tab === "have") renderHave();

  const rows = [];
  for (const l of data.active_listings) {
    rows.push(`<li>
      <span>For sale: <strong>${esc(l.card)}</strong> at ${fmtUsd(l.price)}
        <span class="muted">market ${fmtUsd(l.market_price)} · you get ${fmtUsd(l.price - l.fee_on_sale)}</span></span>
      <button class="btn btn-ghost mk-cancel" data-id="${l.listing_id}">Unlist</button>
    </li>`);
  }
  // Orders stay here only while someone has to act; finished ones live in
  // the Sold and Purchased tabs.
  for (const o of data.sales.filter((o) => o.status === "pending_payment")) {
    const cancelBtn = `<button class="btn btn-ghost mk-void" data-id="${o.order_id}" data-card="${esc(o.card)}" title="The buyer never paid">Cancel order</button>`;
    const action = o.payment_provider === "offline"
      ? `<button class="btn mk-paid" data-id="${o.order_id}" data-card="${esc(o.card)}" data-amount="${o.amount}">Mark paid</button>${cancelBtn}`
      : `<span class="muted">paying by card</span> ${cancelBtn}`;
    rows.push(`<li>
      <span>Buyer waiting: <strong>${esc(o.card)}</strong> for ${fmtUsd(o.amount)}
        <span class="muted">you get ${fmtUsd(o.seller_proceeds)} after the ${fmtUsd(o.platform_fee)} fee</span>
        ${ORDER_STATUS[o.status]}</span>
      <span>${action}<span class="alert-time"> ${shortDate(o.created_at)}</span></span>
    </li>`);
  }
  for (const o of data.purchases.filter((o) => ["pending_payment", "paid"].includes(o.status))) {
    const action = o.status === "paid"
      ? `<button class="btn mk-complete" data-id="${o.order_id}">Card arrived</button>`
      : `<button class="btn btn-ghost mk-void" data-id="${o.order_id}" data-card="${esc(o.card)}">Cancel order</button>`;
    rows.push(`<li>
      <span>Buying: <strong>${esc(o.card)}</strong> for ${fmtUsd(o.amount)} ${ORDER_STATUS[o.status]}
        ${o.status === "pending_payment" ? `<span class="muted">${o.payment_provider === "stripe"
          ? "card checkout not finished; cancel and buy again to restart it"
          : "pay the seller; they confirm it"}</span>` : ""}</span>
      <span>${action}<span class="alert-time"> ${shortDate(o.created_at)}</span></span>
    </li>`);
  }
  $("#market-list").innerHTML = rows.join("") ||
    '<li class="muted">No active listings or open orders. Click “Sell” on a card, or browse the <a href="/market">marketplace</a>.</li>';
}

$("#market-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-id]");
  if (!btn) return;
  try {
    if (btn.classList.contains("mk-cancel")) {
      await postJson(`/api/market/listings/${btn.dataset.id}/cancel`);
      toast("Listing taken down.");
    } else if (btn.classList.contains("mk-paid")) {
      const { ok } = await askConfirm({
        title: "Mark this order paid?",
        html: `<p>Confirm you received ${fmtUsd(Number(btn.dataset.amount))} for
               <strong>${esc(btn.dataset.card)}</strong>. The card moves to the buyer's library
               and the sale is recorded. This can't be undone here.</p>`,
        confirm: "I've been paid",
      });
      if (!ok) return;
      await postJson(`/api/market/orders/${btn.dataset.id}/mark-paid`);
      toast("Sale recorded. The card is now in the buyer's library.");
    } else if (btn.classList.contains("mk-void")) {
      const { ok } = await askConfirm({
        title: "Cancel this order?",
        html: `<p>The order for <strong>${esc(btn.dataset.card)}</strong> is cancelled. No sale is recorded
               and the card goes back on the marketplace for other buyers.</p>`,
        confirm: "Cancel order",
      });
      if (!ok) return;
      await postJson(`/api/market/orders/${btn.dataset.id}/cancel`);
      toast("Order cancelled.");
    } else if (btn.classList.contains("mk-complete")) {
      await postJson(`/api/market/orders/${btn.dataset.id}/complete`);
      toast("Order completed.");
    }
    refreshAll();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- Advanced settings: bring-your-own eBay keys ---------------- */

let ebayStatus = { configured: false, authorized: false };

async function loadEbayStatus() {
  try {
    ebayStatus = await api("/api/settings/ebay");
  } catch (e) {
    ebayStatus = { configured: false, authorized: false };
  }
  const st = $("#ebay-status");
  if (!ebayStatus.configured) {
    st.textContent = "No keys saved — eBay listing is off.";
  } else if (!ebayStatus.authorized) {
    st.textContent = `Keys saved (${ebayStatus.client_id_hint}, ${ebayStatus.env}). ` +
      "Now connect your eBay account below.";
  } else {
    st.textContent = `✓ Connected (${ebayStatus.client_id_hint}, ${ebayStatus.env}) — ` +
      "“eBay” appears on each card in “My cards”.";
  }
  $("#ebay-remove").classList.toggle("hidden", !ebayStatus.configured);
  const connect = $("#ebay-connect");
  connect.classList.toggle("hidden", !ebayStatus.configured || ebayStatus.authorized);
  if (ebayStatus.configured && !ebayStatus.authorized) {
    try {
      const res = await api("/api/settings/ebay/consent-url");
      $("#ebay-consent-link").href = res.url;
    } catch (err) {
      st.textContent += ` (consent-url error: ${err.message})`;
    }
  }
}

$("#btn-advanced").addEventListener("click", () => {
  const panel = $("#advanced-panel");
  panel.classList.toggle("hidden");
  if (!panel.classList.contains("hidden")) {
    loadEbayStatus();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

$("#ebay-keys-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/settings/ebay", {
      method: "POST",
      body: JSON.stringify({
        client_id: f.get("client_id"),
        client_secret: f.get("client_secret"),
        ru_name: f.get("ru_name"),
        env: f.get("env"),
      }),
    });
    e.target.reset();
    await loadEbayStatus();
  } catch (err) {
    toast(`Could not save keys: ${err.message}`, "err");
  }
});

$("#ebay-remove").addEventListener("click", async () => {
  if (!confirm("Remove your eBay keys and disconnect? Existing eBay listings are unaffected.")) return;
  await api("/api/settings/ebay", { method: "DELETE" });
  await loadEbayStatus();
  loadSummary().catch(console.error);
});

$("#ebay-authorize").addEventListener("click", async () => {
  const pasted = $("#ebay-redirect").value.trim();
  if (!pasted) { toast("Open the consent link, approve, then paste the URL you land on.", "err"); return; }
  try {
    await api("/api/settings/ebay/authorize", {
      method: "POST",
      body: JSON.stringify({ redirect: pasted }),
    });
    $("#ebay-redirect").value = "";
    await loadEbayStatus();
    loadSummary().catch(console.error); // reveal the eBay buttons
  } catch (err) {
    toast(`Authorization failed: ${err.message}`, "err");
  }
});

async function listOnEbay(item, btn) {
  const price = item.price;
  if (!price) { toast("This card has no market price yet. Refresh prices first.", "err"); return; }
  const { ok } = await askConfirm({
    title: "List on your eBay account?",
    html: `<p>This publishes 1 live eBay listing: <strong>${esc(item.name)}</strong> at ${fmtUsd(price)}.</p>
           <p>${provenanceHtml(item.provenance)}</p>`,
    confirm: `Publish at ${fmtUsd(price)}`,
  });
  if (!ok) return;
  btn.disabled = true;
  try {
    const res = await postJson("/api/settings/ebay/list-item", { item_id: item.id, price });
    toast(res.listing && res.listing.url ? `Live on eBay: ${res.listing.url}` : `Published (status: ${res.status}).`);
  } catch (err) {
    toast(`eBay listing failed: ${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- account: email confirmation ---------------- */

async function loadAccount() {
  const acct = await api("/api/account");
  lib.account = acct;
  const banner = $("#verify-banner");
  banner.classList.toggle("hidden", acct.verified);
  if (!acct.verified) {
    $("#verify-text").textContent =
      (acct.mail === "smtp"
        ? ` We sent a link to ${acct.email}. Open it to sell, buy and get paid.`
        : ` This install has no mail server set up, so the link for ${acct.email} is printed in the app's console window. Open it to sell, buy and get paid.`)
      + (acct.admin_pending ? " Operator tools unlock once this address is confirmed." : "");
  }
}

$("#btn-resend").addEventListener("click", async () => {
  const btn = $("#btn-resend");
  btn.disabled = true;
  try {
    const res = await postJson("/api/account/resend-verification");
    toast(res.verified ? "This address is already confirmed."
      : res.via === "smtp" ? "Sent. Check your inbox."
      : "A new link was printed in the app's console window.");
    loadAccount().catch(() => {});
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- seller: automatic payouts ---------------- */

async function loadPayoutAccount() {
  const p = await api("/api/payouts/account");
  const line = $("#payout-line");
  line.classList.toggle("hidden", !p.available);
  if (!p.available) return;
  const label = { none: "Set up payouts", incomplete: "Continue payout setup" }[p.state];
  line.innerHTML = `<span><strong>Payouts${p.state === "active" ? " are on" : ""}.</strong> ${esc(p.next)}</span>
    ${label ? `<button class="btn btn-primary" id="btn-connect-payouts">${label}</button>` : ""}`;
  const btn = $("#btn-connect-payouts");
  if (btn) btn.addEventListener("click", async () => {
    const { ok } = await askConfirm({
      title: "Set up payouts with Stripe?",
      html: `<p>You'll go to Stripe to enter your details and bank account. Stripe holds them; this app never sees them.</p>
             <p class="muted">After that, your share of each card sale is sent to you automatically, including anything already owed.</p>`,
      confirm: "Continue to Stripe",
    });
    if (!ok) return;
    btn.disabled = true;
    try {
      const res = await postJson("/api/payouts/connect");
      window.location = res.url;
    } catch (err) {
      btn.disabled = false;
      toast(err.message, "err");
    }
  });
}

$("#btn-run-payouts").addEventListener("click", async () => {
  const btn = $("#btn-run-payouts");
  const { ok } = await askConfirm({
    title: "Pay out now?",
    html: `<p>Every seller who has finished payout setup is sent what they are owed from card sales, by Stripe transfer from your account.</p>
           <p class="muted">Sellers without payout setup are skipped. Nobody is paid twice.</p>`,
    confirm: "Send payouts",
  });
  if (!ok) return;
  btn.disabled = true;
  try {
    const r = await postJson("/api/admin/payouts/run");
    toast(r.errors.length
      ? `Sent ${fmtUsd(r.transferred)} for ${r.orders} order(s). ${r.errors.length} failed: ${r.errors[0]}`
      : r.orders ? `Sent ${fmtUsd(r.transferred)} across ${r.orders} order(s).`
      : "Nothing to send: nobody with payout setup is owed anything.", r.errors.length ? "err" : "ok");
    loadMarket().catch(console.error);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- operator: seller balances and payouts ---------------- */

let payoutSeller = null;

async function loadOperator() {
  const data = await api("/api/admin/balances");
  const due = $("#refunds-due");
  due.classList.toggle("hidden", !data.refunds_due.length);
  due.innerHTML = data.refunds_due.map((r) =>
    `<p class="flag-note">Refund ${fmtUsd(r.amount)} to ${esc(r.buyer)} by hand in the Stripe dashboard
     (order ${r.order_id}, session ${esc(r.payment_ref)}). The automatic refund was refused.</p>`).join("");
  $("#operator-body").innerHTML = data.sellers.map((r) => `<tr data-seller="${r.seller_id}">
      <td>${esc(r.email)}</td>
      <td class="num">${fmtUsd(r.card_proceeds)}</td>
      <td class="num">${fmtUsd(r.paid_out)}</td>
      <td class="num"><strong>${fmtUsd(r.balance)}</strong></td>
      <td class="num">${r.fees_owed ? fmtUsd(r.fees_owed) : "—"}</td>
      <td>${r.auto_payouts ? '<span class="pill ok">automatic</span>' : '<span class="pill">by hand</span>'}</td>
      <td><div class="row-actions">${r.balance > 0
        ? `<button class="btn" data-act="payout" data-email="${esc(r.email)}" data-balance="${r.balance}">Record payout</button>` : ""}</div></td>
    </tr>`).join("") || emptyRow(7, "No sales have been paid for yet.");
}

$("#operator-body").addEventListener("click", (e) => {
  const btn = e.target.closest('[data-act="payout"]');
  if (!btn) return;
  payoutSeller = Number(btn.closest("tr").dataset.seller);
  const form = $("#payout-form");
  form.reset();
  $("#payout-title").textContent = `Record a payout to ${btn.dataset.email}`;
  form.amount.value = btn.dataset.balance;
  form.amount.max = btn.dataset.balance;
  openDialog($("#payout-dialog"));
});

$("#payout-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  try {
    const res = await postJson("/api/admin/payouts", { ...f, seller_id: payoutSeller });
    $("#payout-dialog").close();
    toast(`Payout recorded. ${fmtUsd(res.balance)} still owed.`);
    loadMarket().catch(console.error);
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- coming back from a link: email, payout setup ---------------- */

(function linkOutcome() {
  const params = new URLSearchParams(location.search);
  const messages = {
    verify: {
      sent: ["Account created. Confirm your email with the link we sent to start selling and buying.", "ok"],
      done: ["Email confirmed. You can sell, buy and get paid.", "ok"],
      expired: ["That link has expired. Use “Send the link again” for a new one.", "err"],
      invalid: ["That link is not valid any more. Use “Send the link again” for a new one.", "err"],
      needed: ["Confirm your email address first.", "err"],
      wrong_account: ["That link belongs to a different account. Log in to that account, then open the link again.", "err"],
    },
    payouts: {
      active: ["Payouts are set up. Your share of card sales is sent to you automatically.", "ok"],
      incomplete: ["Stripe still needs a few details before it can pay you. Use “Continue payout setup”.", "err"],
      retry: ["That Stripe link expired. Use “Continue payout setup” for a new one.", "err"],
      unknown: ["Could not reach Stripe to check your payout setup. It is checked again when you reload.", "err"],
    },
  };
  for (const [key, table] of Object.entries(messages)) {
    const hit = table[params.get(key)];
    if (!hit) continue;
    if (!params.get("payment")) history.replaceState(null, "", location.pathname);
    setTimeout(() => toast(hit[0], hit[1]), 300);
  }
})();

/* ---------------- coming back from card checkout ---------------- */

(function paymentOutcome() {
  const params = new URLSearchParams(location.search);
  const outcome = params.get("payment");
  if (!outcome) return;
  const messages = {
    paid: ["Payment received. The card is in your library, under Purchased.", "ok"],
    pending: ["Stripe has not confirmed the payment yet. The order stays open; this page updates when it clears.", "ok"],
    cancelled: ["Checkout was cancelled. You were not charged.", "ok"],
    problem: ["The payment could not be matched to the order, so the card was not transferred. If you were charged for a card that was no longer available, it is refunded automatically; otherwise contact the seller.", "err"],
    unknown: ["Could not reach Stripe to confirm the payment. The order stays open and is checked again shortly.", "err"],
  };
  const [text, kind] = messages[outcome] || messages.unknown;
  history.replaceState(null, "", location.pathname);
  // After the first paint, so the status line is not lost in the page load.
  setTimeout(() => toast(text, kind), 300);
})();

/* ---------------- boot ---------------- */

function refreshAll() {
  loadEbayStatus()
    .catch(() => {})
    .then(() => loadSummary().then(loadMarket))
    .catch((err) => toast(err.message, "err"));
  loadAccount().catch(console.error);
  loadPayoutAccount().catch(console.error);
  loadLedger().catch(console.error);
  loadChart().catch(console.error);
  loadAlerts().catch(console.error);
  loadRules().catch(console.error);
}

refreshAll();
setInterval(() => {
  if (document.querySelector("dialog[open]")) return;  // don't redraw under an open form
  loadSummary().then(loadMarket).catch(() => {});
  loadLedger().catch(() => {});
  loadAlerts().catch(() => {});
}, 60_000);
