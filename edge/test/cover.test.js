import {test} from 'node:test';
import assert from 'node:assert/strict';
import {coverSource,multipartCover,COVER_LIMIT} from '../src/cover.js';
test('only first eligible small image is a cover source',()=>{
 const item={kind:'document',source_kind:'photo',name:'a.JPG',file_size:100,file_id:'original'};
 assert.equal(coverSource({items:[item]}).file_id,'original');
 assert.equal(coverSource({items:[{...item,name:'a.HEIC'},item]}),null);
 assert.equal(coverSource({items:[{...item,file_size:COVER_LIMIT+1}]}),null);
});
test('multipart cover stream has a hard byte cap and never materializes a media buffer',async()=>{
 const body=new ReadableStream({start(c){c.enqueue(new Uint8Array(COVER_LIMIT));c.enqueue(new Uint8Array(1));c.close();}});
 const upload=multipartCover(new Response(body),{chat_id:3});
 const reader=upload.body.getReader();await reader.read();await reader.read();await assert.rejects(reader.read(),/byte budget/);
});
