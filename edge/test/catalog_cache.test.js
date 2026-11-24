import {test} from 'node:test';
import assert from 'node:assert/strict';
import {SnapshotCache,cacheFor} from '../src/catalog_cache.js';
test('snapshot cache enforces byte/entry budgets and isolates databases',()=>{
 const a={},b={};assert.notEqual(cacheFor(a),cacheFor(b));
 const cache=new SnapshotCache(500,2);cache.set('a',{items:[{name:'a'}]});cache.set('b',{items:[]});cache.get('a');cache.set('c',{items:[]});
 assert.equal(cache.get('b'),undefined);assert.ok(Object.isFrozen(cache.get('a').items[0]));
 cache.set('huge',{data:'x'.repeat(1000)});assert.equal(cache.get('huge'),undefined);assert.ok(cache.bytes<=500);
});
