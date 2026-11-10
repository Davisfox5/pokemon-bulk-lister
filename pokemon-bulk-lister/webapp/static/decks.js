/* Decks: popular and saved lists ranked against the library, then the
   selected deck card by card: have, missing, price, where to get it.
   Helpers come from common.js. */
"use strict";

const dk = { lists: null, ref: null, detail: null, missingOnly: false, pollTimer: null };

/* ---------------- deck lists ---------------- */

async function loadLists() {
  const data = await api("/api/decks");
  dk.lists = data;
  $("#library-note").textContent = data.library_cards
    ? `Matched against the ${data.library_cards} cards in your library.`
    : "Your library is empty, so every deck shows 0 cards. Scan or add cards first.";
  renderLists();

  const src = data.popular_source;
  const el = $("#popular-source");
  if (!data.popular.length) {
    el.textContent = "No popular decks loaded. Click Refresh to pull them from Limitless.";
  } else {
    const events = src.tournaments
      .map((t) => `${esc(t.name)} (${esc(t.date)}, ${esc(t.players)} players)`).join("; ");
    el.innerHTML = `Source: ${esc(src.source)}, ${esc((src.format || "").toLowerCase())} format.
      Share = part of the decks entered in ${src.tournaments.length} tournament${src.tournaments.length === 1 ? "" : "s"}:
      ${events}. ${src.origin === "bundled" ? "Snapshot shipped with the app" : "Refreshed"}
      ${esc((src.fetched_at || "").slice(0, 10))}.`;
  }
}

function renderLists() {
  const data = dk.lists;
  const byClosest = (a, b) => b.completion_pct - a.completion_pct || (b.share_pct ?? 0) - (a.share_pct ?? 0);
  const popular = [...data.popular];
  if ($("#deck-sort").value === "closest") popular.sort(byClosest);
  $("#saved-list").innerHTML = data.saved.map((d) => deckRow(d, d.source === "popular" ? "saved from popular" : "imported"))
    .join("") || '<li class="muted">None yet. Save a popular deck or import a list.</li>';
  $("#popular-list").innerHTML = popular.map((d) =>
    deckRow(d, `${d.share_pct}% of decks entered (${d.decks_entered})`)).join("");
}

function deckRow(d, meta) {
  const maybe = d.maybe ? ` · ${d.maybe} to check` : "";
  return `<li data-ref="${esc(d.ref)}" class="${d.ref === dk.ref ? "active" : ""}" tabindex="0">
    <span class="dl-name">${esc(d.name)}</span>
    <span class="dl-have">${d.have}/${d.total}</span>
    <span class="bar"><span style="width:${d.completion_pct}%"></span></span>
    <span class="dl-meta">${esc(meta)} · ${d.missing ? `${d.missing} missing` : "complete"}${maybe}</span>
  </li>`;
}

function pick(ref) {
  dk.ref = ref;
  history.replaceState(null, "", "#" + ref);
  $$(".deck-list li").forEach((li) => li.classList.toggle("active", li.dataset.ref === ref));
  loadDetail().catch((err) => toast(err.message, "err"));
}

["#saved-list", "#popular-list"].forEach((sel) => {
  $(sel).addEventListener("click", (e) => {
    const li = e.target.closest("li[data-ref]");
    if (li) pick(li.dataset.ref);
  });
  $(sel).addEventListener("keydown", (e) => {
    const li = e.target.closest("li[data-ref]");
    if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pick(li.dataset.ref); }
  });
});
$("#deck-sort").addEventListener("input", renderLists);

$("#btn-refresh-popular").addEventListener("click", async () => {
  const btn = $("#btn-refresh-popular");
  btn.disabled = true; btn.textContent = "Refreshing…";
  try {
    const res = await postJson("/api/decks/popular/refresh");
    toast(`Loaded ${res.decks} popular decks from Limitless.`);
    await loadLists();
    if (dk.ref && dk.ref.startsWith("p-")) await loadDetail().catch(() => clearDetail());
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "Refresh";
  }
});

/* ---------------- deck detail ---------------- */

function clearDetail() {
  dk.ref = null; dk.detail = null;
  $("#deck-detail").innerHTML = '<p class="muted">Pick a deck to see which cards you have and which you need.</p>';
}

async function loadDetail() {
  clearTimeout(dk.pollTimer);
  if (!dk.ref) return;
  const ref = dk.ref;
  const d = await api(`/api/decks/${encodeURIComponent(ref)}`);
  if (ref !== dk.ref) return;  // the user clicked another deck meanwhile
  dk.detail = d;
  renderDetail();
  if (d.pricing_running) dk.pollTimer = setTimeout(() => loadDetail().catch(() => {}), 5000);
}

const SECTION_LABELS = { pokemon: "Pokémon", trainer: "Trainers", energy: "Energy", unknown: "Cards (list had no headings)" };

function sourceLine(d) {
  const s = d.meta && d.meta.sample;
  if (!s) return d.source === "paste" ? "Imported decklist." : "";
  const place = s.placing ? `placed ${esc(s.placing)} of ${esc(s.tournament_players)}` : "played";
  return `List by ${esc(s.player)}, who ${place} (${esc(s.record)}) at
    <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.tournament)}</a>, ${esc(s.tournament_date)}.
    ${d.meta.share_pct != null ? `${esc(d.meta.share_pct)}% of decks entered. ` : ""}Source: ${esc(d.meta.source || "Limitless TCG")}.`;
}

function renderDetail() {
  const d = dk.detail, s = d.summary, c = d.cost;
  const needPricing = c.unpriced_missing > 0;
  const covers = c.priced_missing + c.unpriced_missing;
  const costSub = !s.missing ? "nothing to buy"
    : !covers ? "only basic energy is missing"
    : `covers ${c.priced_missing} of ${covers} missing cards${c.basic_energy_missing ? ", basic energy left out" : ""}`;

  const sections = ["pokemon", "trainer", "energy", "unknown"].map((sec) => {
    const rows = d.cards.filter((card) => card.section === sec && (!dk.missingOnly || card.missing > 0 || card.maybe > 0));
    if (!rows.length) return "";
    const all = d.cards.filter((card) => card.section === sec);
    const count = all.reduce((n, card) => n + card.count, 0);
    const have = all.reduce((n, card) => n + card.have, 0);
    return `<tr class="sec-row"><td colspan="6">${SECTION_LABELS[sec]} · have ${have} of ${count}</td></tr>` +
      rows.map(cardRow).join("");
  }).join("");

  $("#deck-detail").innerHTML = `
    <div class="panel-head">
      <h2>${esc(d.name)}</h2>
      <div class="head-actions">
        ${d.saved
          ? '<button class="btn btn-ghost" data-act="delete">Delete</button>'
          : '<button class="btn" data-act="save">Save deck</button>'}
        <button class="btn" data-act="copy-missing" ${s.missing ? "" : "disabled"}>Copy missing list</button>
      </div>
    </div>
    <p class="source-note">${sourceLine(d)}</p>

    <div class="deck-tiles">
      <div class="tile ${s.missing ? "" : "done"}">
        <div class="tile-label">You have</div>
        <div class="tile-value">${s.have} / ${s.total}</div>
        <div class="tile-sub">${s.completion_pct}% of the deck</div>
      </div>
      <div class="tile">
        <div class="tile-label">Missing</div>
        <div class="tile-value">${s.missing}</div>
        <div class="tile-sub">${d.missing_lines_on_platform
          ? `${d.missing_lines_on_platform} of these cards listed on the marketplace` : "none listed on the marketplace"}</div>
      </div>
      <div class="tile ${s.maybe ? "flag" : ""}">
        <div class="tile-label">Check the printing</div>
        <div class="tile-value">${s.maybe}</div>
        <div class="tile-sub">${s.maybe ? "same name, different printing; not counted" : "nothing to check"}</div>
      </div>
      <div class="tile">
        <div class="tile-label">Estimated cost to finish</div>
        <div class="tile-value">${c.priced_missing ? fmtUsd(c.estimate) : "—"}</div>
        <div class="tile-sub">${costSub}</div>
      </div>
    </div>

    <div class="controls">
      <label class="check"><input type="checkbox" id="missing-only" ${dk.missingOnly ? "checked" : ""}> Missing only</label>
      ${d.pricing_running
        ? '<span class="muted">Looking up prices… this page updates as they arrive.</span>'
        : needPricing
          ? `<button class="btn" data-act="price">Price the ${c.unpriced_missing} unpriced missing cards</button>` : ""}
      <span class="muted">Prices: ${esc(c.source)}.</span>
    </div>

    <div class="table-scroll">
      <table class="inv-table deck-table">
        <thead><tr>
          <th class="num">Need</th><th>Card</th><th class="num">Have</th><th class="num">Missing</th>
          <th class="num">Price each</th><th>Where to get it</th>
        </tr></thead>
        <tbody>${sections || '<tr><td colspan="6" class="empty">You have every card in this deck.</td></tr>'}</tbody>
      </table>
    </div>`;
}

function cardRow(card) {
  const idx = dk.detail.cards.indexOf(card);
  const setBits = [card.set_name || card.set, card.number ? "#" + card.number : ""].filter(Boolean).join(" ");
  const other = card.have_other
    ? `<div class="inv-set">${card.have_other} from another printing</div>` : "";
  const maybe = card.maybe
    ? `<span class="flag-note">You own ${card.maybe} “${esc(card.name)}” in a different printing. Same name is not always the same card; check before counting it.</span>` : "";
  const price = card.basic_energy
    ? '<span class="prov">basic energy, not priced</span>'
    : card.price
      ? `${fmtUsd(card.price.price)}<span class="prov">${esc(card.price.basis)}</span>${provenanceHtml(card.price.provenance)}`
      : '<span class="prov">no price on file</span>';

  let get = "";
  if (card.missing > 0 || card.maybe > 0) {
    const best = card.listings[0];
    const onPlatform = best
      ? `<span>${fmtUsd(best.price)} ${esc(best.condition)} from ${esc(best.seller)}${
          best.exact ? "" : ` (${esc([best.set_name, best.card_number ? "#" + best.card_number : ""].filter(Boolean).join(" "))})`}</span>
         ${best.check_printing ? '<span class="pill warn">different printing, check the card</span>' : ""}
         <button class="btn btn-primary" data-act="buy" data-card="${idx}">Buy</button>
         ${card.listing_count > 1
           ? `<a href="/market?q=${encodeURIComponent(card.name)}">${card.listing_count - 1} more</a>` : ""}`
      : '<span class="muted">not on the marketplace</span>';
    get = `<div class="get">${onPlatform}
      <a href="${esc(card.price && card.price.tcgplayer_url || card.links.tcgplayer)}" target="_blank" rel="noopener">TCGplayer</a>
      <a href="${esc(card.links.ebay)}" target="_blank" rel="noopener">eBay</a></div>`;
  }
  return `<tr class="${card.maybe ? "flag" : card.missing ? "" : "covered"}">
    <td class="num">${card.count}</td>
    <td><div class="inv-name">${esc(card.name)}</div><div class="inv-set">${esc(setBits)}</div>${maybe}</td>
    <td class="num">${card.have}${other}</td>
    <td class="num ${card.missing ? "miss" : ""}">${card.missing || "—"}</td>
    <td class="num price-cell">${price}</td>
    <td>${get}</td>
  </tr>`;
}

$("#deck-detail").addEventListener("input", (e) => {
  if (e.target.id === "missing-only") { dk.missingOnly = e.target.checked; renderDetail(); }
});

$("#deck-detail").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn || !dk.detail) return;
  const d = dk.detail;
  try {
    if (btn.dataset.act === "save") {
      const res = await postJson("/api/decks", { popular_ref: d.ref });
      toast(`Saved ${res.name}. It keeps this exact list even after the popular decks refresh.`);
      await loadLists();
      pick(res.ref);
    } else if (btn.dataset.act === "delete") {
      const { ok } = await askConfirm({
        title: "Delete this saved deck?",
        html: `<p><strong>${esc(d.name)}</strong> is removed from your saved decks. Your cards are not affected.</p>`,
        confirm: "Delete",
      });
      if (!ok) return;
      await api(`/api/decks/${d.ref}`, { method: "DELETE" });
      toast(`Deleted ${d.name}.`);
      history.replaceState(null, "", location.pathname);
      clearDetail();
      await loadLists();
    } else if (btn.dataset.act === "copy-missing") {
      await navigator.clipboard.writeText(d.missing_list);
      toast(`Copied ${d.summary.missing} missing cards as a decklist.`);
    } else if (btn.dataset.act === "price") {
      const dry = await postJson(`/api/decks/${encodeURIComponent(d.ref)}/price-missing`, { dry_run: true });
      const { ok } = await askConfirm({
        title: `Look up prices for ${dry.to_price} cards?`,
        html: `<p>This runs ${dry.to_price} price lookups (pokemontcg.io and eBay sold listings) in the background
               and adds the cards to the shared price catalog.${dry.over_cap
                 ? ` ${dry.over_cap} more are over the per-click limit; run it again afterwards.` : ""}</p>
               <p class="muted">It takes a few seconds per card.</p>`,
        confirm: "Look up prices",
      });
      if (!ok) return;
      await postJson(`/api/decks/${encodeURIComponent(d.ref)}/price-missing`);
      toast("Price lookup started.");
      dk.detail.pricing_running = true;
      renderDetail();
      dk.pollTimer = setTimeout(() => loadDetail().catch(() => {}), 5000);
    } else if (btn.dataset.act === "buy") {
      const card = d.cards[Number(btn.dataset.card)];
      const best = card.listings[0];
      btn.disabled = true;
      const bought = await buyListing({ ...best, name: card.name }, dk.payments);
      if (bought) await loadDetail();
      btn.disabled = false;
    }
  } catch (err) {
    toast(err.message, "err");
    loadDetail().catch(() => {});
  }
});

/* ---------------- import ---------------- */

$("#btn-import").addEventListener("click", () => {
  $("#import-form").reset();
  openDialog($("#import-dialog"));
});

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = Object.fromEntries(new FormData(e.target));
  try {
    const res = await postJson("/api/decks", f);
    $("#import-dialog").close();
    toast(res.warnings.length
      ? `Imported ${res.name}. Note: ${res.warnings.join("; ")}.`
      : `Imported ${res.name}.`, res.warnings.length ? "err" : "ok");
    await loadLists();
    pick(res.ref);
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- boot ---------------- */

(async function boot() {
  try {
    api("/api/market/listings").then((m) => { dk.payments = m.payments; }).catch(() => {});
    await loadLists();
    const fromHash = decodeURIComponent(location.hash.slice(1));
    const all = [...dk.lists.saved, ...dk.lists.popular];
    if (fromHash && all.some((d) => d.ref === fromHash)) pick(fromHash);
    else if (dk.lists.library_cards && all.length) {
      // Open on the deck the library is closest to finishing.
      pick([...all].sort((a, b) => b.completion_pct - a.completion_pct)[0].ref);
    }
  } catch (err) {
    toast(err.message, "err");
  }
})();
