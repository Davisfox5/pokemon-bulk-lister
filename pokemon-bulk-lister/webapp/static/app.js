// Single-page UI logic for the bulk-lister. Vanilla JS, no framework.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  cards: [],
  stats: {},
  sort: "confidence_asc",
  needsReview: false,
  unidentified: false,
  editing: null,
  selected: new Set(),
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
  $("#stats").innerHTML = `
    <span><strong>${stats.total ?? 0}</strong> cards</span>
    <span><strong>${stats.identified ?? 0}</strong> identified</span>
    <span><strong>${stats.priced ?? 0}</strong> priced</span>
    <span><strong>${stats.flagged ?? 0}</strong> flagged</span>
    <span><strong>${stats.uploaded ?? 0}</strong> uploaded</span>
    <span><strong>${stats.ebay_listed ?? 0}</strong> on eBay</span>
    <span>est. value: <strong>$${(stats.total_value ?? 0).toFixed(2)}</strong></span>
  `;
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
    return { value: card.terapeak_median_usd, window: "365d", count: card.terapeak_sold_count_365d };
  }
  if (card.ebay_median_30d != null && card.ebay_median_30d > 0) {
    return { value: card.ebay_median_30d, window: "30d", count: card.ebay_sold_count_30d };
  }
  return { value: null, window: null, count: 0 };
}

// What the final price rests on, and why a flagged row was flagged. A price
// with no visible basis is a bug.
function priceBasis(card) {
  if (card.final_price == null) return "";
  const n = [card.tcgplayer_market, card.cardmarket_trend_usd, card.ebay_median_30d, card.terapeak_median_usd]
    .filter(v => v != null && v > 0).length;
  const basis = `<span class="src">${n} source${n === 1 ? "" : "s"}</span>`;
  const why = card.needs_review && card.pricing_notes
    ? `<span class="src why" title="${escapeHtml(card.pricing_notes)}">${escapeHtml(card.pricing_notes.slice(0, 70))}</span>`
    : "";
  return basis + why;
}

function renderRow(card) {
  const flagClass = card.needs_review ? "flag" : "";
  const setBits = [card.set_name, card.card_number ? `#${card.card_number}` : "", card.rarity]
    .filter(Boolean)
    .join(" · ");
  const ebay = bestEbay(card);
  const ebayCell = ebay.value == null
    ? `<span class="muted">—</span>`
    : `${fmt(ebay.value)}<div class="muted small">${ebay.window} · n=${ebay.count}</div>`;
  const checked = state.selected.has(card.id) ? "checked" : "";
  return `
    <tr class="${flagClass}" data-id="${card.id}">
      <td class="select"><input type="checkbox" data-act="select" ${checked} /></td>
      <td class="thumb"><img src="/${card.crop_path}" alt="" loading="lazy" /></td>
      <td>
        <strong>${escapeHtml(card.name) || "<span class='muted'>(unidentified)</span>"}</strong>
        ${card.is_holo ? "<span class='muted'> · holo</span>" : ""}
        <div class="muted small">${escapeHtml(card.condition_guess || "")}</div>
      </td>
      <td class="muted small">${escapeHtml(setBits)}</td>
      <td class="price">${fmt(card.tcgplayer_market)}</td>
      <td class="price">${fmt(card.cardmarket_trend_usd)}</td>
      <td class="price">${ebayCell}</td>
      <td class="price"><strong>${fmt(card.final_price)}</strong>${priceBasis(card)}</td>
      <td class="conf">${(card.pricing_confidence ?? 0).toFixed(2)}</td>
      <td class="actions">
        <button data-act="edit">Edit</button>
        <button data-act="price">Price</button>
      </td>
    </tr>
  `;
}

function renderCards() {
  const body = $("#cards-body");
  if (!state.cards.length) {
    body.innerHTML = `<tr><td colspan="10" class="muted">No cards yet. Upload a grid above.</td></tr>`;
    updateBundleButton();
    return;
  }
  // Drop selections that aren't in the current view.
  const visible = new Set(state.cards.map(c => c.id));
  for (const id of Array.from(state.selected)) {
    if (!visible.has(id)) state.selected.delete(id);
  }
  body.innerHTML = state.cards.map(renderRow).join("");
  syncSelectAllCheckbox();
  updateBundleButton();
}

function syncSelectAllCheckbox() {
  const all = $("#select-all");
  if (!all) return;
  const total = state.cards.length;
  const sel = state.cards.filter(c => state.selected.has(c.id)).length;
  all.checked = total > 0 && sel === total;
  all.indeterminate = sel > 0 && sel < total;
}

function updateBundleButton() {
  const btn = $("#bundle-btn");
  if (!btn) return;
  const n = state.selected.size;
  btn.textContent = `Bundle selected (${n})`;
  btn.disabled = n < 2;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ---------------------------------------------------------------- data fetch
async function loadCards() {
  const params = new URLSearchParams({ sort: state.sort });
  if (state.needsReview) params.set("needs_review", "1");
  if (state.unidentified) params.set("unidentified", "1");
  const data = await api(`/api/cards?${params}`);
  state.cards = data.cards;
  state.stats = data.stats;
  renderStats(state.stats);
  renderCards();
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

  async function uploadFiles(files) {
    if (!files || !files.length) return;
    const mode = scanMode();
    status.textContent = `Uploading ${files.length} photo(s)…`;
    const fd = new FormData();
    for (const f of files) fd.append("file", f);
    fd.append("mode", mode);
    try {
      const res = await fetch("/api/grids/upload", { method: "POST", body: fd });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const skipped = (data.skipped || []).length
        ? ` Skipped ${data.skipped.length} file(s) that are not JPG, PNG, WEBP or TIFF.` : "";
      status.textContent = data.total_crops
        ? `Added ${data.total_crops} card(s) from ${data.grids.length} photo(s).${skipped}`
        : `No cards found in ${data.grids.length} photo(s).${skipped}`;
      await loadCards();
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
  const saved = JSON.parse(localStorage.getItem("scan-settings") || "{}");
  if (saved.mode) $$('input[name="scan-mode"]').forEach(r => { r.checked = r.value === saved.mode; });
  if (saved.auto === false) $("#auto-process").checked = false;
  const remember = () => {
    localStorage.setItem("scan-settings", JSON.stringify({ mode: scanMode(), auto: $("#auto-process").checked }));
    $("#dropzone-hint").textContent = scanMode() === "single"
      ? "Each photo is one card. Shoot it flat on a plain, darker background."
      : "Binder pages are split into up to 9 cards. Empty pockets are skipped.";
  };
  $$('input[name="scan-mode"]').forEach(r => r.addEventListener("change", remember));
  $("#auto-process").addEventListener("change", remember);
  remember();
}

function scanMode() {
  const picked = $('input[name="scan-mode"]:checked');
  return picked ? picked.value : "grid";
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
  $("#confirm-title").textContent = title;
  $("#confirm-text").innerHTML = html;
  $("#confirm-ok").textContent = confirm;
  $("#confirm-check").checked = false;
  $("#confirm-check-row").classList.toggle("hidden", !checkbox);
  $("#confirm-check-label").textContent = checkbox || "";
  const modal = $("#confirm-modal");
  modal.classList.remove("hidden");
  $("#confirm-ok").focus();
  return new Promise(resolve => {
    const done = ok => {
      modal.classList.add("hidden");
      modal.removeEventListener("keydown", onKey);
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
  const pool = picked.length ? picked : state.cards;
  const ready = pool.filter(c => c.name && c.final_price != null);
  const flagged = ready.filter(c => c.needs_review).length;
  const left = pool.length - ready.length;
  if (!ready.length) {
    $("#upload-status").textContent = picked.length
      ? "None of the selected cards are identified and priced yet."
      : "No cards are identified and priced yet. Run “Identify + price new cards” first.";
    return;
  }
  const value = ready.reduce((sum, c) => sum + c.final_price, 0);
  const { ok } = await askConfirm({
    title: `Add ${plural(ready.length, "card")} to your library?`,
    html: `<p>${plural(ready.length, "priced card")}${picked.length ? " from your selection" : ""}, worth
      $${value.toFixed(2)} at current prices, will be added to your library. Cards already added are skipped.</p>
      ${left ? `<p class="muted">${plural(left, "card")} without a name or price stay on the bench.</p>` : ""}
      ${flagged ? `<p class="flag-text">${plural(flagged, "card")} ${flagged === 1 ? "has a price" : "have prices"} flagged for review. They are added too and stay flagged in the library.</p>` : ""}`,
    confirm: "Add to library",
  });
  if (!ok) return;
  // Send exactly the cards the dialog counted, never "everything priced":
  // with a filter on, the table shows only part of the bench.
  const body = { card_ids: ready.map(c => c.id) };
  const res = await api("/api/inventory/promote", { method: "POST", body: JSON.stringify(body) });
  $("#upload-status").innerHTML =
    `Added ${plural(res.promoted, "card")} to your library` +
    (res.skipped_duplicates ? ` (${res.skipped_duplicates} were already there)` : "") +
    `. <a href="/portfolio">Open library</a> to sell them or check them against decks.`;
}

// ---------------------------------------------------------------- table interactions
function setupTable() {
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
    const tr = ev.target.closest("tr");
    const id = parseInt(tr.dataset.id, 10);
    if (btn.dataset.act === "edit") openEdit(id);
    else if (btn.dataset.act === "price") {
      btn.disabled = true; btn.textContent = "…";
      try {
        await api(`/api/cards/${id}/price`, { method: "POST" });
        await loadCards();
      } catch (err) {
        alert(err.message);
      } finally {
        btn.disabled = false; btn.textContent = "Price";
      }
    }
  });

  $("#select-all").addEventListener("change", ev => {
    if (ev.target.checked) state.cards.forEach(c => state.selected.add(c.id));
    else state.cards.forEach(c => state.selected.delete(c.id));
    renderCards();
  });
}

// ---------------------------------------------------------------- modals
function openEdit(id) {
  const card = state.cards.find(c => c.id === id);
  if (!card) return;
  state.editing = id;
  $("#modal-title").textContent = `Edit · ${card.name || "(unidentified)"}`;
  $("#modal-img").src = `/${card.crop_path}`;
  const form = $("#edit-form");
  form.id.value = card.id;
  form.name.value = card.name || "";
  form.set_name.value = card.set_name || "";
  form.set_code.value = card.set_code || "";
  form.card_number.value = card.card_number || "";
  form.rarity.value = card.rarity || "";
  form.condition_guess.value = card.condition_guess || "";
  form.is_holo.checked = !!card.is_holo;
  form.id_confidence.value = card.id_confidence ?? 0;

  $("#modal-prices").innerHTML = `
    <div>TCG market<strong>${fmt(card.tcgplayer_market)}</strong></div>
    <div>CM trend (USD)<strong>${fmt(card.cardmarket_trend_usd)}</strong></div>
    <div>CM trend (EUR)<strong>${fmt(card.cardmarket_trend_eur)}</strong></div>
    <div>eBay median 30d<strong>${fmt(card.ebay_median_30d)}</strong></div>
    <div>eBay max 30d<strong>${fmt(card.ebay_max_30d)}</strong></div>
    <div>Terapeak median<strong>${fmt(card.terapeak_median_usd)}</strong></div>
    <div>eBay sold count<strong>${card.ebay_sold_count_30d || 0}</strong></div>
    <div>Final price<strong>${fmt(card.final_price)}</strong></div>
    <div>Pricing conf.<strong>${(card.pricing_confidence ?? 0).toFixed(2)}</strong></div>
    <div>Outlier<strong>${card.outlier_flag ? "yes" : "no"}</strong></div>
    <div>Image URL<strong>${card.image_url ? "uploaded" : "—"}</strong></div>
    <div>Notes<strong>${escapeHtml(card.pricing_notes || "")}</strong></div>
  `;
  $("#modal").classList.remove("hidden");
}

function closeEdit() { $("#modal").classList.add("hidden"); state.editing = null; }

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
    is_holo: form.is_holo.checked,
    id_confidence: parseFloat(form.id_confidence.value) || 0,
  };
  try {
    await api(`/api/cards/${state.editing}`, { method: "PATCH", body: JSON.stringify(patch) });
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
    }
    await loadCards();
    closeEdit();
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
    const form = $("#edit-form");
    form.name.value = updated.name || "";
    form.set_name.value = updated.set_name || "";
    form.set_code.value = updated.set_code || "";
    form.card_number.value = updated.card_number || "";
    form.rarity.value = updated.rarity || "";
    form.condition_guess.value = updated.condition_guess || "";
    form.is_holo.checked = !!updated.is_holo;
    form.id_confidence.value = updated.id_confidence ?? 0;
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
function setupToolbar() {
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
        replaces their prices.${useTerapeak === "1" ? " Terapeak adds about 5 seconds per card." : ""}</p>
        <p class="muted">To price only new cards, use “Identify + price new cards”.</p>`,
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

  // One-shot probe of Terapeak login state on page load.
  api("/api/terapeak/status").then(s => {
    const el = $("#terapeak-status");
    if (s.logged_in) {
      el.textContent = "(logged in)";
      el.style.color = "#15803d";
    } else {
      el.textContent = "(first run will prompt login)";
    }
  }).catch(() => {});

  $("#bundle-btn").addEventListener("click", openBundle);

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
  const tick = async () => {
    try {
      const status = await api("/api/pricing/status");
      const pct = status.total ? (status.progress / status.total) * 100 : 0;
      fill.style.width = `${pct}%`;
      msg.textContent = status.message || "…";
      if (status.running) setTimeout(tick, 1500);
      else {
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
        el.style.color = ready ? "#15803d" : "#b45309";
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
      tcgplayer: "without a TCGplayer product id (they did not match a TCGplayer product when priced)",
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
    let skipFlagged = false, expected = null;
    if (site === "ebay") {
      // Flagged prices stay off eBay unless the user opts them in, the same
      // default the library's bulk list uses.
      const clean = c.count - c.flagged;
      const answer = await askConfirm({
        title: c.flagged
          ? `Publish live eBay listings for ${plural(clean, "card")}?`
          : `Publish ${plural(c.count, "live eBay listing")}?`,
        html: `<p>${plural(clean, "card")} will be listed live on your eBay account at their final
          prices, ${money(c.total - c.flagged_total)} in total. Buyers can purchase them as soon as this
          finishes. Cards already on eBay are updated, not duplicated.</p>
          ${c.to_upload ? `<p class="muted">Card photos that are not hosted yet (${c.flagged ? `${c.to_upload_unflagged}, or ${c.to_upload} with the flagged cards` : c.to_upload}) are uploaded to Cloudinary first.</p>` : ""}
          ${leftOut}
          ${c.flagged ? `<p class="flag-text">${plural(c.flagged, "more card")} (${money(c.flagged_total)})
            ${c.flagged === 1 ? "has a price" : "have prices"} flagged for review and will not be listed
            unless you tick the box.</p>` : ""}`,
        confirm: "Publish to eBay",
        checkbox: c.flagged ? `Also list the ${c.flagged} flagged` : null,
      });
      if (!answer.ok) return;
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
      ? { method: "POST", body: JSON.stringify({ skip_flagged: skipFlagged, expected }) }
      : { method: "POST", body: JSON.stringify({ site }) };
    try {
      await api(endpoint, opts);
      pollPublishJob();
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

// ---------------------------------------------------------------- connections
// One panel that says, for each outside service, whether it is connected and
// what the next step is, and runs that step where it can.
const conn = { data: null, results: {}, focus: null, timer: null };

async function loadConnections() {
  conn.data = await api("/api/connections");
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
    d.next + problem, actions,
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

async function pollPublishJob() {
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
    if (ev.key === "Escape") {
      closeEdit();
      closeBundle();
      if ($("#confirm-modal").classList.contains("hidden")) closeConnections();
    }
  });
}

setupDropzone();
setupTable();
setupModal();
setupBundleModal();
setupToolbar();
setupPublish();
setupConnections();
setupKeyboard();
loadCards();
