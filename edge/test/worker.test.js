import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import worker from '../src/worker.js';
import {order,albums,mediaCall,classify,validateFolder} from '../src/model.js';
class DB {
  constructor(){this.sql=new DatabaseSync(':memory:');this.sql.exec(readFileSync(new URL('../migrations/0001_edge.sql',import.meta.url),'utf8'));this.sql.exec(readFileSync(new URL('../migrations/0002_cover_cache.sql',import.meta.url),'utf8'));this.sql.exec(readFileSync(new URL('../migrations/0003_latency.sql',import.meta.url),'utf8'));this.sql.exec(readFileSync(new URL('../migrations/0004_timing.sql',import.meta.url),'utf8'));}
  prepare(query){const sql=this.sql;let params=[];const statement={reading:/^\s*(SELECT|WITH)/i.test(query),bind(...values){params=values;return statement;},async first(){return sql.prepare(query).get(...params)||null;},async all(){return {results:sql.prepare(query).all(...params)};},async run(){const result=sql.prepare(query).run(...params);return {meta:{changes:Number(result.changes)}};}};return statement;}
  async batch(statements){this.sql.exec('BEGIN');try{const out=[];for(const s of statements)out.push(s.reading?await s.all():await s.run());this.sql.exec('COMMIT');return out;}catch(e){this.sql.exec('ROLLBACK');throw e;}}
}
function fixture(t){
  const db=new DB(),queue=[],calls=[];let fault=null;
  const old=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{
    if(url.includes('/file/bot'))return new Response(new Uint8Array([1,2,3]),{headers:{'content-length':'3'}});
    const method=url.split('/').at(-1);let body;
    if(typeof init.body==='string')body=JSON.parse(init.body);
    else{body={multipart:true};const reader=init.body.getReader();while(!(await reader.read()).done){/* bounded synthetic image stream */}}
    calls.push({method,body});
    if(fault && fault.method===method){const f=fault;fault=null;if(f.type==='network')throw Error('timeout');return Response.json({ok:false,error_code:429,parameters:{retry_after:1}});}
    if(method==='getFile')return Response.json({ok:true,result:{file_path:'documents/source.jpg',file_size:3}});
    if(method==='sendPhoto')return Response.json({ok:true,result:{message_id:calls.length,photo:[{file_id:'cached-cover-photo'}]}});
    if(method==='getMe')return Response.json({ok:true,result:{id:99}});
    return Response.json({ok:true,result:method==='sendMediaGroup'?body.media.map((_,i)=>({message_id:calls.length+i})): {message_id:calls.length}});
  };
  t.after(()=>{globalThis.fetch=old;db.sql.close();});
  const env={DB:db,WORK:{async send(body){queue.push(body);}},BOT_TOKEN:'private',WEBHOOK_SECRET:'secret',SYNC_TOKEN:'sync',OWNER_IDS:'1',RELAY_USER_ID:'2'};
  const api=async(path,body)=>worker.fetch(new Request('https://test/api/'+path,{method:'POST',headers:{authorization:'Bearer sync','content-type':'application/json'},body:JSON.stringify(body||{})}),env);
  const webhook=async(raw,secret='secret')=>worker.fetch(new Request('https://test/webhook',{method:'POST',headers:{'x-telegram-bot-api-secret-token':secret},body:JSON.stringify(raw)}),env);
  const drain=async()=>{let limit=30;while(queue.length && limit--){db.sql.exec("UPDATE events SET next_at=0");const body=queue.shift();await worker.queue({messages:[{body,ack(){},retry(){throw Error('unexpected infrastructure retry');}}]},env);}assert.ok(limit>0,'queue must settle');};
  return {db,env,api,webhook,drain,calls,fault(f){fault=f;}};
}
const item=(kind='document',id='file')=>({kind,file_id:id,source_kind:kind,file_size:1024,caption:'',name:'IMG_0001.MOV'});
const folder=(id='0123456789abcdef',title='测试1051')=>({id,title,state:'complete',items:[item()],cover:null});
async function publish(f,folders=[folder()],id='a'.repeat(32)){
  assert.equal((await f.api('sync/begin',{id,bot_id:99,count:folders.length})).status,200);
  for(const [ordinal,x] of folders.entries())assert.equal((await f.api('sync/folder',{generation:id,ordinal,folder:x})).status,200);
  const result=await f.api('sync/publish',{generation:id});assert.equal(result.status,200);return result.json();
}
const update=(id,text)=>({update_id:id,message:{message_id:id,chat:{id:3,type:'private'},from:{id:3},text}});
const callback=(id,data,message=10)=>({update_id:id,callback_query:{id:String(id),from:{id:3},data,message:{message_id:message,chat:{id:3,type:'private'}}}});

test('stable original-first order, family-safe albums, unmodified file IDs',()=>{
  const input=[item('video','v1'),item('document','d'),item('photo','p'),item('video','v2')];
  assert.deepEqual(order(input).map(x=>x.file_id),['d','p','v1','v2']);
  assert.deepEqual(input.map(x=>x.file_id),['v1','d','p','v2']);
  assert.deepEqual(albums(input).map(x=>x.length),[1,1,2]);
  assert.equal(albums(Array.from({length:23},()=>item('video'))).length,3);
  assert.equal(albums([item('animation'),item('animation')]).length,2);
  assert.equal(mediaCall(3,[item('document')])[0],'sendDocument');
  assert.equal(mediaCall(3,[item('video'),item('video')])[0],'sendMediaGroup');
  assert.throws(()=>validateFolder({...folder(),items:[item('sticker')]}));
});
test('only private owners and relay receipts enter transfer inbox',()=>{
  const x=update(1,'/resume abc');assert.equal(classify(x,[1],2),'user');
  x.message.from.id=1;x.message.chat.id=1;assert.equal(classify(x,[1],2),'importer');
  x.message.chat.type='group';assert.equal(classify(x,[1],2),'ignore');
  const r=update(2,'');r.message.from.id=2;r.message.chat.id=2;r.message.video={};assert.equal(classify(r,[1],2),'importer');
});
test('webhook secret and sync API reject unauthenticated requests without writes',async t=>{
  const f=fixture(t);assert.equal((await f.webhook(update(1,'/start'),'wrong')).status,401);
  assert.equal((await worker.fetch(new Request('https://test/api/status'),f.env)).status,401);
  assert.equal(f.db.sql.prepare('SELECT count(*) n FROM events').get().n,0);
});
test('bot identity guard and atomic generation publication',async t=>{
  const f=fixture(t);assert.equal((await f.api('sync/begin',{id:'a'.repeat(32),bot_id:100,count:1})).status,409);
  await f.api('sync/begin',{id:'b'.repeat(32),bot_id:99,count:1});
  assert.equal((await f.api('sync/publish',{generation:'b'.repeat(32)})).status,409);
  assert.equal(f.db.sql.prepare("SELECT value FROM config WHERE key='active'").get(),undefined);
  const result=await publish(f);assert.equal(result.stats.messages,2);
});
test('initial snapshot never broadcasts history; later publication preserves edge preferences',async t=>{
  const f=fixture(t);await f.api('sync/begin',{id:'a'.repeat(32),bot_id:99,count:1});
  await f.api('sync/preferences',{generation:'a'.repeat(32),favorites:[{owner:3,job:folder().id}],subscribers:[{owner:3,enabled:1}]});
  await publish(f);assert.equal(f.db.sql.prepare("SELECT count(*) n FROM events WHERE type='notice'").get().n,0);
  await publish(f,[folder(),folder('fedcba9876543210','测试1052')],'b'.repeat(32));
  assert.equal(f.db.sql.prepare("SELECT count(*) n FROM events WHERE type='notice'").get().n,1);
  assert.equal(f.db.sql.prepare('SELECT count(*) n FROM favorites').get().n,1);
  await f.api('sync/begin',{id:'c'.repeat(32),bot_id:99,count:1});
  assert.equal((await f.api('sync/preferences',{generation:'c'.repeat(32),subscribers:[]})).status,409);
});
test('start orders directory numerically and duplicate updates do not repeat sends',async t=>{
  const f=fixture(t);await publish(f,[folder('fedcba9876543210','测试1040'),folder()]);
  await f.webhook(update(1,'/start'));await f.webhook(update(1,'/start'));await f.drain();
  const messages=f.calls.filter(x=>x.method==='sendMessage');assert.equal(messages.length,2);
  assert.ok(messages[0].body.text.includes('资源统计'));
  assert.ok(messages[1].body.text.indexOf('1051')<messages[1].body.text.indexOf('1040'));
  assert.equal(messages[1].body.reply_markup.inline_keyboard[0][0].text,'1');
  assert.ok(!JSON.stringify(messages[1].body.reply_markup).includes('我的收藏'));
});
test('folder photo and details share one message, old panels deleted after card succeeds',async t=>{
  const f=fixture(t);await publish(f,[{...folder(),cover:'cover-photo'}]);
  f.db.sql.prepare('INSERT INTO panels VALUES(3,10,9)').run();
  await f.webhook(callback(1,'open:'+folder().id));await f.drain();
  const photo=f.calls.findIndex(x=>x.method==='sendPhoto'),deleted=f.calls.findIndex(x=>x.method==='deleteMessage');
  assert.ok(photo>=0 && deleted>photo);assert.ok(f.calls[photo].body.caption.includes('内容统计'));
  assert.equal(f.db.sql.prepare('SELECT count(*) n FROM panels').get().n,0);
});
test('whole-folder delivery confirms all media before completion and ignores repeated view',async t=>{
  const f=fixture(t);const x={...folder(),items:[item('video','v1'),item('document','d'),item('video','v2')]};await publish(f,[x]);
  f.db.sql.prepare('INSERT INTO cards VALUES(3,10,?)').run(x.id);
  await f.webhook(callback(1,'view:'+x.id));await f.drain();
  const actual=f.calls.filter(x=>['sendDocument','sendMediaGroup'].includes(x.method));assert.deepEqual(actual.map(x=>x.method),['sendDocument','sendMediaGroup']);
  assert.ok(f.calls.some(x=>x.body.text?.includes('共发送 4 条消息')));
  const count=f.calls.length;await f.webhook(callback(2,'view:'+x.id));await f.drain();assert.equal(f.calls.filter(x=>['sendDocument','sendMediaGroup'].includes(x.method)).length,2);assert.ok(f.calls.length>count);
});
test('explicit 429 retries only rejected operation, never repeats successful stats',async t=>{
  const f=fixture(t);await publish(f);let n=0;const original=globalThis.fetch;
  globalThis.fetch=async(url,init)=>{if(url.endsWith('/sendMessage') && ++n===2)return Response.json({ok:false,error_code:429,parameters:{retry_after:1}});return original(url,init);};
  await f.webhook(update(1,'/start'));await f.drain();
  assert.equal(f.calls.filter(x=>x.body.text?.includes('资源统计')).length,1);
  assert.equal(f.db.sql.prepare("SELECT state FROM events WHERE id='update:1'").get().state,'complete');
});
test('ambiguous media send is not blindly replayed; user can explicitly confirm receipt',async t=>{
  const f=fixture(t);await publish(f);f.db.sql.prepare('INSERT INTO cards VALUES(3,10,?)').run(folder().id);
  f.fault({method:'sendDocument',type:'network'});
  await f.webhook(callback(1,'view:'+folder().id));await f.drain();
  const id='view:3:10';assert.equal(f.db.sql.prepare('SELECT state FROM events WHERE id=?').get(id).state,'uncertain');
  await worker.scheduled({},f.env);await f.drain();assert.equal(f.calls.filter(x=>x.method==='sendDocument').length,1);
  await f.webhook(callback(2,'recover:received:'+id));await f.drain();
  assert.equal(f.db.sql.prepare('SELECT state FROM events WHERE id=?').get(id).state,'complete');
  assert.equal(f.calls.filter(x=>x.method==='sendDocument').length,1);
});
test('importer inbox leases updates, acknowledgement removes only selected records',async t=>{
  const f=fixture(t);const x=update(10,'/resume abc');x.message.from.id=1;x.message.chat.id=1;await f.webhook(x);
  const a=await (await f.api('inbox/claim?wait=0')).json();assert.equal(a.updates.length,1);
  assert.equal((await (await f.api('inbox/claim?wait=0')).json()).updates.length,0);
  await f.api('inbox/ack',{ids:[10]});assert.equal(f.db.sql.prepare('SELECT count(*) n FROM importer_inbox').get().n,0);
});

test('search pagination and jump keep their own scope; invalid pages are explained',async t=>{
  const f=fixture(t);const folders=Array.from({length:12},(_,i)=>folder(i.toString(16).padStart(16,'0'),`关键词${i+1000}`));
  folders.push(folder('f'.repeat(16),'其他9999'));await publish(f,folders);
  await f.webhook(update(1,'关键词'));await f.drain();
  const result=f.calls.find(x=>x.method==='sendMessage');
  const next=result.body.reply_markup.inline_keyboard.at(-1).at(-1).callback_data;
  assert.ok(next.startsWith('search:'));await f.webhook(callback(2,next));await f.drain();
  const last=f.calls.filter(x=>x.method==='sendMessage').at(-1);
  assert.ok(last.body.text.includes('共 12 个文件夹'));assert.ok(last.body.text.includes('第 2/2 页'));assert.ok(!last.body.text.includes('其他'));
  await f.webhook(update(3,'/page 9999'));await f.drain();
  assert.ok(f.calls.at(-1).body.text.includes('页码超出范围'));
});
test('a newly public incomplete folder notifies only after completion',async t=>{
  const f=fixture(t);await publish(f);f.db.sql.prepare('INSERT INTO subscribers VALUES(3,1)').run();
  const extra={...folder('f'.repeat(16),'测试1052'),state:'partial'};
  await publish(f,[folder(),extra],'b'.repeat(32));assert.equal(f.db.sql.prepare("SELECT count(*) n FROM events WHERE type='notice'").get().n,0);
  await publish(f,[folder(),{...extra,state:'complete'}],'c'.repeat(32));
  assert.equal(f.db.sql.prepare("SELECT count(*) n FROM events WHERE type='notice'").get().n,1);
});

test('repeated clicks on an old favorite button cannot accidentally unsubscribe',async t=>{
  const f=fixture(t);await publish(f);
  await f.webhook(callback(1,'fav:'+folder().id));await f.drain();
  await f.webhook(callback(2,'fav:'+folder().id));await f.drain();
  assert.equal(f.db.sql.prepare('SELECT count(*) n FROM favorites').get().n,1);
  await f.webhook(callback(3,'unfav:'+folder().id));await f.drain();
  assert.equal(f.db.sql.prepare('SELECT count(*) n FROM favorites').get().n,0);
});

test('only owners may route legacy administrative callbacks to the importer',()=>{
  const raw=callback(1,'bridge_admin:retry:abc');assert.equal(classify(raw,[1],2),'user');
  raw.callback_query.from.id=1;raw.callback_query.message.chat.id=1;assert.equal(classify(raw,[1],2),'importer');
});

test('uncached bounded JPEG cover streams once and then reuses its native photo ID',async t=>{
  const f=fixture(t);const x={...folder(),items:[{...item(),source_kind:'photo',name:'IMG_0001.JPG',file_size:3}]};await publish(f,[x]);
  await f.webhook(callback(1,'open:'+x.id));await f.drain();
  assert.ok(f.calls.some(x=>x.method==='sendPhoto' && x.body.multipart));
  assert.equal(f.db.sql.prepare('SELECT photo_id FROM cover_cache').get().photo_id,'cached-cover-photo');
  await f.webhook(callback(2,'open:'+x.id));await f.drain();
  assert.equal(f.calls.filter(x=>x.method==='getFile').length,1);
  assert.equal(f.calls.filter(x=>x.method==='sendPhoto').at(-1).body.photo,'cached-cover-photo');
});

test('button updates use independent UI queue while media stays on work queue',async t=>{
 const f=fixture(t),ui=[];f.env.UI={async send(body){ui.push(body);}};await publish(f);
 await f.webhook(update(1,'/start'));
 assert.equal(ui.length,1);assert.equal(f.calls.filter(x=>x.method==='sendMessage').length,0);
 await worker.queue({messages:ui.splice(0).map(body=>({body,ack(){},retry(){throw Error('retry');}}))},f.env);
 assert.equal(f.calls.filter(x=>x.method==='sendMessage').length,2);
 const status=await (await f.api('status')).json();assert.ok(status.latency.length<=1);
});
test('parallel UI consumers cannot start two active folders for one user',async t=>{
 const f=fixture(t);const sql=f.db.sql;
 sql.prepare("INSERT INTO events(id,type,owner,payload,state,touched) VALUES('one','delivery',3,'{}','pending',0)").run();
 assert.throws(()=>sql.prepare("INSERT INTO events(id,type,owner,payload,state,touched) VALUES('two','delivery',3,'{}','pending',0)").run(),/UNIQUE/);
 sql.prepare("UPDATE events SET state='complete' WHERE id='one'").run();
 sql.prepare("INSERT INTO events(id,type,owner,payload,state,touched) VALUES('two','delivery',3,'{}','pending',0)").run();
});

test('short interactions start after persistence without waiting for broker dispatch',async t=>{
 const f=fixture(t),background=[],broker=[];f.env.UI={async send(body){broker.push(body);}};await publish(f);
 const request=new Request('https://test/webhook',{method:'POST',headers:{'x-telegram-bot-api-secret-token':'secret'},body:JSON.stringify(update(99,'/start'))});
 const response=await worker.fetch(request,f.env,{waitUntil(promise){background.push(promise);}});
 assert.equal(response.status,200);assert.equal(background.length,1);await Promise.all(background);
 assert.equal(broker.length,0);assert.equal(f.db.sql.prepare("SELECT state FROM events WHERE id='update:99'").get().state,'complete');
 assert.equal(f.calls.filter(x=>x.method==='sendMessage').length,2);
});
test('uncached image work defers to UI consumer and keeps one card receipt',async t=>{
 const f=fixture(t),background=[],broker=[];f.env.UI={async send(body){broker.push(body);}};
 const x={...folder(),items:[{...item(),source_kind:'photo',name:'a.JPG',file_size:3}]};await publish(f,[x]);
 const request=new Request('https://test/webhook',{method:'POST',headers:{'x-telegram-bot-api-secret-token':'secret'},body:JSON.stringify(callback(99,'open:'+x.id))});
 await worker.fetch(request,f.env,{waitUntil(promise){background.push(promise);}});await Promise.all(background);
 assert.equal(broker.length,1);assert.equal(f.calls.filter(x=>x.method==='sendPhoto').length,0);
 await worker.queue({messages:broker.map(body=>({body,ack(){},retry(){throw Error('retry');}}))},f.env);
 assert.equal(f.calls.filter(x=>x.method==='sendPhoto').length,1);
});

test('yielding after card send preserves panel cleanup and never resends the card',async t=>{
 const f=fixture(t),pending=[],broker=[];f.env.UI={async send(body){broker.push(body);}};
 await publish(f,[{...folder(),cover:'native-photo'}]);f.db.sql.prepare('INSERT INTO panels VALUES(3,10,9)').run();
 const originalFetch=globalThis.fetch,originalNow=Date.now;let offset=0;
 Date.now=()=>originalNow()+offset;t.after(()=>{Date.now=originalNow;});
 globalThis.fetch=async(url,init)=>{const result=await originalFetch(url,init);if(url.endsWith('/sendPhoto'))offset=6000;return result;};
 const request=new Request('https://test/webhook',{method:'POST',headers:{'x-telegram-bot-api-secret-token':'secret'},body:JSON.stringify(callback(99,'open:'+folder().id))});
 await worker.fetch(request,f.env,{waitUntil(promise){pending.push(promise);}});await Promise.all(pending);
 assert.equal(broker.length,1);assert.equal(f.db.sql.prepare('SELECT count(*) n FROM panels').get().n,1);
 await worker.queue({messages:broker.map(body=>({body,ack(){},retry(){throw Error('retry');}}))},f.env);
 assert.equal(f.calls.filter(x=>x.method==='sendPhoto').length,1);assert.equal(f.db.sql.prepare('SELECT count(*) n FROM panels').get().n,0);
});

test('warm public snapshots immediately follow publication and removal',async t=>{
 const f=fixture(t);await publish(f);await f.webhook(update(1,'/start'));await f.drain();
 await f.webhook(callback(2,'open:'+folder().id));await f.drain();
 await publish(f,[folder('f'.repeat(16),'新目录1052')],'b'.repeat(32));
 await f.webhook(update(3,'/start'));await f.drain();
 const page=f.calls.filter(x=>x.method==='sendMessage' && x.body.text.includes('文件夹列表')).at(-1);
 assert.ok(page.body.text.includes('新目录1052'));assert.ok(!page.body.text.includes('测试1051'));
 await f.webhook(callback(4,'open:'+folder().id));await f.drain();assert.ok(f.calls.at(-1).body.text.includes('尚未就绪或已移除'));
});
test('warm directory cache never freezes favorite choices',async t=>{
 const f=fixture(t);await publish(f);await f.webhook(update(1,'/start'));await f.drain();
 await f.webhook(callback(2,'fav:'+folder().id));await f.drain();await f.webhook(update(3,'/favorites'));await f.drain();
 assert.ok(f.calls.filter(x=>x.method==='sendMessage').at(-1).body.text.includes('共 1 个文件夹'));
 await f.webhook(callback(4,'unfav:'+folder().id));await f.drain();await f.webhook(update(5,'/favorites'));await f.drain();
 assert.ok(f.calls.filter(x=>x.method==='sendMessage').at(-1).body.text.includes('共 0 个文件夹'));
});
test('slow cosmetic callback acknowledgement does not block the actual card',async t=>{
 const f=fixture(t);await publish(f,[{...folder(),cover:'photo'}]);let release;
 const original=globalThis.fetch;
 globalThis.fetch=(url,init)=>url.endsWith('/answerCallbackQuery')?new Promise(resolve=>{release=()=>resolve(Response.json({ok:true,result:true}));}):original(url,init);
 await f.webhook(callback(1,'open:'+folder().id));const body={id:'update:1'};
 const processing=worker.queue({messages:[{body,ack(){},retry(){throw Error('retry');}}]},f.env);
 for(let i=0;i<30 && !f.calls.some(x=>x.method==='sendPhoto');i++)await new Promise(resolve=>setImmediate(resolve));
 assert.ok(f.calls.some(x=>x.method==='sendPhoto'));assert.equal(typeof release,'function');release();await processing;
});
