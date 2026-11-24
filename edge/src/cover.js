// Only small JPEG/PNG display covers cross the Worker. Original documents and
// videos are always served by file_id. No decoding, transcoding or disk cache.
export const COVER_LIMIT=2*1024**2;
export function coverSource(folder){
  const first=folder.items.find(x=>x.source_kind==='photo'||/\.(jpg|jpeg|png|heic|heif|webp)$/i.test(x.name||''));
  return first && first.kind==='document' && /\.(jpg|jpeg|png)$/i.test(first.name||'') && first.file_size>0 && first.file_size<=COVER_LIMIT ? first : null;
}
export function multipartCover(response,fields,name='cover.jpg'){
  const length=Number(response.headers.get('content-length')||0);
  if(!response.ok || !response.body || length>COVER_LIMIT)throw Error('Cover source unavailable');
  const boundary='sougabot-'+crypto.randomUUID(),encoder=new TextEncoder();
  const prefix=Object.entries(fields).map(([key,value])=>`--${boundary}\r\nContent-Disposition: form-data; name="${key}"\r\n\r\n${typeof value==='string'?value:JSON.stringify(value)}\r\n`).join('')+`--${boundary}\r\nContent-Disposition: form-data; name="photo"; filename="${name}"\r\nContent-Type: ${name.endsWith('.png')?'image/png':'image/jpeg'}\r\n\r\n`;
  const reader=response.body.getReader();let started=false,bytes=0,done=false;
  return {headers:{'content-type':`multipart/form-data; boundary=${boundary}`},body:new ReadableStream({
    async pull(controller){
      if(!started){started=true;controller.enqueue(encoder.encode(prefix));return;}
      const part=await reader.read();
      if(part.done){done=true;controller.enqueue(encoder.encode(`\r\n--${boundary}--\r\n`));controller.close();return;}
      bytes+=part.value.byteLength;
      if(bytes>COVER_LIMIT){await reader.cancel();controller.error(Error('Cover exceeds byte budget'));return;}
      controller.enqueue(part.value);
    },
    async cancel(){if(!done)await reader.cancel();}
  }),duplex:'half'};
}
