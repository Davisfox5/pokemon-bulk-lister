"""Exercise Scan's asynchronous autosave lifecycle with its actual browser JS."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed for browser helper checks")
def test_autosave_uses_canonical_membership_and_drains_cards_ready_during_a_request():
    source = Path(__file__).resolve().parents[1] / "webapp/static/app.js"
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const status = {innerHTML:''};
const context = {document:{addEventListener(){},querySelector(){return status;}}, TextEncoder, URLSearchParams};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('// ---------------------------------------------------------------- init')[0], context);
// Rendering and price discovery are separate concerns. Keep the real card loader
// and autosave coordinator, replacing only their DOM and unrelated pricing work.
vm.runInContext('renderStats = () => {}; renderCards = () => {}; priceWaitingCards = () => {};',context);
let server = [
 {id:1,name:'Pikachu',final_price:2,id_confidence:.97,in_library:false,sold:false},
 {id:2,name:'Eevee',final_price:null,id_confidence:.97,in_library:false,sold:false},
 {id:3,name:'Sold card',final_price:3,id_confidence:.97,in_library:false,sold:true},
 {id:4,name:'Already saved',final_price:4,id_confidence:.97,in_library:true,sold:false},
 {id:5,name:'Uncertain identity',final_price:5,id_confidence:.2,in_library:false,sold:false}
];
const posts=[], pending=[];
context.api = (path,opts={}) => {
 if (path.startsWith('/api/cards?')) return Promise.resolve({cards:JSON.parse(JSON.stringify(server)),stats:{}});
 assert.equal(path,'/api/inventory/promote');
 const body=JSON.parse(opts.body);
 assert.equal(body.auto,true);
 posts.push(body.card_ids);
 return new Promise((resolve,reject)=>pending.push({resolve,reject}));
};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await context.loadCards();
 assert.deepEqual(posts,[[1]]);
 assert.equal(vm.runInContext('state.cards[0].in_library',context),false,'pending request must not imply saved');
 // Another price finishes while the first save is still in flight.
 server[1].final_price=7;
 await context.loadCards();
 assert.deepEqual(posts,[[1]],'only one save request may run at once');
 server[0].in_library=true;
 pending.shift().resolve({promoted:1,held_for_a_look:0});
 await settle();
 assert.equal(vm.runInContext('state.cards[0].in_library',context),true,'membership comes from the next GET');
 assert.deepEqual(posts,[[1],[2]],'a newly ready card must drain after the pending request');
 // The server may hold an item whose eligibility changed after Scan loaded it.
 pending.shift().resolve({promoted:0,held_for_a_look:1});
 await settle();
 assert.equal(vm.runInContext('state.cards[1].in_library',context),false,'held card must remain unsaved');
 for(let i=0;i<3;i++){await context.loadCards();await context.addPricedToLibrary();}
 assert.deepEqual(posts,[[1],[2]],'held/saved/sold/uncertain cards must not cause idle repeat requests');
 // A failed first chunk must not suppress cards in a later, unsent chunk.
 server=Array.from({length:51},(_,i)=>({id:i+10,name:'Card '+i,final_price:2,id_confidence:.97,in_library:false,sold:false}));
 await context.loadCards();
 assert.equal(posts[2].length,50);
 assert.deepEqual(posts[2],Array.from({length:50},(_,i)=>i+10));
 pending.shift().reject(Object.assign(new Error('temporary failure'),{status:500}));
 await settle();
 assert.deepEqual(posts[3],[60],'the next unsent chunk must still be attempted');
 server[50].in_library=true;
 pending.shift().resolve({promoted:1,held_for_a_look:0});
 await settle();
 await context.loadCards();
 await context.addPricedToLibrary();
 assert.equal(posts.length,4,'a failed chunk must not create an idle retry loop');
})().catch(err=>{console.error(err);process.exitCode=1;});
'''
    done = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
