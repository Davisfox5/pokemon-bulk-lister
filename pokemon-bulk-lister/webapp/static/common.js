/* Shared helpers for the library, decks and marketplace pages. Plain script,
   no modules: everything here is a global the page scripts use. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const fmtUsd = (v) =>
  v == null ? "—" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) {
    window.location = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("login required");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

const postJson = (path, body = {}, method = "POST") =>
  api(path, { method, body: JSON.stringify(body) });

/* ---------------- status line ---------------- */

let toastTimer = null;
function toast(message, kind = "ok") {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), kind === "err" ? 8000 : 4500);
}

/* ---------------- formatting ---------------- */

function ago(iso) {
  if (!iso) return null;
  const mins = Math.max(0, (Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 60 * 48) return `${Math.round(mins / 60)}h ago`;
  return `${Math.round(mins / 1440)}d ago`;
}

const shortDate = (iso) => (iso ? new Date(iso).toLocaleDateString() : "—");

const SOURCE_LABELS = { tcgplayer: "TCGplayer", cardmarket: "Cardmarket", ebay_30d: "eBay" };
const SOURCE_NAMES = {
  tcgplayer: "TCGplayer market", cardmarket: "Cardmarket trend (USD)", ebay_30d: "eBay sold median, 30 days",
};

/* Keep the basis visible; comparisons and numeric confidence are available
   by keyboard or touch instead of depending on a hover tooltip. */
function provenanceHtml(p) {
  if (!p) return '<span class="prov price-source-line">No price yet</span>';
  const keys = Object.keys(SOURCE_LABELS).filter((k) => p.sources && p.sources[k] != null);
  const conf = `${Math.round((p.confidence ?? 0) * 100)}%`;
  const own = /^From (your|the imported) spreadsheet/.test(p.notes || "");
  const basis = keys.length ? keys.map(k => SOURCE_LABELS[k]).join(" + ")
    : own ? "Spreadsheet" : p.priced_at ? "No source prices" : "No price yet";
  const line = [basis, ago(p.priced_at)].filter(Boolean).join(" · ");
  // Close prices are not a disagreement: show the range the price sits in.
  const vals = keys.map(k => p.sources[k]).filter(x => x > 0);
  const lo = vals.length > 1 ? Math.min(...vals) : 0, hi = vals.length > 1 ? Math.max(...vals) : 0;
  const range = !p.outlier && hi - lo >= 0.01 && !/Prices (vary|disagree)/i.test(p.notes || "")
    ? `<span class="price-range">Range ${fmtUsd(lo)} to ${fmtUsd(hi)}</span>` : "";
  // Preserve the actual reason: a generic "Check" would hide a Japanese
  // proxy, a missing number or a different finish behind the same badge.
  // The server words every note (lib/price_notes.py), disagreements included.
  const note = p.notes || "Check the price before listing.";
  const warning = own ? "Spreadsheet estimate. Check the price." : note;
  // Flagged: something to resolve, in red. Otherwise a small yellow note
  // when the sources are far apart or there is only one.
  const why = p.flagged ? `<span class="flag-note">${esc(warning)}</span>`
    : p.caution ? `<span class="caution-note">${esc(p.caution)}</span>` : "";
  if (!p.priced_at && !p.notes && !keys.length) return `<span class="prov price-source-line">${esc(line)}</span>${why}`;
  return `<span class="prov price-source-line">${esc(line)}</span>${range}${why}
    <details class="price-details">
      <summary>Price details</summary>
      <div class="price-detail-body">
        ${keys.map(k => `<div><span>${SOURCE_NAMES[k]}</span><strong>${fmtUsd(p.sources[k])}</strong></div>`).join("")}
        <p>${conf} confidence${p.priced_at ? ` · ${esc(new Date(p.priced_at).toLocaleString())}` : ""}</p>
        ${p.notes ? `<p>${esc(p.notes)}</p>` : ""}
      </div>
    </details>`;
}

function thumbHtml(row, cls = "inv-thumb") {
  if (row.image_url) return `<img class="${cls}" src="${esc(row.image_url)}" alt="" loading="lazy">`;
  if (row.photo_url) return `<img class="${cls}" src="${esc(row.photo_url)}" alt="" loading="lazy">`;
  if (row.crop_path) return `<img class="${cls}" src="/${esc(row.crop_path)}" alt="" loading="lazy">`;
  return `<div class="${cls} ph"></div>`;
}

function cardLine(row) {
  const set = row.set_name || (row.set_code ? String(row.set_code).toUpperCase() : "");
  return `<div class="inv-name">${esc(row.name)}${row.printing === "reverseHolofoil" ? ' <span class="holo">reverse holo</span>' : row.is_holo ? ' <span class="holo">holo</span>' : row.printing === "unknown" ? ' <span class="flag-note">Finish not checked</span>' : ""}</div>
    <div class="inv-set">${esc(set)}${row.card_number ? " #" + esc(row.card_number) : ""}</div>`;
}

/* ---------------- confirm dialog ---------------- */

/* askConfirm({title, html, confirm, checkbox}) -> Promise<{ok, checked}>.
   Used before anything that lists a card or commits to a purchase, so the
   user reads what is about to happen, with counts, before it does. */
function askConfirm({ title, html, confirm = "Confirm", checkbox = null }) {
  let dlg = $("#confirm-dialog");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "confirm-dialog";
    document.body.appendChild(dlg);
  }
  dlg.innerHTML = `
    <h2>${esc(title)}</h2>
    <div class="dialog-body">${html}</div>
    ${checkbox ? `<label class="check"><input type="checkbox" id="confirm-check"> ${esc(checkbox)}</label>` : ""}
    <div class="dialog-row">
      <button type="button" class="btn" data-act="cancel">Cancel</button>
      <button type="button" class="btn btn-primary" data-act="ok">${esc(confirm)}</button>
    </div>`;
  return new Promise((resolve) => {
    const done = (ok) => {
      const checked = !!($("#confirm-check") && $("#confirm-check").checked);
      dlg.close();
      resolve({ ok, checked });
    };
    dlg.querySelector('[data-act="cancel"]').onclick = () => done(false);
    dlg.querySelector('[data-act="ok"]').onclick = () => done(true);
    dlg.oncancel = (e) => { e.preventDefault(); done(false); };
    dlg.showModal();
    dlg.querySelector('[data-act="ok"]').focus();
  });
}

/* Buy flow shared by the marketplace and the deck page. `l` needs
   listing_id, name, price, seller and optionally set_name/card_number/condition. */
async function buyListing(l, payments) {
  const how = payments === "stripe"
    ? "Payment is by card through Stripe. The card moves to the library once payment clears."
    : "No card payments are set up here, so payment goes directly to the seller. The card moves to the library when the seller marks the order paid.";
  const what = [l.set_name, l.card_number ? "#" + l.card_number : "", l.condition].filter(Boolean).join(" · ");
  const { ok } = await askConfirm({
    title: `Buy for ${fmtUsd(l.price)}?`,
    html: `<p><strong>${esc(l.name)}</strong>${what ? " (" + esc(what) + ")" : ""} from seller ${esc(l.seller)}.</p>
           <p class="muted">${how}</p>`,
    confirm: `Buy for ${fmtUsd(l.price)}`,
  });
  if (!ok) return false;
  const res = await postJson(`/api/market/listings/${l.listing_id}/buy`);
  if (res.checkout_url) { window.location = res.checkout_url; return true; }
  toast("Order placed. The order shows under Purchased once the seller confirms payment.");
  return true;
}

/* Enter submits, Escape closes: wire a <dialog> holding one <form>. */
function openDialog(dlg) {
  dlg.showModal();
  const first = dlg.querySelector("input:not([type=hidden]), select, textarea");
  if (first) first.focus();
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-close-dialog]");
  if (btn) btn.closest("dialog").close();
});
