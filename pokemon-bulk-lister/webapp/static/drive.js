/* Google Drive file picker, shared by the Scan and Library pages.

   openDrivePicker({ kind: "photos" | "sheets", multiple, title, onPick(files) })

   Lists the signed-in account's Drive through this app's own endpoints
   (read-only). If the account has not connected Drive yet, the dialog offers
   to, and Google sends the browser back to this page afterwards. */
"use strict";

(function () {
  const h = (s) => String(s ?? "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]));

  async function get(path) {
    const res = await fetch(path);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || `request failed (${res.status})`), data, { status: res.status });
    return data;
  }

  let dlg, opts, trail, picked, listing, shareWith, startId;

  function build() {
    if (dlg) return;
    const style = document.createElement("style");
    style.textContent = `
      #drive-dialog { width: min(640px, 94vw); border: 1px solid var(--border, #e2e2e2); border-radius: 8px; padding: 16px; }
      #drive-dialog h2 { margin: 0 0 8px; font-size: 16px; }
      #drive-dialog .drive-link { display: flex; gap: 6px; margin-top: 6px; }
      #drive-dialog .drive-more { margin-top: 8px; font-size: 13px; color: var(--muted, #6b7280); }
      #drive-dialog .drive-more summary { cursor: pointer; }
      #drive-dialog .drive-tools { display: flex; gap: 12px; align-items: center; font-size: 13px; margin-bottom: 6px; min-height: 18px; }
      #drive-dialog .drive-tools button { background: none; border: 0; padding: 0; color: var(--accent, #2563eb); cursor: pointer; font: inherit; }
      #drive-dialog .drive-tools .said { color: var(--muted, #6b7280); }
      #drive-dialog .drive-link input { flex: 1; min-width: 0; padding: 5px 8px; border: 1px solid var(--border, #e2e2e2); border-radius: 6px; font: inherit; }
      #drive-dialog .drive-trail { font-size: 13px; margin-bottom: 6px; }
      #drive-dialog .drive-trail button, #drive-dialog .drive-row button.folder { background: none; border: 0; padding: 0; color: var(--accent, #2563eb); cursor: pointer; font: inherit; }
      #drive-dialog ul { list-style: none; margin: 0; padding: 0; max-height: 52vh; overflow: auto; border: 1px solid var(--border, #e2e2e2); border-radius: 6px; }
      #drive-dialog .drive-row { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-bottom: 1px solid var(--border, #e2e2e2); font-size: 13px; }
      #drive-dialog .drive-row:last-child { border-bottom: 0; }
      #drive-dialog .drive-row label { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; cursor: pointer; }
      #drive-dialog .drive-row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      #drive-dialog .drive-row .meta { margin-left: auto; color: var(--muted, #6b7280); font-variant-numeric: tabular-nums; white-space: nowrap; }
      #drive-dialog .drive-foot { display: flex; gap: 8px; justify-content: flex-end; align-items: center; margin-top: 10px; }
      #drive-dialog .drive-foot .count { margin-right: auto; color: var(--muted, #6b7280); font-size: 13px; }
      #drive-dialog .drive-note { color: var(--muted, #6b7280); font-size: 13px; padding: 14px 8px; }`;
    document.head.appendChild(style);
    dlg = document.createElement("dialog");
    dlg.id = "drive-dialog";
    dlg.innerHTML = `<h2></h2>
      <div class="drive-trail"></div>
      <div class="drive-tools"></div>
      <div class="drive-body"></div>
      <details class="drive-more"><summary>Folder not listed? Open it by link</summary>
        <form class="drive-link"><input type="url" name="link" placeholder="Paste a Drive folder link" aria-label="Drive folder link">
          <button type="submit">Open</button></form></details>
      <div class="drive-foot"><span class="count"></span>
        <button type="button" data-drive="cancel">Cancel</button>
        <button type="button" data-drive="use" class="primary btn-primary btn" disabled>Import</button></div>`;
    document.body.appendChild(dlg);
    dlg.addEventListener("click", onClick);
    dlg.addEventListener("change", onChange);
    dlg.querySelector(".drive-link").addEventListener("submit", onLink);
  }

  const size = (n) => n == null ? "" : n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;

  function note(html) { dlg.querySelector(".drive-body").innerHTML = `<p class="drive-note">${html}</p>`; }

  function drawTrail() {
    dlg.querySelector(".drive-trail").innerHTML = trail.map((f, i) =>
      i === trail.length - 1 ? `<strong>${h(f.name)}</strong>`
        : `<button type="button" data-drive="up" data-depth="${i}">${h(f.name)}</button>`).join(" / ");
  }

  // Above the list: tick everything here, and make this folder the place
  // the picker opens at from now on.
  function drawTools() {
    const here = trail[trail.length - 1];
    const real = trail.length > 1 && here.id !== "shared-with-me";
    const parts = [];
    if (opts.multiple && listing.files.length) {
      const all = listing.files.every((f) => picked.has(f.id));
      parts.push(`<button type="button" data-drive="all">${all ? "Clear selection here" : `Select all ${listing.files.length} here`}</button>`);
    }
    if (real) {
      parts.push(here.id === startId
        ? `<span class="said">The picker opens in this folder.</span> <button type="button" data-drive="unpin">Stop</button>`
        : `<button type="button" data-drive="pin">Always open in this folder</button>`);
    }
    dlg.querySelector(".drive-tools").innerHTML = parts.join("");
  }

  async function pin(link) {
    const res = await fetch("/api/drive/folder", link
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ link }) }
      : { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (res.ok) startId = data.start_folder ? data.start_folder.id : null;
    drawTools();
  }

  function drawFoot() {
    const n = picked.size;
    dlg.querySelector(".count").textContent = opts.multiple && n ? `${n} selected` : "";
    dlg.querySelector('[data-drive="use"]').disabled = n === 0;
  }

  async function load(more) {
    const here = trail[trail.length - 1];
    drawTrail();
    if (!more) note("Loading…");
    try {
      const page = await get(`/api/drive/files?kind=${opts.kind}&folder=${encodeURIComponent(here.id)}`
        + (more ? `&page=${encodeURIComponent(more)}` : ""));
      listing = more ? { folders: listing.folders.concat(page.folders), files: listing.files.concat(page.files), next: page.next } : page;
    } catch (err) {
      if (err.reconnect || err.status === 409) return offerConnect();
      return note(h(err.message));
    }
    const top = trail.length === 1 && !shareWith ? [{ id: "shared-with-me", name: "Shared with me" }] : [];
    const rows = top.concat(listing.folders).map((f) =>
      `<li class="drive-row"><button type="button" class="folder" data-drive="open" data-id="${h(f.id)}" data-name="${h(f.name)}">📁 ${h(f.name)}</button></li>`)
      .concat(listing.files.map((f) =>
        `<li class="drive-row"><label><input type="${opts.multiple ? "checkbox" : "radio"}" name="drive-file" value="${h(f.id)}" ${picked.has(f.id) ? "checked" : ""}>
           <span class="name">${h(f.name)}</span></label><span class="meta">${size(f.size)}</span></li>`));
    drawTools();
    const what = opts.kind === "photos" ? "photos" : "Google Sheets or CSV files";
    dlg.querySelector(".drive-body").innerHTML = rows.length
      ? `<ul>${rows.join("")}</ul>` + (listing.next ? `<p class="drive-note"><button type="button" data-drive="more">Show more</button></p>` : "")
      : `<p class="drive-note">No ${what} in this folder.${shareWith && trail.length === 1
          ? `<br><br>Paste the link of a folder shared as “anyone with the link” below. A private folder has to be shared with <strong>${h(shareWith)}</strong> first.` : ""}</p>`;
  }

  async function offerConnect() {
    let status = {};
    try { status = await get("/api/drive/status"); } catch (err) {
      return note(err.status === 401 ? `<a href="/login?next=${encodeURIComponent(location.pathname)}">Log in</a> to use Google Drive.` : h(err.message));
    }
    if (!status.configured) {
      return note("Google Drive is not set up on this install. It needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (see the README).");
    }
    note(`Connect your Google Drive to pick files from it. Access is read-only: this app cannot change or delete anything there.
      <br><br><a class="btn primary" href="/api/drive/connect?next=${encodeURIComponent(location.pathname)}">Connect Google Drive</a>`);
  }

  // Open a folder by its link (your own, one shared with you, or a public
  // one) and start there next time.
  async function onLink(e) {
    e.preventDefault();
    const input = e.target.link;
    if (!input.value.trim()) return;
    try {
      const res = await fetch("/api/drive/folder", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ link: input.value }) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw Object.assign(new Error(data.error || "could not open that folder"), data, { status: res.status });
      input.value = "";
      startId = data.start_folder.id;
      dlg.querySelector(".drive-more").open = false;
      trail = [trail[0], data.start_folder];
      load();
    } catch (err) {
      if (err.reconnect) return offerConnect();
      note(h(err.message));
    }
  }

  function onChange(e) {
    if (e.target.name !== "drive-file") return;
    const file = listing.files.find((f) => f.id === e.target.value);
    if (!opts.multiple) picked.clear();
    if (e.target.checked) picked.set(file.id, file); else picked.delete(file.id);
    drawFoot(); drawTools();
  }

  function onClick(e) {
    const btn = e.target.closest("[data-drive]");
    if (!btn) return;
    const act = btn.dataset.drive;
    if (act === "cancel") dlg.close();
    else if (act === "open") { trail.push({ id: btn.dataset.id, name: btn.dataset.name }); load(); }
    else if (act === "up") { trail = trail.slice(0, Number(btn.dataset.depth) + 1); load(); }
    else if (act === "more") load(listing.next);
    else if (act === "pin") pin(trail[trail.length - 1].id);
    else if (act === "unpin") pin(null);
    else if (act === "all") {
      const all = listing.files.every((f) => picked.has(f.id));
      for (const f of listing.files) { if (all) picked.delete(f.id); else picked.set(f.id, f); }
      dlg.querySelectorAll('input[name="drive-file"]').forEach((box) => { box.checked = !all; });
      drawFoot(); drawTools();
    }
    else if (act === "use") {
      const files = Array.from(picked.values());
      dlg.close();
      opts.onPick(files);
    }
  }

  window.openDrivePicker = async function (options) {
    build();
    opts = options;
    // An install with a service account has no Drive of its own to show:
    // the starting point is whatever was shared with its address.
    const status = await get("/api/drive/status").catch(() => ({}));
    shareWith = status.mode === "service_account" ? status.share_with : null;
    trail = shareWith ? [{ id: "shared-with-me", name: "Shared with this app" }] : [{ id: "root", name: "My Drive" }];
    startId = status.start_folder ? status.start_folder.id : null;
    if (status.start_folder) trail.push(status.start_folder);
    dlg.querySelector(".drive-tools").innerHTML = "";
    picked = new Map();
    listing = { folders: [], files: [], next: null };
    dlg.querySelector("h2").textContent = options.title || "Google Drive";
    drawFoot();
    dlg.showModal();
    load();
  };
})();
