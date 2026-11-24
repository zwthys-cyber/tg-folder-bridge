import {coverSource,multipartCover,COVER_LIMIT} from './cover.js';
import {keyboard,button,chunks,albums,mediaCall,details,validateFolder,classify} from './model.js';
const now=()=>Math.floor(Date.now()/1000);
const json=(value,status=200)=>Response.json(value,{status});
const owners=e=>(e.OWNER_IDS||'').split(',').filter(Boolean).map(Number);
const stmt=(e,sql,...args)=>e.DB.prepare(sql).bind(...args);
const first=(e,sql,...args)=>stmt(e,sql,...args).first();
const run=(e,sql,...args)=>stmt(e,sql,...args).run();
const rows=async(e,sql,...args)=>(await stmt(e,sql,...args).all()).results;
class Retry extends Error {constructor(seconds){super('Telegram retry');this.seconds=seconds;}}
class Uncertain extends Error {}
// Keep secret comparison length-independent after hashing; never log tokens/updates.
async function same(a,b) {
  if(!a||!b)return false;
  const digest=async x=>new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(x)));
  const [x,y]=await Promise.all([digest(a),digest(b)]);let n=0;for(let i=0;i<x.length;i++)n|=x[i]^y[i];return n===0;
}
async function telegram(e,method,body,upload) {
  let response;
  try {response=await fetch(`https://api.telegram.org/bot${e.BOT_TOKEN}/${method}`,{method:'POST',...(upload||{headers:{'content-type':'application/json'},body:JSON.stringify(body)}),signal:AbortSignal.timeout(20000)});}
  catch {throw new Uncertain('Telegram network outcome unknown');}
  if(response.status>=500)throw new Uncertain('Telegram server outcome unknown');
  let value;try{value=await response.json();}catch{throw new Uncertain('Telegram response unreadable');}
  if(value.ok)return value.result;
  if(value.error_code===429)throw new Retry(Math.max(1,value.parameters?.retry_after||30));
  throw Error(`Telegram rejected ${method}: ${value.error_code||response.status}`);
}
// A successful operation is replayed from its receipt, never re-sent. A crashed
// in-flight operation is uncertain: automatic recovery cannot duplicate media.
async function send(e,id,method,body,upload) {
  let old=await first(e,'SELECT * FROM sends WHERE id=?',id);
  if(old?.state==='sent')return JSON.parse(old.response);
  if(old?.state==='failed')throw Error(old.error);
  if(old && old.state!=='retry')throw new Uncertain('Prior send requires inspection');
  const claim=old?await run(e,"UPDATE sends SET state='sending' WHERE id=? AND state='retry'",id):await run(e,"INSERT OR IGNORE INTO sends(id,state) VALUES(?,'sending')",id);
  if(!claim.meta.changes)throw new Uncertain('Send already claimed');
  try {
    const value=await telegram(e,method,body,upload);
    await run(e,"UPDATE sends SET state='sent',response=? WHERE id=?",JSON.stringify(value),id);
    return value;
  } catch(error) {
    await run(e,"UPDATE sends SET state=?,error=? WHERE id=?",error instanceof Retry?'retry':error instanceof Uncertain?'uncertain':'failed',String(error.message),id);
    throw error;
  }
}
async function signal(e,id,type,options){
  if(!type)type=(await first(e,'SELECT type FROM events WHERE id=?',id))?.type;
  await (type==='update' && e.UI?e.UI:e.WORK).send({id},options);
}
async function enqueue(e,id,type,owner,payload) {
  await run(e,"INSERT OR IGNORE INTO events(id,type,owner,payload,touched,created_ms) VALUES(?,?,?,?,?,?)",id,type,owner,JSON.stringify(payload),now(),Date.now());
  await signal(e,id,type);
}
async function active(e){return (await first(e,"SELECT value FROM config WHERE key='active'"))?.value;}
async function folder(e,id,generation){const row=await first(e,'SELECT payload FROM folders WHERE generation=? AND id=?',generation||await active(e),id);return row?JSON.parse(row.payload):null;}
async function favorite(e,uid,id){return !!await first(e,'SELECT 1 FROM favorites WHERE owner=? AND job=?',uid,id);}
function controls(f,saved){return keyboard([[button('📁 文件夹详情',`open:${f.id}`),button(saved?'⭐ 已收藏':'☆ 收藏',`${saved?'unfav':'fav'}:${f.id}`)],[button('⭐ 我的收藏','favorites:1'),button('📁 文件夹列表','list:1')]]);}
async function list(e,uid,page,favorites,call,stats=false,search='') {
  const generation=await active(e);
  if(!generation){await call('sendMessage',{chat_id:uid,text:'资源目录尚未接入，请稍后查看。'});return;}
  const condition=favorites?' AND id IN (SELECT job FROM favorites WHERE owner=?)':search?' AND instr(lower(title),lower(?))>0':'';
  const args=favorites?[uid]:search?[search]:[];
  const total=(await first(e,'SELECT count(*) AS n FROM folders WHERE generation=?'+condition,generation,...args)).n;
  const pages=Math.max(1,Math.ceil(total/10));page=Number(page)||1;
  if(!Number.isSafeInteger(page)||page<1||page>pages){await call('sendMessage',{chat_id:uid,text:`页码超出范围，请输入 1–${pages}。`});return;}
  let statsMessage=null;
  if(stats){const summary=JSON.parse((await first(e,"SELECT value FROM config WHERE key='stats'"))?.value||'{}');statsMessage=await call('sendMessage',{chat_id:uid,text:`📊 资源统计\n　• 文件夹：${summary.folders||0} 个\n　• 消息：${summary.messages||0} 条\n　• 图片：${summary.photos||0} 张\n　• 视频：${summary.videos||0} 个`});}
  const selected=await rows(e,'SELECT id,title,json_array_length(payload,\'$.items\') AS n FROM folders WHERE generation=?'+condition+' ORDER BY rank DESC,ordinal LIMIT 10 OFFSET ?',generation,...args,(page-1)*10);
  const buttons=chunks(selected.map((x,i)=>button(String(i+1),`open:${x.id}`)),5);
  let prefix=favorites?'favorites':'list';
  if(search){const digest=new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(search)));const token=Array.from(digest.slice(0,6),x=>x.toString(16).padStart(2,'0')).join('');await run(e,'INSERT OR REPLACE INTO config VALUES(?,?)',`search:${uid}:${token}`,search);prefix=`search:${token}`;}
  const nav=[];
  if(page>1)nav.push(button('⬅️ 上一页',`${prefix}:${page-1}`));
  if(pages>1)nav.push(button(`📍 ${page}/${pages} 跳转`,`jump:${prefix}`));
  if(page<pages)nav.push(button('下一页 ➡️',`${prefix}:${page+1}`));
  if(nav.length)buttons.push(nav);
  if(favorites)buttons.push([button('📁 文件夹列表','list:1')]);
  const message=await call('sendMessage',{chat_id:uid,text:`${favorites?'⭐ 我的收藏':'📁 文件夹列表'}\n\n共 ${total} 个文件夹（第 ${page}/${pages} 页）\n\n`+selected.map((x,i)=>`${i+1}. 📁 ${x.title}（${x.n} 个文件）`).join('\n\n'),reply_markup:keyboard(buttons)});
  await run(e,'INSERT OR REPLACE INTO panels VALUES(?,?,?)',uid,message.message_id,statsMessage?.message_id||null);
}
async function removePanel(e,uid,message,call){
  const panel=await first(e,'SELECT * FROM panels WHERE chat=? AND message=?',uid,message);
  if(!panel)return;
  for(const id of [message,panel.stats].filter(Boolean)) {
    // Deletion may already have succeeded when Telegram redelivers an update.
    try{await call('deleteMessage',{chat_id:uid,message_id:id});}catch(error){if(error instanceof Retry)throw error;}
  }
  await run(e,'DELETE FROM panels WHERE chat=? AND message=?',uid,message);
}
async function open(e,uid,id,origin,call) {
  const f=await folder(e,id);if(!f){await call('sendMessage',{chat_id:uid,text:'文件夹尚未就绪或已移除。'});return;}
  const saved=await favorite(e,uid,id);
  const keys=keyboard([[button('查看内容',`view:${id}`)],[button(saved?'⭐ 已收藏':'☆ 收藏',`${saved?'unfav':'fav'}:${id}`),button('⭐ 我的收藏','favorites:1')],[button('文件夹列表','list:1')]]);
  const body={chat_id:uid,reply_markup:keys};
  let photo=f.cover,upload=null;const source=coverSource(f);
  if(!photo && source){
    const cached=await first(e,'SELECT photo_id FROM cover_cache WHERE job=? AND source_id=?',id,source.file_id);
    photo=cached?.photo_id;
    if(!photo){
      try{
        const file=await telegram(e,'getFile',{file_id:source.file_id});
        if(!file.file_path || file.file_size>COVER_LIMIT)throw Error('Cover unavailable');
        const response=await fetch(`https://api.telegram.org/file/bot${e.BOT_TOKEN}/${file.file_path}`,{signal:AbortSignal.timeout(15000)});
        upload=multipartCover(response,{...body,caption:details(f)},/\.png$/i.test(source.name)?'cover.png':'cover.jpg');
      }catch{/* bounded preview failure must not prevent opening the folder */}
    }
  }
  let card;
  if(photo || upload){
    try{card=await call('sendPhoto',{...body,photo,caption:details(f)},upload);}
    catch(error){if(error instanceof Retry || error instanceof Uncertain)throw error;}
  }
  if(!card)card=await call('sendMessage',{...body,text:details(f)});
  if(source && card.photo?.length)await run(e,'INSERT OR REPLACE INTO cover_cache VALUES(?,?,?)',id,source.file_id,card.photo.at(-1).file_id);
  await run(e,'INSERT OR REPLACE INTO cards VALUES(?,?,?)',uid,card.message_id,id);
  if(origin)await removePanel(e,uid,origin,call);
}
async function userUpdate(e,event) {
  const u=JSON.parse(event.payload),m=u.message,q=u.callback_query,uid=event.owner;
  let seq=0;const call=(method,body,upload)=>send(e,`${event.id}:${seq++}`,method,body,upload);
  if(q){try{await telegram(e,'answerCallbackQuery',{callback_query_id:q.id});}catch{/* expired answers do not invalidate durable actions */}}
  let text=(m?.text||'').trim();let action=q?.data||'';
  // Previously emitted bridge buttons remain usable after migration.
  action=action.replace(/^bridge_catalog:/,'list:').replace(/^bridge_favorites:/,'favorites:').replace(/^bridge_open:/,'open:').replace(/^bridge_view:/,'view:').replace(/^bridge_fav:/,'fav:').replace(/^bridge_unfav:/,'unfav:').replace(/^bridge_notice:/,'notice:');
  if(!action && /^\/start(?:@\w+)?(?:\s|$)/.test(text))return list(e,uid,1,false,call,true);
  if(!action && /^\/page\s+\d+$/.test(text))return list(e,uid,Number(text.split(/\s+/)[1]),false,call);
  if(!action && text==='/cancel'){await run(e,'DELETE FROM config WHERE key=?',`jump:${uid}`);return call('sendMessage',{chat_id:uid,text:'已取消页码输入。'});}
  if(!action && /^\/favorites(?:@\w+)?$/.test(text))return list(e,uid,1,true,call);
  if(!action && /^\/folder\s+([a-f0-9]{16})/.test(text))return open(e,uid,text.split(/\s+/)[1],null,call);
  if(!action && /^\/(notifications|subscribe|unsubscribe)(?:@\w+)?$/.test(text))action='notice:'+(/^\/subscribe/.test(text)?'on':/^\/unsubscribe/.test(text)?'off':'settings');
  if(!action && text==='/admin' && owners(e).includes(uid)) {
    const heartbeat=Number((await first(e,"SELECT value FROM config WHERE key='importer_heartbeat'"))?.value||0);
    const pending=(await first(e,'SELECT count(*) n FROM importer_inbox')).n;
    return call('sendMessage',{chat_id:uid,text:`🛠 服务状态\n日常服务：Cloudflare\n转存程序：${now()-heartbeat<90?'在线':'离线'}\n等待转存程序处理：${pending} 条\n转存程序离线不影响已保存内容的获取。`});
  }
  if(!action && m?.reply_to_message && /^\d+$/.test(text)){
    const jump=await first(e,'SELECT value FROM config WHERE key=?',`jump:${uid}`);
    if(jump){const target=JSON.parse(jump.value);if(target.message===m.reply_to_message.message_id && now()-target.at<600)return list(e,uid,Number(text),target.favorites,call,false,target.search||'');}
  }
  const [kind,arg]=action.split(':');
  if(['list','favorites'].includes(kind)){await list(e,uid,arg,kind==='favorites',call);if(q)await removePanel(e,uid,q.message.message_id,call);return;}
  if(kind==='search'){
    const query=(await first(e,'SELECT value FROM config WHERE key=?',`search:${uid}:${arg}`))?.value;
    if(query)await list(e,uid,action.split(':')[2],false,call,false,query);
    else await call('sendMessage',{chat_id:uid,text:'搜索已过期，请重新输入关键词。'});
    if(q)await removePanel(e,uid,q.message.message_id,call);return;
  }
  if(kind==='jump'){
    const prompt=await call('sendMessage',{chat_id:uid,text:'请回复这条消息，输入要跳转的页码。',reply_markup:{force_reply:true,selective:true}});
    await run(e,'INSERT OR REPLACE INTO config VALUES(?,?)',`jump:${uid}`,JSON.stringify({message:prompt.message_id,favorites:arg==='favorites',search:arg==='search'?(await first(e,'SELECT value FROM config WHERE key=?',`search:${uid}:${action.split(':')[2]}`))?.value||'':'',at:now()}));return;
  }
  if(kind==='open')return open(e,uid,arg,q.message.message_id,call);
  if(['fav','unfav'].includes(kind)){
    if(!await folder(e,arg))return;
    // Explicit set/unset operations remain idempotent even for a second click
    // delivered under another update ID from an old Telegram keyboard.
    if(kind==='unfav')await run(e,'DELETE FROM favorites WHERE owner=? AND job=?',uid,arg);
    else await run(e,'INSERT OR IGNORE INTO favorites VALUES(?,?)',uid,arg);
    const saved=await favorite(e,uid,arg);
    if(q.message.reply_markup){
      const keys=q.message.reply_markup.inline_keyboard.map(row=>row.map(x=>/^(?:bridge_)?(?:fav|unfav):/.test(x.callback_data||'')?button(saved?'⭐ 已收藏':'☆ 收藏',`${saved?'unfav':'fav'}:${arg}`):x));
      await call('editMessageReplyMarkup',{chat_id:uid,message_id:q.message.message_id,reply_markup:keyboard(keys)});
    }
    return call('sendMessage',{chat_id:uid,text:saved?'⭐ 已收藏':'已取消收藏'});
  }
  if(kind==='view'){
    const card=await first(e,'SELECT job FROM cards WHERE chat=? AND message=?',uid,q.message.message_id);
    if(!card || card.job!==arg){await call('sendMessage',{chat_id:uid,text:'请重新打开文件夹，再点击查看内容。'});return;}
    const existing=await first(e,"SELECT id,state FROM events WHERE owner=? AND type='delivery' AND state IN ('pending','processing','uncertain') LIMIT 1",uid);
    if(existing){await call('sendMessage',{chat_id:uid,text:existing.state==='uncertain'?'上一批发送结果需要确认，请先检查聊天记录。':'正在发送内容，请等当前文件夹发送完成。'});return;}
    const id=`view:${uid}:${q.message.message_id}`;
    const f=await folder(e,arg);if(!f)return;
    await run(e,'INSERT OR IGNORE INTO views VALUES(?,?,?)',uid,q.message.message_id,id);
    await enqueue(e,id,'delivery',uid,{folder:f,cursor:0});return;
  }
  if(kind==='recover'){
    const id=action.split(':').slice(2).join(':');
    const target=await first(e,"SELECT * FROM events WHERE id=? AND owner=? AND type='delivery' AND state='uncertain'",id,uid);
    if(!target)return;
    const cursor=JSON.parse(target.payload).cursor;
    if(!['received','retry'].includes(arg))return;
    await e.DB.batch([stmt(e,"UPDATE sends SET state=?,response='{}' WHERE id=? AND state IN ('sending','uncertain')",arg==='received'?'sent':'retry',`${id}:${cursor}`),stmt(e,"UPDATE events SET state='pending',next_at=0,touched=? WHERE id=? AND state='uncertain'",now(),id)]);
    await signal(e,id);return;
  }
  if(kind==='notice'){
    if(['on','off'].includes(arg))await run(e,'INSERT OR REPLACE INTO subscribers VALUES(?,?)',uid,arg==='on'?1:0);
    const enabled=(await first(e,'SELECT enabled FROM subscribers WHERE owner=?',uid))?.enabled;
    return call('sendMessage',{chat_id:uid,text:`${enabled?'🔔 新文件夹通知：已开启':'🔕 新文件夹通知：未开启'}\n仅通知订阅后新完成的文件夹。`,reply_markup:keyboard([[button(enabled?'关闭通知':'开启通知',`notice:${enabled?'off':'on'}`)]])});
  }
  if(text && !text.startsWith('/') && text.length<=100)return list(e,uid,1,false,call,false,text);
  return call('sendMessage',{chat_id:uid,text:'发送 /start 浏览文件夹，/favorites 查看收藏，/notifications 设置新文件夹通知。'});
}
async function delivery(e,event){
  const plan=JSON.parse(event.payload),f=plan.folder,groups=albums(f.items);
  // Four Telegram calls maximum per queue invocation, below free-tier budgets.
  const end=Math.min(plan.cursor+4,groups.length+2);
  for(let i=plan.cursor;i<end;i++){
    let method,body;
    if(i===0){method='sendMessage';body={chat_id:event.owner,text:details(f,true)};}
    else if(i<=groups.length)[method,body]=mediaCall(event.owner,groups[i-1]);
    else{method='sendMessage';body={chat_id:event.owner,text:`✅ 文件夹内容发送完成\n\n共发送 ${f.items.length+1} 条消息`,reply_markup:controls(f,await favorite(e,event.owner,f.id))};}
    await send(e,`${event.id}:${i}`,method,body);
    plan.cursor=i+1;
    await run(e,'UPDATE events SET payload=?,touched=? WHERE id=?',JSON.stringify(plan),now(),event.id);
    // Respect Telegram per-chat pacing rather than blasting media groups.
    if(i+1<end)await new Promise(resolve=>setTimeout(resolve,1100));
  }
  if(plan.cursor<groups.length+2){await run(e,"UPDATE events SET state='pending',next_at=?,touched=? WHERE id=?",now()+2,now(),event.id);await e.WORK.send({id:event.id},{delaySeconds:2});return false;}
  return true;
}
async function process(e,id){
  const claim=await run(e,"UPDATE events SET state='processing',touched=?,started_ms=CASE WHEN started_ms=0 THEN ? ELSE started_ms END WHERE id=? AND state='pending' AND next_at<=?",now(),Date.now(),id,now());
  if(!claim.meta.changes)return;
  const event=await first(e,'SELECT * FROM events WHERE id=?',id);
  try{
    const done=event.type==='delivery'?await delivery(e,event):event.type==='notice'?await notice(e,event):await userUpdate(e,event);
    if(done!==false)await run(e,"UPDATE events SET state='complete',touched=?,finished_ms=? WHERE id=?",now(),Date.now(),id);
  }catch(error){
    const retry=error instanceof Retry;
    await run(e,'UPDATE events SET state=?,next_at=?,error=?,touched=? WHERE id=?',retry?'pending':error instanceof Uncertain?'uncertain':'failed',retry?now()+error.seconds:0,String(error.message).slice(0,200),now(),id);
    if(retry)await signal(e,id,event.type,{delaySeconds:Math.min(error.seconds,43200)});
    else if(error instanceof Uncertain && event.type==='delivery'){
      try{await send(e,`recovery:${id}`,'sendMessage',{chat_id:event.owner,text:'上一批发送结果未确认，请检查聊天记录后选择。',reply_markup:keyboard([[button('确认上一批已收到',`recover:received:${id}`)],[button('确认未收到，重发上一批',`recover:retry:${id}`)]])});}catch{/* state remains visible in authenticated status */}
    }
  }
}
async function notice(e,event){const f=JSON.parse(event.payload);if(!(await first(e,'SELECT enabled FROM subscribers WHERE owner=?',event.owner))?.enabled)return;await send(e,event.id,'sendMessage',{chat_id:event.owner,text:`📁 新文件夹已上架\n\n${f.title}\n共 ${f.count} 个文件`,reply_markup:keyboard([[button('查看文件夹',`open:${f.id}`)],[button('取消更新通知','notice:off')]])});}
async function api(request,e,path){
  if(!await same(request.headers.get('authorization'),`Bearer ${e.SYNC_TOKEN}`) || !e.SYNC_TOKEN)return json({error:'Unauthorized'},401);
  if(path==='/api/status')return json({active:await active(e),events:await rows(e,'SELECT state,count(*) count FROM events GROUP BY state'),latency:await rows(e,"SELECT type,count(*) samples,round(avg(started_ms-created_ms)) queue_ms,round(avg(finished_ms-started_ms)) processing_ms FROM (SELECT * FROM events WHERE created_ms>0 AND finished_ms>0 ORDER BY created_ms DESC LIMIT 30) GROUP BY type")});
  if(path==='/api/inbox/claim'){
    await run(e,"INSERT OR REPLACE INTO config VALUES('importer_heartbeat',?)",String(now()));
    const wait=Math.min(15000,Math.max(0,Number(new URL(request.url).searchParams.get('wait')??15000)||0));
    const deadline=Date.now()+wait;let entries;
    do{
      entries=await rows(e,'UPDATE importer_inbox SET lease=? WHERE id IN (SELECT id FROM importer_inbox WHERE lease<? AND expires>? ORDER BY id LIMIT 20) RETURNING id,payload',now()+120,now(),now());
      if(entries.length || Date.now()>=deadline)break;
      await new Promise(resolve=>setTimeout(resolve,Math.min(1000,deadline-Date.now())));
    }while(true);
    return json({updates:entries.map(x=>JSON.parse(x.payload))});
  }
  if(Number(request.headers.get('content-length')||0)>1000000)return json({error:'Too large'},413);
  const body=await request.json();
  if(path==='/api/inbox/ack'){for(const id of (body.ids||[]).slice(0,20))await run(e,'DELETE FROM importer_inbox WHERE id=?',id);return json({ok:true});}
  if(path==='/api/sync/begin'){
    if(!/^[a-f0-9]{32}$/.test(body.id)||!Number.isInteger(body.count)||body.count<0||body.count>100000)return json({error:'Invalid manifest'},400);
    const me=await telegram(e,'getMe',{});if(body.bot_id!==me.id)return json({error:'Bot identity mismatch'},409);
    await run(e,"INSERT OR IGNORE INTO generations(id,bot_id,expected) VALUES(?,?,?)",body.id,me.id,body.count);return json({ok:true});
  }
  const generation=await first(e,"SELECT * FROM generations WHERE id=? AND state='staging'",body.generation||'');
  if(!generation)return json({error:'Unknown staging generation'},409);
  if(path==='/api/sync/folder'){
    const f=validateFolder(body.folder);const match=f.title.match(/(\d+)\s*$/);
    await run(e,'INSERT OR REPLACE INTO folders VALUES(?,?,?,?,?,?)',generation.id,f.id,f.title,match?Number(match[1]): -1,Number(body.ordinal)||0,JSON.stringify(f));return json({ok:true});
  }
  if(path==='/api/sync/preferences'){
    // Seed only on first migration; later snapshots never overwrite edge choices.
    if(await active(e))return json({error:'Preferences already owned by edge'},409);
    const prefs=(body.favorites||[]).slice(0,20),subs=(body.subscribers||[]).slice(0,20);
    const statements=[...prefs.map(x=>stmt(e,'INSERT OR IGNORE INTO favorites VALUES(?,?)',x.owner,x.job)),...subs.map(x=>stmt(e,'INSERT OR IGNORE INTO subscribers VALUES(?,?)',x.owner,x.enabled?1:0))];
    if(statements.length)await e.DB.batch(statements);return json({ok:true});
  }
  if(path==='/api/sync/publish'){
    const stored=(await first(e,'SELECT count(*) n FROM folders WHERE generation=?',generation.id)).n;
    if(stored!==generation.expected)return json({error:'Incomplete generation'},409);
    // Statistics are derived server-side, never trusted from the export client.
    const stats=await first(e,"SELECT count(DISTINCT f.id) folders,count(*)+count(DISTINCT f.id) messages,coalesce(sum(json_extract(x.value,'$.source_kind')='photo'),0) photos,coalesce(sum(json_extract(x.value,'$.source_kind')='video'),0) videos FROM folders f,json_each(f.payload,'$.items') x WHERE generation=?",generation.id);
    const old=await active(e);
    // No historical broadcast on initial migration. Later new folders get an
    // outbox row in the same atomic D1 batch as catalog publication.
    const statements=[];
    if(old)statements.push(stmt(e,"INSERT OR IGNORE INTO events(id,type,owner,payload,touched) SELECT 'notice:'||f.id||':'||s.owner,'notice',s.owner,json_object('id',f.id,'title',f.title,'count',json_array_length(f.payload,'$.items')),? FROM folders f CROSS JOIN subscribers s WHERE f.generation=? AND s.enabled=1 AND json_extract(f.payload,'$.state')='complete' AND f.id NOT IN (SELECT job FROM notice_seen)",now(),generation.id));
    statements.push(stmt(e,"INSERT OR IGNORE INTO notice_seen SELECT id FROM folders WHERE generation=? AND (json_extract(payload,'$.state')='complete' OR ?=1)",generation.id,old?0:1),stmt(e,"INSERT OR REPLACE INTO config VALUES('active',?)",generation.id),stmt(e,"INSERT OR REPLACE INTO config VALUES('stats',?)",JSON.stringify(stats)),stmt(e,"UPDATE generations SET state='published' WHERE id=?",generation.id));
    await e.DB.batch(statements);return json({ok:true,stats});
  }
  return json({error:'Not found'},404);
}
export default {
  async fetch(request,e){
    const path=new URL(request.url).pathname;
    try{
      if(path==='/health')return json({ok:true,service:'sougabot-edge'});
      if(path.startsWith('/api/'))return await api(request,e,path);
      if(path!=='/webhook'||request.method!=='POST')return json({error:'Not found'},404);
      if(!await same(request.headers.get('x-telegram-bot-api-secret-token'),e.WEBHOOK_SECRET))return json({error:'Unauthorized'},401);
      const raw=await request.text();if(raw.length>1000000)return json({error:'Too large'},413);
      const update=JSON.parse(raw);if(!Number.isSafeInteger(update.update_id))return json({error:'Invalid update'},400);
      const type=classify(update,owners(e),Number(e.RELAY_USER_ID));
      if(type==='importer'){
        await run(e,'INSERT OR IGNORE INTO importer_inbox(id,payload,expires) VALUES(?,?,?)',update.update_id,raw,now()+86400*7);
      }else if(type==='user'){
        const uid=update.message?.from.id||update.callback_query.from.id;
        await enqueue(e,`update:${update.update_id}`,'update',uid,update);
      }
      return json({ok:true});
    }catch{return json({error:'Request could not be processed'},503);}
  },
  async queue(batch,e){for(const message of batch.messages){try{await process(e,message.body.id);message.ack();}catch{message.retry({delaySeconds:30});}}},
  async scheduled(_event,e){
    // No implicit replay after an interrupted external send.
    await run(e,"UPDATE events SET state='uncertain',error='Interrupted handler' WHERE state='processing' AND touched<?",now()-600);
    const pending=await rows(e,"SELECT id,type FROM events WHERE state='pending' AND next_at<=? ORDER BY touched LIMIT 20",now());
    for(const x of pending)await signal(e,x.id,x.type);
    const obsolete=await rows(e,"SELECT id FROM generations WHERE state='published' AND id<>(SELECT value FROM config WHERE key='active') LIMIT 1");
    if(obsolete.length){await run(e,'DELETE FROM folders WHERE generation=?',obsolete[0].id);await run(e,'DELETE FROM generations WHERE id=?',obsolete[0].id);}
    // Bounded retention preserves uncertain sends; never discard unresolved inbox.
    await run(e,"DELETE FROM events WHERE id IN (SELECT id FROM events WHERE state='complete' AND touched<? LIMIT 100)",now()-86400*7);
  }
};
