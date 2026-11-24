export const keyboard = rows => ({inline_keyboard: rows});
export const button = (text, data) => ({text, callback_data: data});
export const chunks = (items, n) => Array.from({length: Math.ceil(items.length / n)}, (_, i) => items.slice(i*n,(i+1)*n));
export function order(items) {
  const rank = x => x.kind === 'document' ? 0 : x.kind === 'video' ? 2 : 1;
  return items.map((x,i)=>({x,i})).sort((a,b)=>rank(a.x)-rank(b.x)||a.i-b.i).map(x=>x.x);
}
export function albums(items) {
  const result=[];
  for(const item of order(items)) {
    const previous=result.at(-1);
    if(!previous || previous.length===10 || previous[0].kind!==item.kind || !['photo','video','document'].includes(item.kind)) result.push([item]);
    else previous.push(item);
  }
  return result;
}
export function mediaCall(chat, items) {
  const caption = x => (x.caption || '').slice(0,1024);
  if(items.length>1) return ['sendMediaGroup',{chat_id:chat,media:items.map(x=>({type:x.kind,media:x.file_id,caption:caption(x)}))}];
  const x=items[0], names={photo:['sendPhoto','photo'],video:['sendVideo','video'],document:['sendDocument','document'],animation:['sendAnimation','animation'],audio:['sendAudio','audio'],voice:['sendVoice','voice'],video_note:['sendVideoNote','video_note']};
  const [method,field]=names[x.kind] || [];
  if(!method) throw Error('Unsupported media kind');
  return [method,{chat_id:chat,[field]:x.file_id,...(x.kind==='video_note'?{}:{caption:caption(x)})}];
}
export function details(folder, starting=false) {
  const counts={photo:0,video:0,document:0};
  for(const x of folder.items) counts[x.source_kind in counts?x.source_kind:'document']++;
  const known=folder.items.every(x=>x.file_size!==null && x.file_size!==undefined);
  const total=folder.items.reduce((n,x)=>n+(x.file_size||0),0);
  const size=known?`${(total/(total>=2**30?2**30:2**20)).toFixed(2)} ${total>=2**30?'GB':'MB'}`:'未知';
  return `📁 ${folder.title}\n\n${folder.partial_notice?folder.partial_notice+'\n\n':''}📖 内容统计：\n　• 消息数：${folder.items.length+1} 条\n　• 图片数：${counts.photo} 张\n　• 视频数：${counts.video} 个\n　• 文档数：${counts.document} 个\n　• 总大小：${size}\n\n📅 创建时间：${folder.source_created_at||'未知'}\n✏️ 最后更新：${folder.source_updated_at||'未知'}\n\n${starting?'⏳ 开始发送内容...':'👇 点击下方按钮查看内容'}`;
}
export function validateFolder(f) {
  if(!f || !/^[a-f0-9]{16}$/.test(f.id) || typeof f.title!=='string' || f.title.length>160 || !Array.isArray(f.items) || !f.items.length || f.items.length>5000) throw Error('Invalid folder');
  for(const x of f.items) if(!['photo','video','document','animation','audio','voice','video_note'].includes(x.kind) || typeof x.file_id!=='string' || !x.file_id || x.file_id.length>1024 || (x.caption||'').length>1024) throw Error('Invalid media');
  if(new TextEncoder().encode(JSON.stringify(f)).length>900000) throw Error('Folder exceeds row budget');
  return f;
}
export function classify(raw, owners, relay) {
  const m=raw.message, q=raw.callback_query;
  if(q?.message?.chat?.type==='private' && q.from.id===q.message.chat.id) return 'user';
  if(!m || m.chat?.type!=='private' || m.from?.id!==m.chat.id) return 'ignore';
  if(m.from.id===relay && ['document','photo','video','animation','audio','voice','video_note'].some(k=>m[k])) return 'importer';
  const text=m.text||'';
  if(owners.includes(m.from.id) && (/^\/(fetch|folders|status|resume|speed|transport|diagnostics|resolve|dedup|failures)(?:@\w+)?(?:\s|$)/.test(text)|| /^https:\/\/t\.me\/\w+\?start=/.test(text))) return 'importer';
  return 'user';
}
