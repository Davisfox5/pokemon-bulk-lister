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

const SOURCE_LABELS = { tcgplayer: "TCG", cardmarket: "CM", ebay_30d: "eBay" };
const SOURCE_NAMES = {
  tcgplayer: "TCGplayer market", cardmarket: "Cardmarket trend (USD)", ebay_30d: "eBay sold median, 30 days",
};

/* Where a price came from and how far to trust it. Every price on these
   pages gets one of these under it: one compact line, full names on hover. */
function provenanceHtml(p) {
  if (!p || !p.priced_at) return '<span class="prov">no market price on file</span>';
  const keys = Object.keys(SOURCE_LABELS).filter((k) => p.sources && p.sources[k] != null);
  const short = keys.map((k) => `${SOURCE_LABELS[k]} ${fmtUsd(p.sources[k])}`);
  const long = keys.map((k) => `${SOURCE_NAMES[k]}: ${fmtUsd(p.sources[k])}`);
  const conf = (p.confidence ?? 0).toFixed(2);
  const title = [
    ...(long.length ? long : ["No source prices on file"]),
    `Confidence ${conf} (0 to 1)`,
    `Priced ${new Date(p.priced_at).toLocaleString()}`,
    p.notes || "",
  ].filter(Boolean).join("\n");
  const line = [short.length ? short.join(" · ") : "no source prices", `conf ${conf}`, ago(p.priced_at)].join(" · ");
  const why = p.flagged
    ? `<span class="flag-note">needs review${p.outlier ? ": sources disagree" : ""}${p.notes ? " · " + esc(p.notes) : ""}</span>`
    : "";
  return `<span class="prov" title="${esc(title)}">${line}</span>${why}`;
}

function thumbHtml(row, cls = "inv-thumb") {
  if (row.image_url) return `<img class="${cls}" src="${esc(row.image_url)}" alt="" loading="lazy">`;
  if (row.crop_path) return `<img class="${cls}" src="/${esc(row.crop_path)}" alt="" loading="lazy">`;
  return `<div class="${cls} ph"></div>`;
}

function cardLine(row) {
  const set = row.set_name || (row.set_code ? String(row.set_code).toUpperCase() : "");
  return `<div class="inv-name">${esc(row.name)}${row.is_holo ? ' <span class="holo">holo</span>' : ""}</div>
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
    ? "You'll go to Stripe to pay by card. The card moves to your library once payment clears."
    : "No card payments are set up here, so you pay the seller directly. The card moves to your library when the seller marks it paid.";
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
  toast("Order placed. It shows under Purchased once the seller confirms payment.");
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
