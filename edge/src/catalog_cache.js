// Cache only immutable published snapshots, never an active pointer or user data.
// Every lookup checks the current primary database version and visibility.
export class SnapshotCache {
  constructor(maxBytes=4*1024**2,maxEntries=128){this.maxBytes=maxBytes;this.maxEntries=maxEntries;this.bytes=0;this.entries=new Map();}
  get(key){const item=this.entries.get(key);if(!item)return;this.entries.delete(key);this.entries.set(key,item);return item.value;}
  delete(key){const old=this.entries.get(key);if(old){this.bytes-=old.bytes;this.entries.delete(key);}}
  set(key,value){
    this.delete(key);const bytes=new TextEncoder().encode(JSON.stringify(value)).byteLength;
    if(bytes>this.maxBytes)return value;
    while(this.entries.size>=this.maxEntries || this.bytes+bytes>this.maxBytes)this.delete(this.entries.keys().next().value);
    const freeze=x=>{if(x && typeof x==='object' && !Object.isFrozen(x)){for(const child of Object.values(x))freeze(child);Object.freeze(x);}return x;};
    this.entries.set(key,{bytes,value:freeze(value)});this.bytes+=bytes;return value;
  }
}
const scopes=new WeakMap();
export function cacheFor(database){let cache=scopes.get(database);if(!cache){cache=new SnapshotCache();scopes.set(database,cache);}return cache;}
export async function cachedFolder(database,id,generation,read){
  const cache=cacheFor(database),key='folder:'+id,old=cache.get(key);
  const row=await read("SELECT generation,CASE WHEN generation=? THEN NULL ELSE payload END payload FROM folders WHERE generation=coalesce(?,(SELECT value FROM config WHERE key='active')) AND id=?",old?.generation||null,generation||null,id);
  if(!row){cache.delete(key);return null;}
  if(old && row.generation===old.generation)return old.folder;
  const folder=JSON.parse(row.payload);cache.set(key,{generation:row.generation,folder});return folder;
}
export async function cachedPublicPage(database,page,read){
  const cache=cacheFor(database),key='page:'+page,old=cache.get(key);
  const row=await read(`WITH live AS (
    SELECT a.value generation,s.value summary FROM config a LEFT JOIN config s ON s.key='stats' WHERE a.key='active'
  ) SELECT generation,summary,CASE WHEN generation=? THEN NULL ELSE (
    SELECT json_group_array(json_object('id',id,'title',title,'n',n)) FROM (
      SELECT id,title,json_array_length(payload,'$.items') n FROM folders
      WHERE generation=(SELECT generation FROM live) ORDER BY rank DESC,ordinal LIMIT 10 OFFSET ?
    )) END items FROM live`,old?.generation||null,(page-1)*10);
  if(!row){cache.delete(key);return null;}
  if(old && row.generation===old.generation)return old;
  return cache.set(key,{generation:row.generation,summary:JSON.parse(row.summary||'{}'),selected:JSON.parse(row.items||'[]'),total:Number(JSON.parse(row.summary||'{}').folders||0)});
}
