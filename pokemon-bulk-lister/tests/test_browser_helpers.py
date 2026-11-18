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
assert.match(cell, /eBay asking \(median\) \$380\.00, 4 listings/);
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
