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
