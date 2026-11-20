"""Exercise the actual browser CSV chunker without installing a JS framework."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for browser helper checks')
def test_csv_chunks_preserve_quoted_records_and_fit_json_uploads():
    source = Path(__file__).resolve().parents[1] / 'webapp/static/app.js'
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8').split('// ---------------------------------------------------------------- init')[0];
const context = {document: {addEventListener() {}}, TextEncoder, URLSearchParams};
vm.createContext(context);
vm.runInContext(source, context);
const split = (text, limit) => Array.from(context.csvParts(text, limit));
const header = 'Name,Notes\r\n';
const rows = ['Pikachu,"line one\nline two"\r\n', 'Eevee,"a ""quoted"" note"\r\n', 'モンメン,かわいい\r\n'];
const text = header + rows.join('');
const parts = split(text, 60);
assert(parts.length > 1);
assert.equal(parts.map(p => p.slice(header.length)).join(''), rows.join(''));
for (const part of parts) {
  assert(part.startsWith(header));
  assert(Buffer.byteLength(JSON.stringify(part), 'utf8') - 2 <= 60);
}
assert(parts.some(p => p.includes(rows[0])));
assert(parts.some(p => p.includes(rows[1])));
assert.throws(() => split(header + 'Pikachu,' + 'x'.repeat(80), 60), /row is too large/);
assert.throws(() => split(header + 'Pikachu,"' + 'x'.repeat(80), 60), /unclosed quote/);
assert.deepEqual(split('Name\nPikachu', 100), ['Name\nPikachu']);
'''
    done = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for browser helper checks')
def test_price_provenance_keeps_specific_reasons_and_escapes_imported_text():
    source = Path(__file__).resolve().parents[1] / 'webapp/static/common.js'
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const context = {document:{addEventListener(){}}, setTimeout, clearTimeout};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
for (const reason of ['Japanese card. This price is the English printing.', 'Card number was not used. This may be a different printing.', 'From your spreadsheet (owner estimate)', 'Finish not checked.']) {
 const html = context.provenanceHtml({priced_at:new Date().toISOString(),sources:{},confidence:.2,flagged:true,notes:reason});
 assert(html.includes(reason), html);
 assert(html.includes('20% confidence'));
}
const html = context.provenanceHtml({priced_at:new Date().toISOString(),confidence:.2,flagged:true,notes:'<img src=x onerror=alert(1)>'});
assert(!html.includes('<img'));
assert(html.includes('&lt;img'));
assert(context.cardLine({name:'Pikachu',printing:'reverseHolofoil',is_holo:false}).includes('reverse holo'));
'''
    done = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for browser helper checks')
def test_scan_shows_ebay_asking_and_thin_sold_prices_apart_from_the_estimate():
    source = Path(__file__).resolve().parents[1] / 'webapp/static/app.js'
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8').split('// ---------------------------------------------------------------- init')[0];
const context = {document: {addEventListener() {}}, TextEncoder, URLSearchParams};
vm.createContext(context);
vm.runInContext(source, context);
const card = {ebay_median_30d: null, ebay_match: {
  sold: {count: 2, results: 9, median: 310, usable: false, url: 'https://www.ebay.com/sch/i.html?_nkw=x&LH_Sold=1'},
  asking: {count: 4, results: 20, median: 380, usable: true, url: 'https://www.ebay.com/sch/i.html?_nkw=x&LH_BIN=1'}}};
const cell = context.ebayCellHtml(card);
assert.match(cell, /\$310\.00 median of 2 sales, too few to price from/);
assert.match(cell, /\$380\.00<div class="muted small">eBay asking \(median\), 4 listings/);
const modal = context.ebayModalHtml(card);
assert.match(modal, /eBay sold<strong>2 sales, too few<\/strong>/);
assert.match(modal, /eBay asking \(median\)<strong>\$380\.00<\/strong>/);
assert.match(modal, /2 matched of 9 results/);
assert.match(modal, /See this search on eBay/);
const none = context.ebayModalHtml({ebay_median_30d: 12.5});
assert.match(none, /eBay sold<strong>\$12\.50<\/strong>/);
assert.doesNotMatch(none, /matched of/);
'''
    done = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr



@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for browser helper checks')
def test_ebay_search_with_no_exact_matches_still_says_so_and_links_the_search():
    source = Path(__file__).resolve().parents[1] / 'webapp/static/app.js'
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8').split('// ---------------------------------------------------------------- init')[0];
const context = {document: {addEventListener() {}}, TextEncoder, URLSearchParams};
vm.createContext(context);
vm.runInContext(source, context);
const zero = {ebay_median_30d: null, ebay_match: {sold: null,
  asking: {count: 0, results: 12, median: null, usable: false, url: 'https://www.ebay.com/sch/i.html?_nkw=Togetic&LH_BIN=1'}}};
assert.match(context.ebayCellHtml(zero), /no exact eBay matches/);
const modal = context.ebayModalHtml(zero);
assert.match(modal, /eBay asking \(median\)<strong><span class="muted small">no exact eBay matches/);
assert.match(modal, /no exact eBay matches \(0 matched of 12 results\)/);
assert.match(modal, /See this search on eBay/);
// Not searched at all: a dash, no eBay rows.
assert.match(context.ebayCellHtml({}), /—/);
assert.doesNotMatch(context.ebayModalHtml({}), /asking/);
// The live Dedenne result: asking median in the table and the editor.
const dedenne = {tcgplayer_market: 0.13, cardmarket_trend_usd: 0.09, final_price: 0.11, needs_review: 0,
  pricing_notes: '', ebay_median_30d: null, ebay_match: {sold: null, asking: {count: 46, results: 84,
  median: 1.42, usable: true, url: 'https://www.ebay.com/sch/i.html?_nkw=Dedenne+67%2F172&LH_BIN=1'}}};
assert.match(context.ebayCellHtml(dedenne), /\$1\.42<div class="muted small">eBay asking \(median\), 46 listings/);
const block = context.modalPricesHtml(dedenne);
assert.match(block, /eBay asking \(median\)<strong>\$1\.42/);
assert.match(block, /46 matched of 84 results/);
assert.match(block, /Price estimate<strong>\$0\.11/);
// A fresh price reply replaces an old note.
const stale = context.modalPricesHtml({...dedenne, needs_review: 1, pricing_notes: 'The price lookup failed. Try pricing again.'});
assert.match(stale, /The price lookup failed/);
assert.doesNotMatch(context.modalPricesHtml(dedenne), /price lookup failed/);
"""
    done = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_refresh_price_rewrites_the_editor_and_keeps_it_open():
    source = (Path(__file__).resolve().parents[1] / 'webapp/static/app.js').read_text()
    body = source[source.index('async function saveEdit('):source.index('async function aiIdentify(')]
    priced = body[body.index('if (priceAfter) {'):body.index('} else {')]
    assert 'modalPricesHtml(updated)' in priced and 'closeEdit()' not in priced and 'return;' in priced
