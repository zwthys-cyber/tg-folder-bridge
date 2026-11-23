"""Recover an existing byte-identical original using trusted full-range hashes."""
import asyncio
import hashlib
import hmac
import struct
import time
from contextlib import asynccontextmanager

from telethon import functions, types
from core import media_key
from transport_recovery import error_detail, rate_wait


async def compare_hashes(size, fetch_left, fetch_right, *, max_requests=1024, progress=None):
    if not isinstance(size,int) or not 0 < size <= 4*1024**3:
        return None
    requests = 0
    caches = [{},{}]
    async def at(side,offset):
        nonlocal requests
        if offset not in caches[side]:
            if requests >= max_requests:
                return None
            requests += 1
            values = await (fetch_left if side==0 else fetch_right)(offset)
            if not isinstance(values,(list,tuple)) or not values or len(values)>4096:
                return None
            for value in values:
                if (not isinstance(value,types.FileHash)
                        or type(value.offset) is not int or type(value.limit) is not int
                        or value.offset < 0 or not 0 < value.limit <= 1024**2
                        or not isinstance(value.hash,bytes) or len(value.hash)!=32):
                    return None
                if value.offset >= offset:
                    caches[side][value.offset] = value
        return caches[side].pop(offset,None)
    offset = ranges = 0
    proof = hashlib.sha256(struct.pack('<Q',size))
    while offset < size:
        left,right = await at(0,offset),await at(1,offset)
        if (left is None or right is None or left.limit!=right.limit
                or not hmac.compare_digest(left.hash,right.hash)):
            return None
        proof.update(struct.pack('<QQ',offset,left.limit)+left.hash)
        offset += min(left.limit,size-offset)
        ranges += 1
        if progress:
            progress(offset,size)
    return dict(verified_bytes=size,hash_ranges=ranges,hash_requests=requests,
                proof_sha256=proof.hexdigest())


@asynccontextmanager
async def file_hashes(client,document):
    sender = None
    try:
        if document.dc_id != client.session.dc_id:
            sender = await asyncio.wait_for(client._borrow_exported_sender(document.dc_id),15)
        location = types.InputDocumentFileLocation(document.id,document.access_hash,
                                                   document.file_reference,'')
        async def fetch(offset):
            request = functions.upload.GetFileHashesRequest(location,offset)
            return await asyncio.wait_for(client._call(sender,request,flood_sleep_threshold=0)
                if sender else client(request),15)
        yield fetch
    finally:
        if sender is not None:
            await client._return_exported_sender(sender)


async def recover_identical_copy(bridge,message,key,job):
    document = getattr(message,'document',None)
    if not isinstance(document,types.Document) or not document.size:
        return False
    checked = job.setdefault('identity_checked',set())
    identity = media_key(message)
    if identity in checked:
        return False
    owner = bridge.store.get(job['id'])['owner']
    candidates = bridge.store.db.execute('''SELECT f.* FROM files f
        JOIN jobs j ON j.id=f.job
        JOIN source_entries e ON e.job=f.job AND e.source_chat=f.source_chat
          AND e.source_message=f.source_message
        WHERE j.owner=? AND f.chat=? AND f.source_chat=? AND e.size=?
          AND length(f.content_sha256)=64 AND f.media_key LIKE 'document:%'
          AND f.file_id<>'' AND NOT (f.job=? AND f.source_chat=? AND f.source_message=?)
        ORDER BY (f.job=?) DESC,(j.state='complete') DESC,j.rowid DESC LIMIT 3''',
        (owner,owner,message.chat_id,document.size,job['id'],*key,job['id'])).fetchall()
    if not candidates:
        return False
    checked.add(identity)
    await bridge.await_cooldown(job)
    started = time.monotonic()
    deadline = started+180
    metrics = dict(bytes=0,total=document.size,transport='remote_hash_compare')
    failure = 'identity_unverified'
    job['phase'] = '核对已保存副本'
    bridge.store.update(job['id'],'running',job['expected'],
        '发现同大小的已保存文件，正在核对完整分段哈希；内容一致后才复用。')
    try:
        ids = list(dict.fromkeys([message.id]+[r['source_message'] for r in candidates]))
        fetched = await asyncio.wait_for(bridge.client.get_messages(message.chat_id,ids=ids),15)
        by_id = {m.id:m for m in fetched if m}
        original = by_id.get(message.id)
        if (not original or media_key(original)!=media_key(message)
                or not isinstance(original.document,types.Document)
                or original.document.size!=document.size):
            failure = 'source_identity_changed'
            return False
        for row in candidates:
            if time.monotonic() >= deadline:
                break
            candidate = by_id.get(row['source_message'])
            if (not candidate or media_key(candidate)!=row['media_key']
                    or not isinstance(candidate.document,types.Document)
                    or candidate.document.size!=document.size):
                continue
            async with file_hashes(bridge.client,original.document) as left, file_hashes(bridge.client,candidate.document) as right:
                last_progress = 0
                def progress(current,total):
                    nonlocal last_progress
                    now = time.monotonic()
                    if now-last_progress >= 3 or current==total:
                        last_progress = now
                        bridge.store.update(job['id'],'running',job['expected'],
                            f'核对已保存副本：{current/1024**2:.1f} / {total/1024**2:.1f} MiB；完整一致后才复用。')
                proof = await asyncio.wait_for(compare_hashes(document.size,left,right,progress=progress),
                                                max(0.1,deadline-time.monotonic()))
            if not proof:
                continue
            if row['job']==job['id']:
                bridge.store.merge_alias(job['id'],key,
                    (row['source_chat'],row['source_message']),media_key(message))
            else:
                saved = dict(row)
                saved['media_key'] = media_key(message)
                bridge.store.reuse_document(job['id'],key,saved,message.raw_text or '')
            metrics.update(proof)
            failure = ''
            bridge.store.update(job['id'],'running',job['expected'],
                '已核实全部分段哈希一致，复用已保存的同一原文件。')
            return True
        failure = 'no_verified_identical_copy'
        return False
    except Exception as error:
        failure = error_detail(error,'内容身份核对')
        wait = rate_wait(error,0)
        if wait is not None:
            bridge.cooldown.defer(wait,failure)
        if type(error).__name__ in {'AuthKeyDuplicatedError','AuthKeyUnregisteredError','SessionRevokedError'}:
            raise
        return False
    finally:
        job['phase'] = '下载'
        metrics['seconds'] = time.monotonic()-started
        bridge.store.record_transfer(job['id'],'核对副本',metrics,failure)
