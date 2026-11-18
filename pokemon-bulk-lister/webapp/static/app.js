// Single-page UI logic for the bulk-lister. Vanilla JS, no framework.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  cards: [],
  stats: {},
  sort: "newest",
  needsReview: false,
  unidentified: false,
  editing: null,
  selected: new Set(),
  search: "",
  jobRunning: false,
  pricing: new Set(),      // card ids being priced right now
  priceTried: new Set(),   // ids this page already tried, so a failure is not retried in a loop
  libraryTried: new Set(), // ids this page already sent to the library
  libraryOff: false,       // not signed in, or the server refused: stop trying
};

// ---------------------------------------------------------------- network
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text();
    let message = `${res.status}: ${body}`;
    try { message = JSON.parse(body).error || message; } catch { /* not JSON */ }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

// ---------------------------------------------------------------- render
function renderStats(stats) {
  const total = stats.total || 0;
  const hadCards = document.body.classList.contains("has-cards");
  document.body.classList.toggle("has-cards", total > 0);
  document.body.classList.toggle("is-empty", total === 0);
  if (!hadCards && total) $("#upload-panel").open = false;
  if (!total) $("#upload-panel").open = true;
  $("#scan-title").textContent = total ? "Review cards" : "Scan cards";
  $("#scan-subtitle").classList.toggle("hidden", total > 0);
  $("#stats").innerHTML = stats.priced
    ? `<span>Estimated value <strong>${fmt(stats.total_value || 0)}</strong></span>` : "";
  const flagged = stats.flagged || 0;
  $("#review-count").textContent = flagged ? flagged : "";
  const review = $("#review-next-btn");
  review.textContent = `Review ${flagged} card${flagged === 1 ? "" : "s"}`;
  review.classList.toggle("hidden", !flagged);
  $("#process-btn").classList.toggle("hidden", !total || (stats.identified >= total && stats.priced >= total));
}

function reviewWhy(notes) {
  return (notes || '').replace(/^⚠\s*/, '').trim() || 'Check the card identity, finish and price before listing.';
}

function fmt(v) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return `$${v.toFixed(2)}`;
  return v;
}

function bestEbay(card) {
  // Prefer Terapeak's 365-day median when present (deeper signal),
  // otherwise fall back to the 30-day MI median.
  if (card.terapeak_median_usd != null && card.terapeak_median_usd > 0) {
    return { value: card.terapeak_median_usd, window: "last year", count: card.terapeak_sold_count_365d };
  }
  if (card.ebay_median_30d != null && card.ebay_median_30d > 0) {
    return { value: card.ebay_median_30d, window: "last 30 days", count: card.ebay_sold_count_30d };
  }
  return { value: null, window: null, count: 0 };
}

// What the final price rests on, and why a flagged row was flagged. A price
// with no visible basis is a bug.
function priceBasis(card) {
  const sources = [
    ["TCGplayer", card.tcgplayer_market], ["Cardmarket", card.cardmarket_trend_usd],
    ["eBay", bestEbay(card).value],
  ].filter(([, value]) => value != null && value > 0);
  const notes = reviewWhy(card.pricing_notes);
  let issue = "Check price";
  if (/Japanese/i.test(notes)) issue = "Japanese card · English price";
  else if (/finish.*(unknown|check|uncertain)|printing.*unknown/i.test(notes)) issue = "Check finish";
  else if (/number/i.test(notes)) issue = "Check card number";
  else if (/disagree|outlier/i.test(notes)) issue = "Prices disagree";
  else if (/identity|identif/i.test(notes)) issue = "Check card match";
  if (card.final_price == null && !card.pricing_notes) return '<span class="src">Not priced yet</span>';
  const basis = sources.length ? sources.map(([name]) => name).join(" · ") : "No market source";
  return `<span class="price-source-line">${escapeHtml(basis)}</span>
    ${card.needs_review ? `<span class="flag-note">${escapeHtml(issue)}</span>` : ""}
    <details class="price-details" data-detail="price"><summary>Price details</summary><div class="price-detail-body">
      ${sources.map(([name, value]) => `<span>${escapeHtml(name)} <strong>${fmt(value)}</strong></span>`).join("")}
      <span>${Math.round((card.pricing_confidence || 0) * 100)}% price confidence</span>
      ${card.pricing_notes || card.needs_review ? `<p>${escapeHtml(notes)}</p>` : ""}
    </div></details>`;
}

const onEbay = card => !!card.ebay_listing_id && card.ebay_listing_status === "listed";

// Cards live on eBay that were just changed: ask, then bring those listings
// up to date. A listing changes only after a yes.
async function offerEbayUpdate(ids) {
  const live = ids.filter(id => { const c = state.cards.find(x => x.id === id); return c && onEbay(c); });
  if (!live.length) return;
  const { ok } = await askConfirm({
    title: `Update ${plural(live.length, "eBay listing")}?`,
    html: `<p>${live.length === 1 ? "This card is" : `${live.length} of these cards are`} live on eBay.
      The ${live.length === 1 ? "listing takes" : "listings take"} the card's details, notes and price as they are now.
      Buyers see the change right away.</p>`,
    confirm: "Update on eBay",
  });
  if (!ok) return;
  let updated = 0;
  const failed = [];
  try {
    for (let i = 0; i < live.length; i += 25) {
      const res = await api("/api/publish/ebay/revise", { method: "POST", body: JSON.stringify({ ids: live.slice(i, i + 25) }) });
      updated += res.updated || 0;
      failed.push(...(res.failed || []));
    }
    $("#upload-status").textContent = `Updated ${plural(updated, "eBay listing")}`
      + (failed.length ? `. ${failed.length} failed: ${failed[0].error}` : ".");
  } catch (err) {
    $("#upload-status").textContent = `Could not update eBay: ${err.message}`;
  }
}

// What the listing will say, written from the card's details by the server.
async function showListingPreview(id) {
  const box = $("#modal-listing");
  box.textContent = "";
  try {
    const p = await api(`/api/cards/${id}/listing`);
    if (state.editing !== id) return;
    const who = { ebay: "Description written by eBay", ours: "Description written by AI" }[p.written_by]
      || { waiting: "AI description on its way", sent: "eBay is writing the description" }[p.ai_status] || "";
    box.innerHTML = `<strong>${escapeHtml(p.title)}</strong>${escapeHtml(p.description)}`
      + (who ? `<span class="src">${who}</span>` : "")
      + `<span class="src">${p.price == null ? "No price yet" : `Lists at ${fmt(p.price)}${p.price_is_yours ? " (your price)" : " (estimate)"}`}${p.on_ebay ? " · live on eBay" : ""}</span>`;
  } catch { /* the preview is a convenience */ }
}

// A photo's own storage address when the server gave one: loading it
// through this app costs a function call and a database read per photo.
function photoSrc(card) {
  return card.photo_url || `/${card.crop_path}`;
}

function renderRow(card) {
  const flagClass = card.needs_review ? "flag" : "";
  const setBits = [card.set_name, card.card_number ? `#${card.card_number}` : ""]
    .filter(Boolean)
    .join(" · ");
  const ebay = bestEbay(card);
  const ebayCell = ebay.value == null
    ? `<span class="muted">—</span>`
    : `${fmt(ebay.value)}<div class="muted small">${ebay.count || 0} sold, ${ebay.window}</div>`;
  const checked = state.selected.has(card.id) ? "checked" : "";
  return `
    <tr class="${flagClass}" data-id="${card.id}">
      <td class="select"><input type="checkbox" data-act="select" aria-label="Select ${escapeHtml(card.name || 'unidentified card')}" ${checked} /></td>
      <td class="thumb"><img src="${escapeHtml(photoSrc(card))}" alt="Photo of ${escapeHtml(card.name || 'unidentified card')}" loading="lazy" /></td>
      <td>
        <strong>${escapeHtml(card.name) || "<span class='muted'>No name yet</span>"}</strong>
        ${card.printing === "reverseHolofoil" ? "<span class='muted'> · reverse holo</span>" : card.is_holo ? "<span class='muted'> · holo</span>" : card.printing === "unknown" ? "<span class='flag-text'> · finish not checked</span>" : ""}
        ${card.sold ? "<span class='sold-tag' title='Sold. It is left out of publishing and exports.'>sold</span>" : ""}
        ${card.in_library ? "<span class='muted small' title='This card is in your library.'> · in library</span>" : ""}
        <div class="muted small">${escapeHtml(card.condition_guess || "")}</div>
      </td>
      <td class="muted small">${escapeHtml(setBits)}</td>
      <td class="price">${fmt(card.tcgplayer_market)}</td>
      <td class="price">${fmt(card.cardmarket_trend_usd)}</td>
      <td class="price">${ebayCell}</td>
      <td class="price">${state.pricing.has(card.id) ? '<span class="pricing-now">Pricing…</span>'
        : card.list_price != null
          ? `<strong>${fmt(card.list_price)}</strong><span class="src your-price">Your price</span><span class="src">Estimate ${fmt(card.final_price)}</span>`
          : `<strong>${fmt(card.final_price)}</strong>${priceBasis(card)}`}${onEbay(card) ? '<span class="src">Live on eBay</span>' : ""}</td>
      <td><span class="id-status ${card.id_confidence <= 0.6 ? 'review' : ''}">${!card.name ? 'Not identified' : card.id_confidence <= 0.6 ? 'Check ID' : card.id_confidence >= 0.97 ? 'Catalog match' : 'Check photo'}</span>
      </td>
      <td class="actions">
        ${card.name && card.id_confidence <= 0.6 ? '<button data-act="choose" class="primary">Choose card</button>' : ""}
        <button data-act="edit">${card.needs_review && card.id_confidence > 0.6 ? "Review" : "Edit"}</button>
        <details class="row-menu" data-detail="actions"><summary aria-label="More actions for ${escapeHtml(card.name || 'this card')}">More</summary><div>
          <button data-act="price">Refresh price</button>
          <button data-act="delete">Delete card</button>
        </div></details>
      </td>
    </tr>
  `;
}

function visibleCards() {
  const q = state.search.trim().toLocaleLowerCase();
  return state.cards.filter(c => !q || [c.name, c.set_name, c.set_code, c.card_number].some(v => String(v || '').toLocaleLowerCase().includes(q)));
}

// Rows are kept on the page and only the ones that changed are redrawn.
// Rebuilding the whole table makes the browser load every photo again, which
// shows as the photos flashing each time a name or price arrives.
const drawnRows = new Map();   // card id -> the row's HTML as last drawn

function renderCards() {
  const body = $("#cards-body");
  const openDetails = Array.from(body.querySelectorAll('tr[data-id] details[open]'))
    .map(el => [el.closest('tr').dataset.id, el.dataset.detail]);
  const cards = visibleCards();
  const visible = new Set(cards.map(c => c.id));
  for (const id of Array.from(state.selected)) {
    if (!visible.has(id)) state.selected.delete(id);
  }
  $("#visible-count").textContent = cards.length;
  if (!cards.length) {
    const filtered = state.search || state.needsReview || state.unidentified;
    body.innerHTML = `<tr><td colspan="10" class="muted empty-state"><strong>${filtered ? 'No cards match these filters' : 'Your cards will appear here'}</strong>${filtered ? '<button type="button" data-act="clear-filters">Clear filters</button>' : 'Add a photo to get started.'}</td></tr>`;
    drawnRows.clear();
    syncSelectAllCheckbox();
    updateBundleButton();
    return;
  }
  body.querySelectorAll("tr:not([data-id])").forEach(tr => tr.remove());     // the "no cards" row
  const onPage = new Map(Array.from(body.querySelectorAll("tr[data-id]"), tr => [Number(tr.dataset.id), tr]));
  const kept = new Set();
  let before = null;
  for (const card of cards) {
    const html = renderRow(card).trim();
    let tr = onPage.get(card.id);
    if (!tr || drawnRows.get(card.id) !== html) {
      const holder = document.createElement("tbody");
      holder.innerHTML = html;
      const fresh = holder.firstElementChild;
      if (tr) {
        // The same photo: keep the element already showing it.
        const shown = tr.querySelector("td.thumb img");
        const wanted = fresh.querySelector("td.thumb img");
        if (shown && wanted && shown.getAttribute("src") === wanted.getAttribute("src")) {
          shown.alt = wanted.alt;
          wanted.replaceWith(shown);
        }
        tr.replaceWith(fresh);
      }
      tr = fresh;
      drawnRows.set(card.id, html);
    }
    // Native clicks change the live checkbox property without changing its
    // cached HTML. Clearing selection must also update rows we retain.
    tr.querySelector('input[data-act="select"]').checked = state.selected.has(card.id);
    const place = before ? before.nextElementSibling : body.firstElementChild;
    if (tr !== place) body.insertBefore(tr, place);
    before = tr;
    kept.add(card.id);
  }
  for (const [id, tr] of onPage) {
    if (!kept.has(id)) { tr.remove(); drawnRows.delete(id); }
  }
  for (const [id, kind] of openDetails) {
    const detail = body.querySelector(`tr[data-id="${id}"] details[data-detail="${kind}"]`);
    if (detail) detail.open = true;
  }
  syncSelectAllCheckbox();
  updateBundleButton();
}

function syncSelectAllCheckbox() {
  const all = $("#select-all");
  if (!all) return;
  const cards = visibleCards();
  const total = cards.length;
  const sel = cards.filter(c => state.selected.has(c.id)).length;
  all.checked = total > 0 && sel === total;
  all.indeterminate = sel > 0 && sel < total;
}

function updateBundleButton() {
  const n = state.selected.size;
  $("#scan-selection").classList.toggle("hidden", n === 0);
  $("#selected-count").textContent = `${n} selected`;
  $("#bundle-btn").disabled = n < 2;
  $("#delete-btn").disabled = n < 1;
  const pool = n ? selectedCards() : state.cards;
  const ready = pool.filter(c => c.name && c.final_price != null && !c.in_library && !c.sold).length;
  const library = $("#library-btn");
  library.textContent = ready ? `Add ${ready} to library` : "Add to library";
  library.classList.toggle("hidden", !ready);
}

// Remove cards (and their photos) from this page, after a yes.
async function deleteCards(ids) {
  const n = ids.length;
  const { ok } = await askConfirm({
    title: `Delete ${n} card${n === 1 ? "" : "s"}?`,
    html: `<p>${n === 1 ? "This card and its photo are" : `These ${n} cards and their photos are`} removed from this page.
           Cards already added to your library stay there.</p>`,
    confirm: "Delete",
  });
  if (!ok) return;
  try {
    const res = await api("/api/cards/delete", { method: "POST", body: JSON.stringify({ ids }) });
    ids.forEach(id => state.selected.delete(id));
    $("#upload-status").textContent = `Deleted ${res.deleted} card${res.deleted === 1 ? "" : "s"}.`;
    await loadCards();
  } catch (err) {
    alert(err.message);
  }
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ---------------------------------------------------------------- data fetch
let cardsRequest = 0;
let cardsShown = "";
async function loadCards() {
  const request = ++cardsRequest;
  const params = new URLSearchParams({ sort: state.sort });
  if (state.needsReview) params.set("needs_review", "1");
  if (state.unidentified) params.set("unidentified", "1");
  const data = await api(`/api/cards?${params}`);
  if (request !== cardsRequest) return;
  state.cards = data.cards;
  state.stats = data.stats;
  // The table refreshes by itself, so it is only redrawn when something changed.
  const now = JSON.stringify([data.cards, data.stats]);
  if (now !== cardsShown) {
    cardsShown = now;
    renderStats(state.stats);
    renderCards();
  }
  priceWaitingCards();
  addPricedToLibrary();
}

// A scanned card goes into the library by itself once it has a name the
// catalog confirmed and a price. One that still says "Check ID" waits for a
// person: Choose settles it, or "Add to library" adds it as it is.
let libraryBusy = false;
async function addPricedToLibrary() {
  if (libraryBusy || state.libraryOff) return;
  const ready = state.cards.filter(c => c.name && c.final_price != null && c.id_confidence > 0.6
    && !c.in_library && !c.sold && !state.libraryTried.has(c.id));
  if (!ready.length) return;
  libraryBusy = true;
  try {
    let added = 0;
    for (let i = 0; i < ready.length; i += 50) {      // a few at a time keeps each request short
      const part = ready.slice(i, i + 50);
      part.forEach(c => state.libraryTried.add(c.id));
      const res = await api("/api/inventory/promote", {
        method: "POST", body: JSON.stringify({ card_ids: part.map(c => c.id), auto: true }) });
      added += res.promoted || 0;
      // Eligibility can change while the request is in flight. Use the server's
      // membership flags, including cards it held for a person to check.
      await loadCards();
    }
    if (added) {
      $("#upload-status").innerHTML = `Added ${plural(added, "priced card")} to your library. `
        + `<a href="/portfolio">Open library</a>`;
    }
  } catch (err) {
    if (err.status === 401 || err.status === 403) state.libraryOff = true;   // not signed in here
  } finally {
    libraryBusy = false;
    // Pricing can finish for another card while a batch is being added. Drain
    // that work now; libraryTried prevents retries from turning into polling.
    addPricedToLibrary();
  }
}

function redrawCards() {
  cardsShown = "";
  renderCards();
}

// A named card with no price and no pricing note has not been priced since
// it was named or changed (an edit clears both). Price those in the
// background, two at a time, and show each price as it arrives.
async function priceWaitingCards() {
  if (state.jobRunning) return;
  const waiting = state.cards.filter(c => c.name && c.final_price == null && !c.pricing_notes && !c.sold
    && !state.pricing.has(c.id) && !state.priceTried.has(c.id));
  if (!waiting.length) return;
  waiting.forEach(c => state.pricing.add(c.id));
  redrawCards();
  const work = async () => {
    for (let card = waiting.shift(); card; card = waiting.shift()) {
      state.priceTried.add(card.id);
      try {
        const updated = await api(`/api/cards/${card.id}/price`, { method: "POST" });
        const i = state.cards.findIndex(c => c.id === updated.id);
        if (i >= 0) state.cards[i] = { ...state.cards[i], ...updated };
      } catch { /* left unpriced; the Price button tries again */ }
      state.pricing.delete(card.id);
      redrawCards();
    }
  };
  await Promise.all([work(), work()]);
  loadCards().catch(() => {});
}

// The table is live without a timer. While work is under way the page is
// already talking to the server (a job's progress, each price as it lands)
// and the rows update from that. When nothing is under way nothing is asked,
// so an open tab costs nothing. Coming back to the tab asks once, to pick up
// anything changed elsewhere.
let lastLook = Date.now();
function startLiveRefresh() {
  const look = () => {
    const busy = document.querySelector(".modal:not(.hidden)") || state.pricing.size || state.jobRunning;
    if (document.hidden || busy || Date.now() - lastLook < 30000) return;
    lastLook = Date.now();
    loadCards().catch(() => {});
  };
  document.addEventListener("visibilitychange", look);
  window.addEventListener("focus", look);
}

// ---------------------------------------------------------------- upload
function setupDropzone() {
  const dz = $("#dropzone");
  const input = $("#file-input");
  const status = $("#upload-status");

  dz.addEventListener("click", () => input.click());
  ["dragenter", "dragover"].forEach(e => dz.addEventListener(e, ev => {
    ev.preventDefault(); dz.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach(e => dz.addEventListener(e, ev => {
    ev.preventDefault(); dz.classList.remove("drag");
  }));
  dz.addEventListener("drop", ev => uploadFiles(ev.dataTransfer.files));
  input.addEventListener("change", () => uploadFiles(input.files));

  async function sendPhotos(list, mode) {
    const fd = new FormData();
    for (const f of list) fd.append("file", f, f.name);
    if (mode) fd.append("mode", mode);
    const res = await fetch("/api/grids/upload", { method: "POST", body: fd });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  }

  async function uploadFiles(files) {
    if (!files || !files.length) return;
    const mode = scanMode();
    status.textContent = `Uploading ${files.length} photo(s)…`;
    try {
      let data;
      const limit = await uploadLimit();
      if (!limit) {
        data = await sendPhotos(Array.from(files), mode);
      } else {
        // A hosted install caps each request, so photos go one at a time,
        // shrunk first when they are too big.
        data = { grids: [], total_crops: 0, skipped: [], errors: [] };
        let n = 0;
        for (const original of files) {
          status.textContent = `Uploading photo ${++n} of ${files.length}…`;
          const photo = await fitPhoto(original, limit);
          if (!photo) { data.skipped.push(original.name); continue; }
          const one = await sendPhotos([photo], mode);
          data.grids.push(...one.grids);
          data.total_crops += one.total_crops;
          data.skipped.push(...(one.skipped || []));
          data.errors.push(...(one.errors || []));
        }
      }
      const skipped = (data.skipped || []).length
        ? ` Skipped ${data.skipped.length} unreadable or unsupported file(s): ${data.skipped.join(', ')}. Try JPEG, PNG or a smaller photo.` : "";
      status.textContent = data.total_crops
        ? `Added ${data.total_crops} card(s) from ${data.grids.length} photo(s).${skipped}`
        : `No cards found in ${data.grids.length} photo(s).${skipped}`;
      await loadCards();
      if (data.total_crops) {
        $("#upload-panel").open = false;
        $("#cards-heading").focus({ preventScroll: true });
      }
      if (data.total_crops && $("#auto-process").checked) startProcess();
    } catch (err) {
      status.textContent = `Upload failed: ${err.message}`;
    } finally {
      input.value = "";
      camera.value = "";
    }
  }

  const camera = $("#camera-input");
  camera.addEventListener("change", () => uploadFiles(camera.files));
  $("#camera-btn").addEventListener("click", () => camera.click());
  dz.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); input.click(); }
  });

  // Remember the two scan settings between visits.
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("scan-settings") || "{}") || {}; } catch { /* Storage may be blocked or contain an old setting. */ }
  if (saved.auto === false) $("#auto-process").checked = false;
  const remember = () => {
    try { localStorage.setItem("scan-settings", JSON.stringify({ auto: $("#auto-process").checked })); } catch { /* Optional preference. */ }
  };
  $("#auto-process").addEventListener("change", remember);
  remember();
}

// Photos picked from Google Drive: the server fetches each one itself.
document.addEventListener("DOMContentLoaded", () => {
  const btn = $("#drive-photos-btn");
  if (!btn) return;
  const status = $("#upload-status");
  btn.addEventListener("click", () => openDrivePicker({
    kind: "photos", multiple: true, title: "Choose card photos from Google Drive",
    onPick: async (files) => {
      const mode = scanMode();
      let crops = 0, done = 0;
      const skipped = [];
      for (const file of files) {
        status.textContent = `Importing photo ${++done} of ${files.length} from Google Drive…`;
        try {
          const one = await api("/api/drive/import/photo", {
            method: "POST", body: JSON.stringify({ file_id: file.id, mode }) });
          crops += one.total_crops;
          skipped.push(...(one.skipped || []));
        } catch (err) {
          skipped.push(`${file.name} (${err.message})`);
        }
      }
      status.textContent = `Added ${crops} card(s) from ${files.length - skipped.length} Drive photo(s).`
        + (skipped.length ? ` Skipped: ${skipped.join(", ")}.` : "");
      await loadCards();
      if (crops) $("#upload-panel").open = false;
      if (crops && $("#auto-process").checked) startProcess();
    },
  }));
  const back = new URLSearchParams(location.search).get("drive");
  if (back) {
    history.replaceState(null, "", location.pathname);
    status.textContent = {
      connected: "Google Drive connected. Tap Google Drive to pick photos.",
      declined: "Google Drive was not connected: access was declined.",
      failed: "Google Drive could not be connected. Try again.",
      "not-set-up": "Google Drive is not set up on this install (GOOGLE_CLIENT_ID is missing).",
    }[back] || "";
  }
});

// What the server will accept in one request, or null for no cap.
let uploadLimitCache;
async function uploadLimit() {
  if (uploadLimitCache === undefined) {
    uploadLimitCache = (await api("/api/session").catch(() => ({}))).max_upload_bytes || null;
  }
  return uploadLimitCache;
}

// A photo small enough to send: the file itself, or a re-encoded JPEG scaled
// down until it fits. Returns null for a file the browser cannot read (TIFF).
async function fitPhoto(file, limit) {
  if (file.size <= limit) return file;
  let bitmap;
  try { bitmap = await createImageBitmap(file); } catch { return null; }
  const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
  for (const [side, quality] of [[4000, 0.9], [3400, 0.88], [2800, 0.85], [2200, 0.82]]) {
    const scale = Math.min(1, side / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", quality));
    if (blob && blob.size <= limit) return new File([blob], name, { type: "image/jpeg" });
  }
  return null;
}

// A CSV as pieces small enough to send, each starting with the header line.
function csvParts(text, maxBytes = 3000000) {
  // Requests carry JSON: count UTF-8 bytes including escaped quotes/newlines.
  // A newline inside a quoted cell is not a record boundary.
  const encoder = new TextEncoder();
  const bytes = s => encoder.encode(JSON.stringify(s)).length - 2;
  if (bytes(text) <= maxBytes) return [text];
  const records = [];
  let quoted = false, start = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '"') {
      if (quoted && text[i + 1] === '"') i++;
      else quoted = !quoted;
    } else if (text[i] === '\n' && !quoted) {
      records.push(text.slice(start, i + 1));
      start = i + 1;
    }
  }
  if (quoted) throw new Error('A CSV cell has an unclosed quote. Export the file again.');
  if (start < text.length) records.push(text.slice(start));
  const header = records.shift() || '';
  const headerBytes = bytes(header);
  let part = header, used = headerBytes;
  const parts = [];
  for (const row of records) {
    const rowBytes = bytes(row);
    if (headerBytes + rowBytes > maxBytes) throw new Error('A CSV row is too large to upload. Shorten its notes or description.');
    if (used + rowBytes > maxBytes) {
      parts.push(part);
      part = header; used = headerBytes;
    }
    part += row; used += rowBytes;
  }
  if (part !== header || !parts.length) parts.push(part);
  return parts;
}

// The server works out what is in each photo (one card, several, a binder
// page), so no mode is sent.
function scanMode() {
  return null;
}

const JOB_BUTTONS = ["#process-btn", "#identify-btn", "#run-pricing-btn"];

async function startProcess() {
  JOB_BUTTONS.forEach(s => { $(s).disabled = true; });
  try {
    await api("/api/scan/process", { method: "POST" });
    pollJob();
  } catch (err) {
    JOB_BUTTONS.forEach(s => { $(s).disabled = false; });
    $("#upload-status").textContent = `Could not start: ${err.message}`;
  }
}

// ---------------------------------------------------------------- confirm
// Resolves {ok, checked}. Used before publishing or moving cards off the bench.
function askConfirm({ title, html, confirm = "Confirm", checkbox = null }) {
  const previousFocus = document.activeElement;
  $("#confirm-title").textContent = title;
  $("#confirm-text").innerHTML = html;
  $("#confirm-ok").textContent = confirm;
  $("#confirm-check").checked = false;
  $("#confirm-check-row").classList.toggle("hidden", !checkbox);
  $("#confirm-check-label").textContent = checkbox || "";
  const modal = $("#confirm-modal");
  modal.classList.remove("hidden");
  $("#confirm-cancel").focus();
  return new Promise(resolve => {
    const done = ok => {
      modal.classList.add("hidden");
      modal.removeEventListener("keydown", onKey);
      previousFocus?.focus();
      resolve({ ok, checked: $("#confirm-check").checked });
    };
    const onKey = ev => { if (ev.key === "Escape") { ev.stopPropagation(); done(false); } };
    modal.addEventListener("keydown", onKey);
    $("#confirm-ok").onclick = () => done(true);
    $("#confirm-cancel").onclick = () => done(false);
    $("#confirm-x").onclick = () => done(false);
  });
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// ---------------------------------------------------------------- library
async function addToLibrary() {
  const session = await api("/api/session");
  if (!session.logged_in) {
    window.location = "/login?next=/";
    return;
  }
  const picked = selectedCards();
  const pool = (picked.length ? picked : state.cards).filter(c => !c.in_library && !c.sold);
  const ready = pool.filter(c => c.name && c.final_price != null);
  const flagged = ready.filter(c => c.needs_review).length;
  const left = pool.length - ready.length;
  if (!ready.length) {
    $("#upload-status").textContent = picked.length
      ? "The selected cards still need a name and price."
      : "Identify and price your cards before adding them to the library.";
    return;
  }
  const value = ready.reduce((sum, c) => sum + c.final_price, 0);
  const { ok } = await askConfirm({
    title: `Add ${plural(ready.length, "card")} to your library?`,
    html: `<p>${plural(ready.length, "priced card")}${picked.length ? " from your selection" : ""}, worth
      $${value.toFixed(2)} at current prices, will be added to your library. Cards already added are skipped.</p>
      ${left ? `<p class="muted">${plural(left, "card")} still need a name or a price. They stay on this page.</p>` : ""}
      ${flagged ? `<p class="flag-text">${plural(flagged, "card")} ${flagged === 1 ? "needs" : "need"} a price check. ${flagged === 1 ? "It is" : "They are"} added too, and stay marked in the library.</p>` : ""}`,
    confirm: "Add to library",
  });
  if (!ok) return;
  // Send exactly the cards the dialog counted, never "everything priced":
  // with a filter on, the table shows only part of the bench.
  const body = { card_ids: ready.map(c => c.id) };
  const res = await api("/api/inventory/promote", { method: "POST", body: JSON.stringify(body) });
  await loadCards();
  $("#upload-status").innerHTML =
    `Added ${plural(res.promoted, "card")} to your library` +
    (res.skipped_duplicates ? ` (${res.skipped_duplicates} were already there)` : "") +
    `. <a href="/portfolio">Open library</a> to sell them or check them against decks.`;
}

// ---------------------------------------------------------------- table interactions
function setupTable() {
  $("#select-visible-btn").addEventListener("click", () => {
    const cards = visibleCards();
    const clear = cards.length && cards.every(c => state.selected.has(c.id));
    cards.forEach(c => clear ? state.selected.delete(c.id) : state.selected.add(c.id));
    renderCards();
  });
  $("#cards-body").addEventListener("click", async ev => {
    const cb = ev.target.closest("input[type=checkbox][data-act=select]");
    if (cb) {
      const tr = ev.target.closest("tr");
      const id = parseInt(tr.dataset.id, 10);
      if (cb.checked) state.selected.add(id);
      else state.selected.delete(id);
      syncSelectAllCheckbox();
      updateBundleButton();
      return;
    }
    const btn = ev.target.closest("button");
    if (!btn) return;
    if (btn.dataset.act === "clear-filters") { clearScanFilters(); return; }
    const tr = ev.target.closest("tr");
    const id = parseInt(tr.dataset.id, 10);
    if (btn.dataset.act === "delete") { await deleteCards([id]); return; }
    if (btn.dataset.act === "choose") { chooseCard(id); return; }
    if (btn.dataset.act === "edit") openEdit(id);
    else if (btn.dataset.act === "price") {
      btn.disabled = true; btn.textContent = "…";
      try {
        state.priceTried.add(id);
        await api(`/api/cards/${id}/price`, { method: "POST" });
        await loadCards();
      } catch (err) {
        alert(err.message);
      } finally {
        btn.disabled = false; btn.textContent = "Refresh price";
      }
    }
  });

  $("#select-all").addEventListener("change", ev => {
    if (ev.target.checked) visibleCards().forEach(c => state.selected.add(c.id));
    else visibleCards().forEach(c => state.selected.delete(c.id));
    renderCards();
  });
}

// ---------------------------------------------------------------- modals
let editorOpener;
function openEdit(id) {
  const card = state.cards.find(c => c.id === id);
  if (!card) return;
  state.editing = id;
  editorOpener = document.activeElement;
  $("#modal-title").textContent = `Edit · ${card.name || "(unidentified)"}`;
  $("#modal-img").src = photoSrc(card);
  const form = $("#edit-form");
  form.id.value = card.id;
  form.name.value = card.name || "";
  form.set_name.value = card.set_name || "";
  form.set_code.value = card.set_code || "";
  form.card_number.value = card.card_number || "";
  form.rarity.value = card.rarity || "";
  form.condition_guess.value = card.condition_guess || "";
  form.is_holo.checked = !!card.is_holo;
  form.printing.value = card.printing || (card.is_holo ? "holofoil" : "normal");
  form.id_confidence.value = card.id_confidence ?? 0;
  form.list_price.value = card.list_price ?? "";
  form.listing_notes.value = card.listing_notes || "";
  showListingPreview(id);
  $("#modal-confidence").textContent = `${Math.round((card.id_confidence || 0) * 100)}% identification confidence`;

  $("#modal-prices").innerHTML = `
    <div>TCGplayer<strong>${fmt(card.tcgplayer_market)}</strong></div>
    <div>Cardmarket<strong>${fmt(card.cardmarket_trend_usd)}</strong></div>
    <div>eBay sold<strong>${fmt(card.ebay_median_30d)}</strong></div>
    <div>Price estimate<strong>${fmt(card.final_price)}</strong></div>
    <div class="price-note">${card.needs_review ? escapeHtml(reviewWhy(card.pricing_notes)) : ""}</div>
  `;
  $("#modal").classList.remove("hidden");
  form.elements.namedItem("name").focus();
}

function closeEdit() {
  const wasOpen = !$("#modal").classList.contains("hidden");
  $("#modal").classList.add("hidden"); state.editing = null;
  if (wasOpen) editorOpener?.focus();
}

async function saveEdit({ priceAfter = false } = {}) {
  if (!state.editing) return;
  const form = $("#edit-form");
  const saveBtn = form.querySelector('button[type="submit"]');
  const priceBtn = $("#modal-price-btn");
  const activeBtn = priceAfter ? priceBtn : saveBtn;
  const originalLabel = activeBtn.textContent;
  const allBtns = [saveBtn, priceBtn, $("#modal-upload-btn"), $("#modal-close")];
  allBtns.forEach(b => b.disabled = true);
  activeBtn.textContent = priceAfter ? "Saving…" : "Saving…";

  const patch = {
    name: form.name.value.trim(),
    set_name: form.set_name.value.trim(),
    set_code: form.set_code.value.trim(),
    card_number: form.card_number.value.trim(),
    rarity: form.rarity.value.trim(),
    condition_guess: form.condition_guess.value,
    is_holo: form.printing.value === "holofoil",
    printing: form.printing.value,
    id_confidence: parseFloat(form.id_confidence.value) || 0,
    list_price: form.list_price.value.trim(),
    listing_notes: form.listing_notes.value,
  };
  const edited = state.editing;
  try {
    await api(`/api/cards/${state.editing}`, { method: "PATCH", body: JSON.stringify(patch) });
    state.libraryTried.delete(state.editing);
    if (priceAfter) {
      activeBtn.textContent = "Pricing…";
      const updated = await api(`/api/cards/${state.editing}/price`, { method: "POST" });
      // Refresh the modal body with the new prices before closing.
      const idx = state.cards.findIndex(c => c.id === updated.id);
      if (idx >= 0) state.cards[idx] = updated;
      activeBtn.textContent = `Priced $${(updated.final_price ?? 0).toFixed(2)}`;
      await new Promise(r => setTimeout(r, 600));   // brief flash so user sees the new price
    } else {
      activeBtn.textContent = "Saved ✓";
      await new Promise(r => setTimeout(r, 350));
      state.priceTried.delete(state.editing);     // a changed card is priced again by itself
    }
    closeEdit();
    await loadCards();
    await offerEbayUpdate([edited]);
  } catch (err) {
    activeBtn.textContent = originalLabel;
    alert(`Failed: ${err.message}`);
  } finally {
    allBtns.forEach(b => b.disabled = false);
    activeBtn.textContent = originalLabel;
  }
}

async function aiIdentify() {
  if (!state.editing) return;
  const btn = $("#modal-identify-btn");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "Identifying…";
  try {
    // A card that already has a name is being *re*-identified — the user
    // disagrees with the current ID, so skip the free image-match layers
    // (which would just return the same answer) and force a fresh AI look.
    const current = state.cards.find(c => c.id === state.editing);
    const forceAi = !!(current && (current.name || "").trim());
    const updated = await api(`/api/cards/${state.editing}/identify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force_ai: forceAi }),
    });
    state.libraryTried.delete(state.editing);
    const form = $("#edit-form");
    form.name.value = updated.name || "";
    form.set_name.value = updated.set_name || "";
    form.set_code.value = updated.set_code || "";
    form.card_number.value = updated.card_number || "";
    form.rarity.value = updated.rarity || "";
    form.condition_guess.value = updated.condition_guess || "";
    form.is_holo.checked = !!updated.is_holo;
    form.printing.value = updated.printing || (updated.is_holo ? "holofoil" : "normal");
    form.id_confidence.value = updated.id_confidence ?? 0;
    $("#modal-confidence").textContent = `${Math.round((updated.id_confidence || 0) * 100)}% identification confidence`;
    const idx = state.cards.findIndex(c => c.id === updated.id);
    if (idx >= 0) state.cards[idx] = updated;
    $("#modal-title").textContent = `Edit · ${updated.name || "(unidentified)"}`;
    btn.textContent = "Identified ✓";
    await new Promise(r => setTimeout(r, 400));
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

async function uploadCloudinary() {
  if (!state.editing) return;
  const btn = $("#modal-upload-btn");
  btn.disabled = true; const orig = btn.textContent; btn.textContent = "Uploading…";
  try {
    await api(`/api/cards/${state.editing}/upload-image`, { method: "POST" });
    await loadCards();
    btn.textContent = "Uploaded ✓";
  } catch (err) {
    btn.textContent = orig;
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- which card is this
// For a card the catalog could not settle: the real cards it could be, or
// "something else" (type it in, or read the photo again).
let choosing = null;
let chooseRequest = 0;
let chooserOpener;
function closeChoose() {
  $("#choose-modal").classList.add("hidden");
  choosing = null;
  chooserOpener?.focus();
}

function drawChoices(data) {
  const cur = data.current || {};
  const read = data.read || {};
  const seen = [cur.name, cur.set_name, cur.card_number ? `#${cur.card_number}${read.number_total ? "/" + read.number_total : ""}` : ""]
    .filter(Boolean).join(" · ");
  $("#choose-read").textContent = seen ? `Read as: ${seen}` : "Nothing could be read off this card.";
  choosing.choices = data.choices || [];
  const rows = choosing.choices.map((c, i) => `
    <label class="choose-opt"><input type="radio" name="choose-pick" value="${i}" />
      <span><strong>${escapeHtml(c.name)}</strong>
        <span class="src">${escapeHtml([c.set_name, c.card_number ? "#" + c.card_number : "", c.rarity].filter(Boolean).join(" · "))}</span>
        <span class="src">${escapeHtml(c.why || "")}</span></span></label>`).join("");
  $("#choose-list").innerHTML = rows + `
    <label class="choose-opt"><input type="radio" name="choose-pick" value="other" ${rows ? "" : "checked"} />
      <span><strong>Something else</strong><span class="src">${rows ? "None of these is the card." : "The catalog has no card that fits what was read."}</span></span></label>`;
  $("#choose-status").textContent = "";
  syncChoose();
}

function syncChoose() {
  const picked = document.querySelector('input[name="choose-pick"]:checked');
  $("#choose-other").classList.toggle("hidden", !picked || picked.value !== "other");
  $("#choose-ok").disabled = !picked || picked.value === "other";
}

async function chooseCard(id) {
  const card = state.cards.find(c => c.id === id);
  if (!card) return;
  chooserOpener = document.activeElement;
  const request = ++chooseRequest;
  choosing = { id, choices: [], request };
  $("#choose-img").src = photoSrc(card);
  $("#choose-img").alt = `Photo of ${card.name || "the card"}`;
  $("#choose-read").textContent = "";
  $("#choose-list").innerHTML = "";
  $("#choose-other").classList.add("hidden");
  $("#choose-retry").disabled = false;
  $("#choose-ok").disabled = true;
  $("#choose-status").textContent = "Looking it up…";
  $("#choose-modal").classList.remove("hidden");
  $("#choose-cancel").focus();
  try {
    const data = await api(`/api/cards/${id}/choices`);
    if (choosing?.request === request) drawChoices(data);
  } catch (err) {
    if (choosing?.request !== request) return;
    drawChoices({ choices: [], current: card });
    $("#choose-status").textContent = `Could not look it up: ${err.message}`;
  }
}

// ---------------------------------------------------------------- one price for many
function closeListing() { $("#listing-modal").classList.add("hidden"); }

function openListing() {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  const form = $("#listing-form");
  form.reset();
  $("#listing-title").textContent = `Set price for ${plural(ids.length, "card")}`;
  $("#listing-what").textContent = "Each card lists at this price in place of its estimate, on eBay and in exports.";
  $("#listing-status").textContent = "";
  $("#listing-modal").classList.remove("hidden");
  form.list_price.focus();
}

function setupListing() {
  const form = $("#listing-form");
  $("#listing-btn").addEventListener("click", openListing);
  $("#listing-x").addEventListener("click", closeListing);
  $("#listing-cancel").addEventListener("click", closeListing);
  $("#listing-modal").addEventListener("click", ev => { if (ev.target.id === "listing-modal") closeListing(); });
  form.clear_price.addEventListener("change", () => { form.list_price.disabled = form.clear_price.checked; });
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const ids = Array.from(state.selected);
    const body = { ids };
    if (form.clear_price.checked) body.list_price = null;
    else if (form.list_price.value.trim()) body.list_price = form.list_price.value.trim();
    if (form.listing_notes.value.trim()) body.listing_notes = form.listing_notes.value;
    if (!("list_price" in body) && !("listing_notes" in body)) {
      $("#listing-status").textContent = "Enter a price or notes first.";
      return;
    }
    try {
      const res = await api("/api/cards/listing", { method: "POST", body: JSON.stringify(body) });
      closeListing();
      $("#upload-status").textContent = `Updated ${plural(res.changed, "card")}.`;
      await loadCards();
      await offerEbayUpdate(ids);
    } catch (err) {
      $("#listing-status").textContent = err.message;
    }
  });
}

function setupChoose() {
  $("#choose-x").addEventListener("click", closeChoose);
  $("#choose-cancel").addEventListener("click", closeChoose);
  $("#choose-modal").addEventListener("click", ev => { if (ev.target.id === "choose-modal") closeChoose(); });
  $("#choose-list").addEventListener("change", syncChoose);
  $("#choose-manual").addEventListener("click", () => {
    const id = choosing?.id;
    closeChoose();
    if (id) openEdit(id);
  });
  $("#choose-retry").addEventListener("click", async () => {
    if (!choosing) return;
    const { id, request } = choosing;
    const btn = $("#choose-retry");
    btn.disabled = true;
    $("#choose-status").textContent = "Reading the photo again…";
    try {
      const updated = await api(`/api/cards/${id}/identify`, { method: "POST", body: JSON.stringify({ force_ai: true }) });
      state.libraryTried.delete(id);
      const i = state.cards.findIndex(c => c.id === id);
      if (i >= 0) state.cards[i] = { ...state.cards[i], ...updated };
      redrawCards();
      if (choosing?.request !== request) { loadCards().catch(() => {}); return; }
      if (updated.id_confidence > 0.6) {          // the catalog confirmed the new reading
        closeChoose();
        state.priceTried.delete(id);
        await loadCards();
        return;
      }
      const data = await api(`/api/cards/${id}/choices`);
      if (choosing?.request === request) drawChoices(data);
    } catch (err) {
      if (choosing?.request === request) $("#choose-status").textContent = `Could not read it: ${err.message}`;
    } finally {
      if (choosing?.request === request) btn.disabled = false;
    }
  });
  $("#choose-ok").addEventListener("click", async () => {
    const picked = document.querySelector('input[name="choose-pick"]:checked');
    const card = choosing && picked ? choosing.choices[Number(picked.value)] : null;
    if (!card) return;
    const { id, request } = choosing;
    $("#choose-ok").disabled = true;
    try {
      const patch = { name: card.name, set_name: card.set_name || "", set_code: card.set_code || "",
                      card_number: card.card_number || "", id_confidence: 0.97, needs_review: false };
      if (card.rarity) patch.rarity = card.rarity;
      await api(`/api/cards/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
      state.libraryTried.delete(id);
      if (choosing?.request === request) closeChoose();
      state.priceTried.delete(id);
      await loadCards();                          // the new card is priced by itself
    } catch (err) {
      if (choosing?.request !== request) return;
      $("#choose-status").textContent = `Could not save: ${err.message}`;
      $("#choose-ok").disabled = false;
    }
  });
}

function setupModal() {
  $("#modal-close").addEventListener("click", closeEdit);
  $("#modal").addEventListener("click", ev => {
    if (ev.target.id === "modal") closeEdit();
  });
  $("#edit-form").addEventListener("submit", ev => { ev.preventDefault(); saveEdit(); });
  $("#modal-price-btn").addEventListener("click", () => saveEdit({ priceAfter: true }));
  $("#modal-identify-btn").addEventListener("click", aiIdentify);
  $("#modal-upload-btn").addEventListener("click", uploadCloudinary);
}

// ---------------------------------------------------------------- bundle modal
function selectedCards() {
  return state.cards.filter(c => state.selected.has(c.id));
}

function bundleSum() {
  return selectedCards().reduce((acc, c) => acc + (c.final_price || 0), 0);
}

function bundlePricePreview() {
  const form = $("#bundle-form");
  if (!form) return 0;
  const discount = form.discount.value;
  const sum = bundleSum();
  if (discount === "custom") {
    return parseFloat(form.price.value) || 0;
  }
  return Math.round(sum * (1 - parseFloat(discount)) * 100) / 100;
}

function refreshBundlePreview() {
  $("#bundle-count").textContent = state.selected.size;
  $("#bundle-sum").textContent = `$${bundleSum().toFixed(2)}`;
  $("#bundle-price-preview").textContent = `$${bundlePricePreview().toFixed(2)}`;
}

function openBundle() {
  if (state.selected.size < 2) return;
  const form = $("#bundle-form");
  form.reset();
  form.discount.value = "0.30";
  form.quantity.value = "1";
  $("#bundle-price-row").classList.add("hidden");
  $("#bundle-status").textContent = "";
  refreshBundlePreview();
  $("#bundle-modal").classList.remove("hidden");
}

function closeBundle() {
  $("#bundle-modal").classList.add("hidden");
}

function setupBundleModal() {
  $("#bundle-close").addEventListener("click", closeBundle);
  $("#bundle-modal").addEventListener("click", ev => {
    if (ev.target.id === "bundle-modal") closeBundle();
  });
  const form = $("#bundle-form");
  form.discount.addEventListener("change", () => {
    const custom = form.discount.value === "custom";
    $("#bundle-price-row").classList.toggle("hidden", !custom);
    if (custom && !form.price.value) {
      form.price.value = (bundleSum() * 0.7).toFixed(2);
    }
    refreshBundlePreview();
  });
  form.price.addEventListener("input", refreshBundlePreview);

  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const ids = Array.from(state.selected);
    if (ids.length < 2) {
      $("#bundle-status").textContent = "Select at least 2 cards.";
      return;
    }
    const payload = {
      card_ids: ids,
      quantity: parseInt(form.quantity.value, 10) || 1,
    };
    const title = form.title.value.trim();
    if (title) payload.title = title;
    const note = form.note.value.trim();
    if (note) payload.note = note;
    const slug = form.slug.value.trim();
    if (slug) payload.slug = slug;
    if (form.discount.value === "custom") {
      payload.price = parseFloat(form.price.value) || 0;
    } else {
      payload.discount = parseFloat(form.discount.value);
    }

    const submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    const origLabel = submitBtn.textContent;
    submitBtn.textContent = "Exporting…";
    $("#bundle-status").textContent = "";
    try {
      const data = await api("/api/export/lot", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const links = data.written.map(w => `<a href="/${w.file}" target="_blank">${w.file}</a>`).join(" · ");
      const s = data.summary || {};
      $("#bundle-status").innerHTML =
        `Wrote ${data.written.length} lot CSV(s) — bundle price $${(s.bundle_price ?? 0).toFixed(2)} (sum was $${(s.sum_of_final_prices ?? 0).toFixed(2)}). ${links}`;
      $("#upload-status").innerHTML = `Lot exported: ${links}`;
    } catch (err) {
      $("#bundle-status").textContent = `Failed: ${err.message}`;
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = origLabel;
    }
  });
}

// ---------------------------------------------------------------- toolbar
function clearScanFilters() {
  state.search = ""; state.needsReview = false; state.unidentified = false;
  $("#card-search").value = "";
  $("#filter-review").checked = false;
  $("#filter-unid").checked = false;
  cardsShown = "";
  loadCards();
}
function setupToolbar() {
  $("#clear-selection-btn").addEventListener("click", () => { state.selected.clear(); renderCards(); });
  $("#review-next-btn").addEventListener("click", async () => {
    state.needsReview = true; state.unidentified = false; state.search = "";
    $("#filter-review").checked = true; $("#filter-unid").checked = false; $("#card-search").value = "";
    cardsShown = "";
    await loadCards();
    $("#cards-heading").focus();
  });
  $("#card-search").addEventListener("input", e => { state.search = e.target.value; renderCards(); });
  $("#show-comparisons").addEventListener("change", e => {
    $("#cards-table").classList.toggle("compact-prices", !e.target.checked);
  });
  $("#sort").addEventListener("change", e => { state.sort = e.target.value; loadCards(); });
  $("#filter-review").addEventListener("change", e => { state.needsReview = e.target.checked; loadCards(); });
  $("#filter-unid").addEventListener("change", e => { state.unidentified = e.target.checked; loadCards(); });

  $("#process-btn").addEventListener("click", startProcess);

  $("#library-btn").addEventListener("click", async () => {
    const btn = $("#library-btn");
    btn.disabled = true;
    try {
      await addToLibrary();
    } catch (err) {
      if (err.status === 401) window.location = "/login?next=/";
      else $("#upload-status").textContent = `Could not add to library: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $("#identify-btn").addEventListener("click", async () => {
    JOB_BUTTONS.forEach(s => { $(s).disabled = true; });
    try {
      await api("/api/identify/run-all", { method: "POST" });
      pollJob();
    } catch (err) {
      alert(err.message);
      JOB_BUTTONS.forEach(s => { $(s).disabled = false; });
    }
  });

  $("#run-pricing-btn").addEventListener("click", async () => {
    const useTerapeak = $("#use-terapeak").checked ? "1" : "0";
    const n = state.stats.identified ?? 0;  // whole bench, not the filtered view
    const { ok } = await askConfirm({
      title: `Re-price ${plural(n, "card")}?`,
      html: `<p>This looks up every identified card again, including the ones already priced, and
        replaces their prices.${useTerapeak === "1" ? " A year of eBay sales adds about 5 seconds per card." : ""}</p>
        <p class="muted">Use “Identify &amp; price” for new cards only.</p>`,
      confirm: "Re-price all",
    });
    if (!ok) return;
    JOB_BUTTONS.forEach(s => { $(s).disabled = true; });
    try {
      await api(`/api/pricing/run-all?terapeak=${useTerapeak}`, { method: "POST" });
      pollJob();
    } catch (err) {
      alert(err.message);
      JOB_BUTTONS.forEach(s => { $(s).disabled = false; });
    }
  });

  // One-shot probe of Terapeak login state on page load (operator only;
  // refused quietly for other accounts, whose checkbox is hidden).
  api("/api/terapeak/status").then(s => {
    const el = $("#terapeak-status");
    if (s.logged_in) {
      el.textContent = "signed in";
      el.style.color = "var(--up)";
    } else {
      el.textContent = "you’ll sign in once";
    }
  }).catch(() => {});

  $("#bundle-btn").addEventListener("click", openBundle);
  $("#delete-btn").addEventListener("click", () => deleteCards(Array.from(state.selected)));

  // Check every card against the real catalog: no AI call, a few seconds a go.
  $("#verify-btn").addEventListener("click", async () => {
    const btn = $("#verify-btn");
    const status = $("#upload-status");
    btn.disabled = true;
    const sum = { verified: 0, probable: 0, unverified: 0, unknown: 0 };
    try {
      let after = 0;
      for (;;) {
        const r = await api("/api/identify/verify", { method: "POST", body: JSON.stringify({ after }) });
        for (const k of Object.keys(sum)) sum[k] += r[k] || 0;
        after = r.last_id;
        status.textContent = `Checking names… ${sum.verified + sum.probable + sum.unverified + sum.unknown} done`;
        if (!r.remaining || !r.checked) break;
      }
      status.textContent = `${sum.verified} names confirmed, ${sum.probable} numbers fixed, `
        + `${sum.unverified} don’t match a real card`
        + (sum.unknown ? `, ${sum.unknown} couldn’t be checked` : "") + ".";
      await loadCards();
    } catch (err) {
      status.textContent = `Could not check names: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $("#export-csv-btn").addEventListener("click", async () => {
    const btn = $("#export-csv-btn");
    btn.disabled = true; const orig = btn.textContent; btn.textContent = "Generating…";
    try {
      const data = await api("/api/export/csvs", { method: "POST" });
      const links = data.written.map(w => `<a href="/${w.file}" target="_blank">${w.file} (${w.rows})</a>`).join(" · ");
      $("#upload-status").innerHTML = `Wrote ${data.written.length} CSV(s): ${links}`;
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false; btn.textContent = orig;
    }
  });
}

async function pollJob() {
  const bar = $("#job-bar");
  const fill = $("#progress-fill");
  const msg = $(".job-message");
  bar.classList.remove("hidden");
  state.jobRunning = true;
  let seen = "";
  const tick = async () => {
    try {
      const status = await api("/api/pricing/status");
      const pct = status.total ? (status.progress / status.total) * 100 : 0;
      fill.style.width = `${pct}%`;
      msg.textContent = status.message || "…";
      if (status.running) {
        // Names and prices go into the table as each batch lands.
        const at = `${status.phase || ""}:${status.progress}`;
        if (at !== seen) { seen = at; loadCards().catch(() => {}); }
        setTimeout(tick, 1500);
      } else {
        state.jobRunning = false;
        await loadCards();
        JOB_BUTTONS.forEach(s => { $(s).disabled = false; });
        // The finished message says how many were identified and priced;
        // keep it on screen after the bar goes away.
        if (status.message) $("#upload-status").textContent = `Last run: ${status.message}.`;
        setTimeout(() => bar.classList.add("hidden"), 1500);
      }
    } catch {
      setTimeout(tick, 2000);
    }
  };
  tick();
}

// ---------------------------------------------------------------- publish
function setupPublish() {
  const buttons = {
    ebay: $("#publish-ebay-btn"),
    tcgplayer: $("#publish-tcg-btn"),
    whatnot: $("#publish-whatnot-btn"),
  };

  async function refreshStatus() {
    let s;
    try {
      s = await api("/api/publish/status");
    } catch {
      return;
    }
    for (const site of ["ebay", "tcgplayer", "whatnot"]) {
      const el = $(`#pub-${site}`);
      const ready = s[site] && s[site].ready;
      if (el) {
        el.textContent = ready ? "(ready)" : "(setup needed)";
        el.style.color = ready ? "var(--up)" : "var(--attention)";
        buttons[site].title = ready ? "" : (s[site] && s[site].reason) || "";
      }
    }
  }

  const SITE_NAMES = { ebay: "eBay", tcgplayer: "TCGplayer", whatnot: "Whatnot" };

  async function publish(site) {
    const btn = buttons[site];
    let status;
    try {
      status = await api("/api/publish/status");
    } catch (err) {
      alert(err.message);
      return;
    }
    const c = status.counts[site];
    if (!status[site].ready) {
      $("#publish-msg").textContent = `${SITE_NAMES[site]} is not connected yet.`;
      openConnections(site);
      return;
    }
    const WHY_LEFT_OUT = {
      ebay: "without a hosted photo (Cloudinary is not set up; see Connections)",
      tcgplayer: c.catalog === false
        ? "cannot be matched until you upload your TCGplayer pricing export (Connections)"
        : "with no single matching row in your TCGplayer pricing export",
    };
    const leftOut = c.left_out
      ? `<p class="muted">${plural(c.left_out, "priced card")} ${WHY_LEFT_OUT[site]} will be left out.</p>` : "";
    if (!c.count) {
      $("#publish-msg").textContent = status.counts.priced
        ? `Nothing to send to ${SITE_NAMES[site]}: ${plural(c.left_out, "priced card")} ${WHY_LEFT_OUT[site]}.`
        : "Nothing to publish: no cards are priced yet.";
      return;
    }
    const money = v => `$${v.toFixed(2)}`;
    let skipFlagged = false, expected = null, aiDescriptions = false;
    if (site === "ebay") {
      // Flagged prices stay off eBay unless the user opts them in, the same
      // default the library's bulk list uses.
      const clean = c.count - c.flagged;
      // Show what a listing will say: written from the card's details, the
      // same way for every card.
      let sample = "";
      const first = state.cards.find(x => x.name && (x.list_price ?? x.final_price) != null && !x.sold && !onEbay(x));
      if (first) {
        try {
          const p = await api(`/api/cards/${first.id}/listing`);
          sample = `<p class="muted small">Each listing is written from the card's details and your notes. For example:</p>
            <div class="listing-preview"><strong>${escapeHtml(p.title)}</strong>${escapeHtml(p.description)}</div>`;
        } catch { /* the sample is a convenience */ }
      }
      const answer = await askConfirm({
        title: c.flagged
          ? `Publish live eBay listings for ${plural(clean, "card")}?`
          : `Publish ${plural(c.count, "live eBay listing")}?`,
        html: `<p>${plural(clean, "card")} will be listed live on your eBay account at their final
          prices, ${money(c.total - c.flagged_total)} in total. Buyers can purchase them as soon as this
          finishes. Cards already on eBay are updated, not duplicated. A price you set yourself is used
          in place of the estimate.</p>
          ${sample}
          ${c.to_upload ? `<p class="muted">Card photos that are not hosted yet (${c.flagged ? `${c.to_upload_unflagged}, or ${c.to_upload} with the flagged cards` : c.to_upload}) are uploaded to Cloudinary first.</p>` : ""}
          ${leftOut}
          ${c.flagged ? `<p class="flag-text">${plural(c.flagged, "more card")} (${money(c.flagged_total)})
            ${c.flagged === 1 ? "has a price" : "have prices"} flagged for review and will not be listed
            unless you tick the box.</p>` : ""}
          <label class="check ai-desc-opt"><input type="checkbox" id="ai-desc-opt" />
            Write descriptions with AI</label>
          <p class="muted small">Listings go live now with the description above. With the box ticked, each
            one switches to an AI-written description when it is ready: eBay writes them in batches of 10,
            a few minutes after a batch fills, and any short batch goes within 12 hours. Your notes stay under it.</p>`,
        confirm: "Publish to eBay",
        checkbox: c.flagged ? `Also list the ${c.flagged} flagged` : null,
      });
      if (!answer.ok) return;
      aiDescriptions = !!$("#ai-desc-opt")?.checked;
      skipFlagged = !answer.checked;
      expected = answer.checked ? c.count : clean;
      if (!expected) {
        $("#publish-msg").textContent = "Nothing listed: every eligible card has a flagged price.";
        return;
      }
    } else {
      const answer = await askConfirm({
        title: `Upload ${plural(c.count, "card")} to ${SITE_NAMES[site]}?`,
        html: `<p>A CSV with ${plural(c.count, "priced card")}, ${money(c.total)} in total, will be
          generated and submitted through your ${SITE_NAMES[site]} seller portal session.</p>
          ${leftOut}
          ${c.flagged ? `<p class="flag-text">${plural(c.flagged, "of these card")} ${c.flagged === 1 ? "has a price" : "have prices"} flagged for review. Fix or re-price them first if they should not go out as is.</p>` : ""}`,
        confirm: `Upload to ${SITE_NAMES[site]}`,
      });
      if (!answer.ok) return;
    }
    btn.disabled = true;
    const endpoint = site === "ebay" ? "/api/publish/ebay" : "/api/publish/portal";
    const opts = site === "ebay"
      ? { method: "POST", body: JSON.stringify({ skip_flagged: skipFlagged, expected, ai_descriptions: aiDescriptions }) }
      : { method: "POST", body: JSON.stringify({ site }) };
    try {
      await api(endpoint, opts);
      pollPublishJob({ descriptions: site === "ebay" && aiDescriptions });
    } catch (err) {
      alert(err.message);
      btn.disabled = false;
    }
  }

  Object.entries(buttons).forEach(([site, btn]) => {
    if (btn) btn.addEventListener("click", () => publish(site));
  });
  refreshStatus();
  refreshPublishStatus = refreshStatus;
}

let refreshPublishStatus = () => {};

// ---------------------------------------------------------------- sales read back
// eBay orders come over its API; Whatnot and TCGplayer sales come in as the
// CSV those sites export. Either way a sale finds its card by SKU.
async function loadSales() {
  let s;
  try {
    s = await api("/api/sales/status");
  } catch (err) {
    // Sales change libraries, so this part of the Scan page needs the operator.
    $("#sync-ebay-btn").disabled = true;
    $("#sales-import-btn").disabled = true;
    $("#sales-unmatched").classList.add("hidden");
    $("#sales-meta").innerHTML = err.status === 401
      ? '<a href="/login?next=/">Log in as the operator</a> to read sales back'
      : escapeHtml(err.message);
    return;
  }
  $("#sales-import-btn").disabled = false;
  const e = s.ebay;
  $("#sales-meta").textContent = !e.connected ? "eBay not connected"
    : !e.can_read_sales ? "reconnect eBay under Connections to read its sales"
    : !s.sync_minutes ? "automatic eBay checks are off"
    : s.last_sync_at ? `eBay last checked ${new Date(s.last_sync_at).toLocaleTimeString()}; checked automatically every ${s.sync_minutes} minutes`
    : `eBay is checked automatically every ${s.sync_minutes} minutes`;
  $("#sync-ebay-btn").disabled = !e.connected;
  const box = $("#sales-unmatched");
  box.classList.toggle("hidden", !s.unmatched.length);
  $("#sales-unmatched-body").innerHTML = s.unmatched.map(u => `<tr class="flag" data-id="${u.id}">
      <td>${u.sold_at ? new Date(u.sold_at).toLocaleDateString() : "—"}</td>
      <td>${escapeHtml(u.source)}</td>
      <td>${escapeHtml(u.sku || "—")}</td>
      <td>${escapeHtml(u.title || "")}</td>
      <td class="price">${u.quantity}</td>
      <td class="price">${fmt(u.unit_price)}</td>
      <td class="small">${escapeHtml(u.detail || "")}</td>
      <td class="actions"><button data-act="dismiss-sale">Dismiss</button></td>
    </tr>`).join("");
}

function salesSummary(r, where) {
  const bits = [`${plural(r.recorded, "sale")} recorded from ${where} ($${(r.total || 0).toFixed(2)})`];
  if (r.unmatched) bits.push(`${r.unmatched} could not be matched to a library card (listed below)`);
  if (r.duplicates) bits.push(`${r.duplicates} already recorded`);
  if (r.void) bits.push(`${r.void} cancelled or refunded`);
  if (r.adjusted) bits.push(`${r.adjusted} sale(s) were partly refunded since, and their price was lowered to match`);
  if (r.reversed) bits.push(`${r.reversed} earlier sale(s) were refunded since, and the card was put back`);
  if (r.not_paid_yet) bits.push(`${r.not_paid_yet} not paid yet`);
  if (r.skipped) bits.push(`${r.skipped} rows skipped (no SKU or price)`);
  return bits.join("; ") + ".";
}

function setupSales() {
  const msg = $("#sales-msg");
  $("#sync-ebay-btn").addEventListener("click", async () => {
    const btn = $("#sync-ebay-btn");
    btn.disabled = true; msg.textContent = "Reading eBay orders from the last 30 days…";
    try {
      const r = await api("/api/sales/sync/ebay", { method: "POST", body: "{}" });
      msg.textContent = salesSummary(r, "eBay") + (r.problems.length ? ` Problems: ${r.problems.join("; ")}` : "");
      await loadCards();
    } catch (err) {
      msg.textContent = `eBay sync did not run: ${err.message}`;
    } finally {
      btn.disabled = false;
      loadSales().catch(() => {});
    }
  });

  const file = $("#sales-file");
  $("#sales-import-btn").addEventListener("click", () => file.click());
  file.addEventListener("change", async () => {
    if (!file.files.length) return;
    const source = "whatnot", name = "Whatnot";
    const send = async dryRun => {
      const fd = new FormData();
      fd.append("file", file.files[0]);
      fd.append("source", source);
      if (dryRun) fd.append("dry_run", "1");
      const res = await fetch("/api/sales/import", { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `import failed (${res.status})`);
      return data;
    };
    try {
      const p = await send(true);
      const fresh = p.sales - p.already_imported;
      if (!fresh) {
        msg.textContent = p.sales
          ? `Nothing new: all ${p.sales} sales in that file were imported before.`
          : `No sales found in that file (${p.rows} rows, ${p.skipped} without a SKU or price).`;
        return;
      }
      const { ok } = await askConfirm({
        title: `Record ${plural(fresh, "sale")} from ${name}?`,
        html: `<p>${plural(fresh, "sale")} worth $${p.total.toFixed(2)} will be recorded against your library. Each sold card leaves it,
          comes off the marketplace, and shows under Sold.</p>
          <p class="muted">Read from columns: SKU “${escapeHtml(p.columns.sku)}”, price “${escapeHtml(p.columns.price)}”${p.columns.quantity ? `, quantity “${escapeHtml(p.columns.quantity)}”` : ""}${p.columns.date ? `, date “${escapeHtml(p.columns.date)}”` : ""}.
          ${p.already_imported ? `${p.already_imported} were imported before and are skipped.` : ""}
          ${p.skipped ? `${p.skipped} rows have no SKU or price and are skipped.` : ""}</p>`,
        confirm: "Record sales",
      });
      if (!ok) return;
      msg.textContent = salesSummary(await send(false), name);
      await loadCards();   // sold cards get their tag and leave the publish counts
    } catch (err) {
      msg.textContent = `Could not import: ${err.message}`;
    } finally {
      file.value = "";
      loadSales().catch(() => {});
    }
  });

  $("#sales-unmatched-body").addEventListener("click", async ev => {
    const btn = ev.target.closest('[data-act="dismiss-sale"]');
    if (!btn) return;
    await api(`/api/sales/external/${btn.closest("tr").dataset.id}/dismiss`, { method: "POST", body: "{}" });
    loadSales().catch(() => {});
  });
  loadSales().catch(() => {});
}

// ---------------------------------------------------------------- connections
// One panel that says, for each outside service, whether it is connected and
// what the next step is, and runs that step where it can.
const conn = { data: null, results: {}, focus: null, timer: null };

async function loadConnections() {
  conn.data = await api("/api/connections");
  conn.tcg = await api("/api/tcgplayer/catalog").catch(() => null);
  renderConnections();
  refreshPublishStatus();
  // Keep polling while a sign-in window is open.
  clearTimeout(conn.timer);
  const busy = ["tcgplayer", "whatnot"].some(s =>
    ["opening", "waiting"].includes(conn.data[s].login.state));
  if (busy && !$("#conn-modal").classList.contains("hidden")) {
    conn.timer = setTimeout(() => loadConnections().catch(() => {}), 1500);
  }
}

function openConnections(focus = null) {
  conn.focus = focus;
  $("#conn-modal").classList.remove("hidden");
  $("#conn-list").textContent = "Loading…";
  loadConnections().catch(err => { $("#conn-list").textContent = `Could not load: ${err.message}`; });
}

function closeConnections() {
  $("#conn-modal").classList.add("hidden");
  clearTimeout(conn.timer);
}

function connItem(key, name, state, cls, next, actions = "", note = "") {
  const res = conn.results[key];
  const steps = res && res.steps
    ? res.steps.map(s => `<li class="${s.ok ? "ok" : "bad"}"><strong>${escapeHtml(s.name)}:</strong> ${escapeHtml(s.detail)}</li>`).join("")
    : res ? `<li class="${res.ok ? "ok" : "bad"}">${escapeHtml(res.detail || res.error || "")}</li>` : "";
  return `<section class="conn-item" data-conn="${key}">
    <div class="conn-head"><strong>${name}</strong><span class="conn-state ${cls}">${state}</span></div>
    <p class="conn-next">${escapeHtml(next || "")}</p>
    ${actions ? `<div class="conn-actions">${actions}</div>` : ""}
    ${steps ? `<ul class="conn-result">${steps}</ul>` : ""}
    ${note ? `<p class="conn-note">${note}</p>` : ""}
  </section>`;
}

// TCGplayer's upload needs its own id for each card. Those come from the
// pricing export the seller downloads from TCGplayer.
function tcgCatalogHtml() {
  const t = conn.tcg;
  if (!t) return "";
  const status = t.loaded
    ? `Pricing export loaded: ${t.rows} products. ${t.matched} of your ${t.priced} priced cards match a TCGplayer Id.`
    : "Upload your TCGplayer pricing export (seller portal, Pricing, Export) so uploads carry real TCGplayer Ids. Without it the TCGplayer CSV is empty.";
  return `<div class="conn-actions" style="margin-top:8px">
      <span class="small">${escapeHtml(status)}</span>
      <button data-act="tcg-catalog">${t.loaded ? "Replace export" : "Upload export"}</button>
      <input id="tcg-catalog-file" type="file" accept=".csv,text/csv" hidden />
    </div>`;
}

function portalItem(key, name) {
  const d = conn.data[key];
  const login = d.login.state;
  let actions;
  if (!d.playwright) actions = "";
  else if (login === "opening") actions = `<span class="muted">Opening a browser window…</span>`;
  else if (login === "waiting") {
    actions = `<button class="primary" data-act="portal-finish">I'm signed in, save it</button>
               <button data-act="portal-cancel">Cancel</button>`;
  } else if (d.session) {
    actions = `<button data-act="portal-check">Check session</button>
               <button data-act="portal-login">Sign in again</button>
               <button data-act="portal-disconnect">Forget session</button>`;
  } else {
    actions = `<button class="primary" data-act="portal-login">Sign in to ${name}</button>`;
  }
  const problem = ["error", "cancelled"].includes(login) && d.login.detail
    ? ` Last attempt: ${d.login.detail}.` : "";
  return connItem(key, name,
    d.session ? "session saved" : "not connected", d.session ? "ok" : "todo",
    d.next + problem, actions + (key === "tcgplayer" ? tcgCatalogHtml() : ""),
    `${name} has no listing API for regular sellers, so this drives your seller portal in a browser.
     That is against ${name}'s terms and can break when they change the page. Uploading the exported CSV by hand always works.`);
}

function renderConnections() {
  const d = conn.data;
  const e = d.ebay;
  let ebayActions = "";
  if (e.configured && !e.authorized) {
    ebayActions = `<button class="primary" data-act="ebay-consent">1. Open eBay consent page</button>
      <input id="ebay-redirect-input" placeholder="2. Paste the URL eBay sent you to" aria-label="Redirect URL from eBay" />
      <button data-act="ebay-authorize">3. Connect</button>`;
  } else if (e.authorized) {
    ebayActions = `<button data-act="ebay-check">Check account</button>
                   <button data-act="ebay-disconnect">Disconnect</button>`;
  }
  const s = d.stripe;
  $("#conn-list").innerHTML = [
    connItem("identify", "Card identification (Anthropic)", d.identify.ready ? "ready" : "needs setup",
      d.identify.ready ? "ok" : "todo", d.identify.next),
    connItem("ebay", `eBay${e.env === "sandbox" ? " (sandbox)" : ""}`,
      e.ready ? "connected" : "needs setup", e.ready ? "ok" : "todo", e.next + (e.error ? ` (${e.error})` : ""),
      ebayActions,
      e.authorized ? "" : "After you approve, eBay sends the browser to your RuName's accepted URL. That page can be anything; copy its full address from the address bar."),
    connItem("cloudinary", "Photo hosting (Cloudinary)", d.cloudinary.ready ? "ready" : "needs setup",
      d.cloudinary.ready ? "ok" : "todo", d.cloudinary.next),
    portalItem("tcgplayer", "TCGplayer"),
    portalItem("whatnot", "Whatnot"),
    connItem("stripe", "Card payments (Stripe)",
      !s.configured ? "off" : s.mode === "test" ? "test mode" : "live", s.ready ? "ok" : "todo", s.next,
      s.ready ? `<button data-act="stripe-check">Check key</button>` : "",
      s.mode === "test" ? "Test card: 4242 4242 4242 4242, any future expiry, any CVC." : ""),
  ].join("");
  if (conn.focus) {
    const el = $(`[data-conn="${conn.focus}"]`);
    if (el) el.scrollIntoView({ block: "center" });
    conn.focus = null;
  }
}

function setupConnections() {
  ["#connections-btn", "#connections-btn-2"].forEach(sel => $(sel).addEventListener("click", () => openConnections()));
  $("#conn-close").addEventListener("click", closeConnections);
  $("#conn-modal").addEventListener("click", ev => { if (ev.target.id === "conn-modal") closeConnections(); });

  $("#conn-list").addEventListener("click", async ev => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const key = btn.closest("[data-conn]").dataset.conn;
    if (btn.dataset.act === "tcg-catalog") {
      // Handled apart from the other buttons: the panel must not redraw
      // (and replace the file input) while the file dialog is open.
      const input = $("#tcg-catalog-file");
      input.onchange = async () => {
        if (!input.files.length) return;
        // The whole-catalog export is tens of megabytes, so it goes in parts.
        const parts = csvParts(await input.files[0].text());
        let res, data = {};
        for (let i = 0; i < parts.length; i++) {
          const fd = new FormData();
          fd.append("file", new Blob([parts[i]], { type: "text/csv" }), "export.csv");
          fd.append("part", i + 1);
          fd.append("parts", parts.length);
          res = await fetch("/api/tcgplayer/catalog", { method: "POST", body: fd });
          data = await res.json().catch(() => ({}));
          if (!res.ok) break;
        }
        conn.results.tcgplayer = res.ok
          ? { ok: true, detail: `Loaded ${data.rows} products; ${data.matched} of ${data.priced} priced cards matched.` }
          : { ok: false, detail: data.error || "upload failed" };
        loadConnections().catch(() => {});
      };
      input.click();
      return;
    }
    const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });
    const label = btn.textContent;
    btn.disabled = true;
    delete conn.results[key];
    try {
      switch (btn.dataset.act) {
        case "ebay-consent": {
          const { url } = await post("/api/connections/ebay/consent-url");
          window.open(url, "_blank", "noopener");
          break;
        }
        case "ebay-authorize": {
          const redirect = $("#ebay-redirect-input").value.trim();
          if (!redirect) { conn.results.ebay = { ok: false, detail: "Paste the URL from the browser's address bar first." }; break; }
          await post("/api/connections/ebay/authorize", { redirect });
          conn.results.ebay = { ok: true, detail: "eBay account connected." };
          break;
        }
        case "ebay-check":
          btn.textContent = "Checking…";
          conn.results.ebay = await post("/api/connections/ebay/check");
          break;
        case "ebay-disconnect": {
          const { ok } = await askConfirm({
            title: "Disconnect eBay?",
            html: "<p>The saved authorization is deleted. Listings already live on eBay stay up; you can reconnect at any time.</p>",
            confirm: "Disconnect",
          });
          if (ok) await post("/api/connections/ebay/disconnect");
          break;
        }
        case "stripe-check":
          btn.textContent = "Checking…";
          conn.results.stripe = await post("/api/connections/stripe/check");
          break;
        case "portal-login":
          await post(`/api/connections/portal/${key}/login`);
          break;
        case "portal-finish":
          btn.textContent = "Saving…";
          await post(`/api/connections/portal/${key}/login/finish`);
          conn.results[key] = { ok: true, detail: "Session saved. Run “Check session” to confirm it works." };
          break;
        case "portal-cancel":
          await post(`/api/connections/portal/${key}/login/cancel`);
          break;
        case "portal-check":
          btn.textContent = "Checking… (opens the import page in the background)";
          conn.results[key] = await post(`/api/connections/portal/${key}/check`);
          break;
        case "portal-disconnect":
          await post(`/api/connections/portal/${key}/disconnect`);
          break;
      }
    } catch (err) {
      conn.results[key] = { ok: false, detail: err.message };
    } finally {
      btn.disabled = false;
      btn.textContent = label;
      loadConnections().catch(() => {});
    }
  });
}

// AI descriptions: ask the server to move them along (send full batches to
// eBay, collect what eBay has finished, write the rest), and ask again only
// while eBay is working on some. Cards waiting for a batch to fill are left
// to the scheduled run, so an idle page asks for nothing.
let watchingDescriptions = null;
async function watchDescriptions() {
  if (watchingDescriptions) return;
  const started = Date.now();
  const say = s => {
    const parts = [];
    if (s.written_today) parts.push(`${plural(s.written_today, "AI description")} written today`);
    if (s.at_ebay) parts.push(`${s.at_ebay} being written by eBay`);
    if (s.waiting) parts.push(`${s.waiting} waiting for a batch`);
    if (parts.length) $("#publish-msg").textContent = parts.join(", ") + ".";
  };
  const tick = async () => {
    try {
      const s = await api("/api/publish/descriptions", { method: "POST" });
      say(s);
      if (s.from_ebay || s.written_by_us) await loadCards();
      if (s.at_ebay && Date.now() - started < 20 * 60 * 1000) {
        watchingDescriptions = setTimeout(tick, 45000);
        return;
      }
    } catch { /* the scheduled runs carry on without the page */ }
    watchingDescriptions = null;
  };
  watchingDescriptions = setTimeout(tick, 0);
}

async function pollPublishJob({ descriptions = false } = {}) {
  const bar = $("#publish-bar");
  const fill = $("#publish-progress-fill");
  const msg = $("#publish-bar-msg");
  bar.classList.remove("hidden");
  const tick = async () => {
    try {
      const status = await api("/api/publish/job-status");
      const pct = status.total ? (status.progress / status.total) * 100 : 0;
      fill.style.width = `${pct}%`;
      msg.textContent = `${status.site || "publish"}: ${status.message || "…"}`;
      if (status.running) setTimeout(tick, 1500);
      else {
        await loadCards();
        $("#publish-msg").textContent = status.message || "";
        ["#publish-ebay-btn", "#publish-tcg-btn", "#publish-whatnot-btn"].forEach(s => $(s).disabled = false);
        if (descriptions) watchDescriptions();
        setTimeout(() => bar.classList.add("hidden"), 2500);
      }
    } catch {
      setTimeout(tick, 2000);
    }
  };
  tick();
}

// ---------------------------------------------------------------- init
function setupKeyboard() {
  document.addEventListener("keydown", ev => {
    const modal = ["#confirm-modal", "#choose-modal", "#conn-modal", "#bundle-modal", "#modal"]
      .map(s => $(s)).find(el => !el.classList.contains("hidden"));
    if (ev.key === "Tab" && modal) {
      const focusable = Array.from(modal.querySelectorAll('button, input:not([type=hidden]), select, textarea, a[href], summary, [tabindex="0"]'))
        .filter(el => {
          if (el.disabled || !el.getClientRects().length) return false;
          // Closed details can still report layout rectangles in Chromium.
          for (let parent = el.parentElement; parent && parent !== modal; parent = parent.parentElement) {
            if (parent.tagName === "DETAILS" && !parent.open && !parent.querySelector("summary")?.contains(el)) return false;
          }
          return true;
        });
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (ev.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) {
        ev.preventDefault(); last?.focus();
      } else if (!ev.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) {
        ev.preventDefault(); first?.focus();
      }
    }
    if (ev.key === "Escape") {
      if (modal?.id === "confirm-modal") $("#confirm-cancel").click();
      else if (modal?.id === "choose-modal") closeChoose();
      else if (modal?.id === "listing-modal") closeListing();
      else if (modal?.id === "conn-modal") closeConnections();
      else if (modal?.id === "bundle-modal") closeBundle();
      else closeEdit();
    }
  });
}

setupDropzone();
setupTable();
setupModal();
setupChoose();
setupListing();
setupBundleModal();
setupToolbar();
setupKeyboard();
loadCards();
startLiveRefresh();
// A job started before this page was opened carries on showing here.
api("/api/pricing/status").then(s => { if (s.running && !state.jobRunning) pollJob(); }).catch(() => {});
// Publishing, Connections, sales sync and Terapeak use the install's own
// outside accounts, so only the operator gets them. The server enforces it;
// this keeps the page from showing controls that would be refused.
api("/api/session").catch(() => ({ operator: true })).then((me) => {
  if (me.operator === false) {
    document.querySelectorAll(".publish-panel, .sales-panel, #terapeak-label")
      .forEach(el => el.classList.add("hidden"));
    return;
  }
  setupPublish();
  setupConnections();
  setupSales();
});
