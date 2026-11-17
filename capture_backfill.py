"""Idle-time, bounded header/tail sampling for existing files' embedded dates."""
import asyncio
import math
import os
import time
import shutil
import tempfile
from pathlib import Path
from capture_time import IMAGES,VIDEOS,inspect,VERSION,original_source
from core import media_key
from source_lock import source_lease
from transport_recovery import rate_wait,error_detail

CHUNK=512*1024
SEGMENT=4*1024**2


def ranges(size):
    if size<=0:return []
    head=min(size,SEGMENT)
    result=[(0,head)]
    if size>head:
        tail=max(head,((size-SEGMENT)//CHUNK)*CHUNK)
        result.append((tail,size))
    return result


async def sample(client,message,path,size):
    """Sparse holes do not allocate the original video's full disk footprint."""
    downloaded=0
    with path.open('wb') as output:
        output.truncate(size)
        for start,end in ranges(size):
            iterator=client.iter_download(message,offset=start,limit=math.ceil((end-start)/CHUNK),
                chunk_size=CHUNK,request_size=CHUNK,file_size=size)
            offset=start
            try:
                while offset<end:
                    block=await iterator.__anext__()
                    expected=min(CHUNK,end-offset)
                    if len(block)<expected:raise OSError('Incomplete metadata sample')
                    output.seek(offset);output.write(block[:expected])
                    offset+=expected;downloaded+=expected
            finally:
                await iterator.close()
    return downloaded


class CaptureBackfill:
    def capture_candidate(self):
        # Recently viewed folders first, then recent imports. Only metadata is changed.
        return self.store.db.execute('''SELECT f.*,e.name AS source_name,e.size AS file_size
            FROM files f LEFT JOIN source_entries e ON e.job=f.job AND e.source_chat=f.source_chat
                AND e.source_message=f.source_message
            WHERE f.capture_version<? AND f.job IN (SELECT id FROM jobs WHERE state='complete' OR audit_public=1)
            ORDER BY COALESCE((SELECT touched FROM capture_priorities WHERE job=f.job),0) DESC,
                COALESCE((SELECT MAX(d.rowid) FROM deliveries d WHERE d.job=f.job),0) DESC,
                f.rowid DESC LIMIT 1''',(VERSION,)).fetchone()

    async def backfill_one(self,row):
        source=(row['source_chat'],row['source_message'])
        # Full-byte identity permits sharing dates; never reuse only a stream hash or filename.
        known=self.store.db.execute("SELECT captured_at,capture_time_source,capture_timezone_assumed FROM files WHERE content_sha256<>'' "
            "AND content_sha256=? AND captured_at<>'' AND capture_version>=? LIMIT 1",(row['content_sha256'],VERSION)).fetchone()
        if known:
            self.store.set_capture_time(row['job'],source,dict(known));return
        if self.reconnect_required or not self.client.is_connected():await self.connect_user()
        peers=getattr(self,'capture_peers',None)
        if peers is None:
            peers={}
            for name in self.allowed:
                entity=await self.client.get_entity(name)
                peers[entity.id]=entity
            self.capture_peers=peers
        if source[0] not in peers:
            self.store.set_capture_time(row['job'],source,{});return
        message=await self.client.get_messages(peers[source[0]],ids=source[1])
        if not message or not media_key(message):
            self.store.set_capture_time(row['job'],source,{});return
        ext=(getattr(getattr(message,'file',None),'ext','') or Path(row['source_name'] or '').suffix).lower()
        size=getattr(getattr(message,'file',None),'size',None) or row['file_size']
        if ext not in IMAGES|VIDEOS or not size or size>4*1024**3:
            self.store.set_capture_time(row['job'],source,{});return
        root=self.settings_file.parent/'capture-spool';root.mkdir(mode=0o700,exist_ok=True)
        if shutil.disk_usage(root).free<10*1024**3+2*SEGMENT:
            return
        with tempfile.TemporaryDirectory(prefix='metadata-',dir=root) as directory:
            path=Path(directory)/('original'+ext)
            used=await sample(self.client,message,path,size)
            result=await inspect(path)
            self.store.set_capture_time(row['job'],source,result)
            print(f"拍摄时间补读：任务 {row['job']}；原始日期 {original_source(result.get('capture_time_source'))}；取样 {used/1024**2:.1f} MiB",flush=True)

    async def capture_one_safely(self,row):
        """Optional metadata work must not turn a successful import into a failed task."""
        key=(row['job'],row['source_chat'],row['source_message'])
        failures=getattr(self,'capture_failures',None)
        if failures is None:self.capture_failures=failures={}
        try:
            await asyncio.wait_for(self.backfill_one(row),45)
            failures.pop(key,None)
        except asyncio.CancelledError:raise
        except Exception as error:
            wait=rate_wait(error,0)
            if wait is not None:
                self.cooldown.defer(wait,'拍摄时间补读：'+error_detail(error))
                await self.client.disconnect();self.reconnect_required=True
                return
            failures[key]=failures.get(key,0)+1
            print('拍摄时间补读失败：'+type(error).__name__,flush=True)
            if failures[key]>=3:
                self.store.set_capture_time(row['job'],key[1:],{})
                failures.pop(key,None)

    async def capture_between_files(self,job):
        # The caller already owns source_lease and has confirmed the previous upload.
        # No menu clicks are made here, and no second source connection is created.
        if (os.getenv('CAPTURE_BACKFILL','1')!='1' or self.transfer_mode!='download_upload'
                or self.blocked or self.pending or self.cooldown.remaining()):return
        now=time.monotonic()
        try:interval=max(5,float(os.getenv('CAPTURE_BACKFILL_INTERVAL_SECONDS','15')))
        except ValueError:interval=15
        if now-getattr(self,'capture_last_slice',-float('inf'))<interval:return
        row=self.capture_candidate()
        if row:
            old_phase=job.get('phase','')
            detail=self.store.get(job['id'])['detail']
            self.store.update(job['id'],'running',job['expected'],'已确认上一文件收件，正在补读历史拍摄日期。')
            try:await self.capture_one_safely(dict(row))
            finally:
                self.capture_last_slice=time.monotonic()
                job['phase']=old_phase
                self.store.update(job['id'],'running',job['expected'],detail)
            if self.reconnect_required:
                await self.await_cooldown(job)
                await self.connect_user()

    async def capture_loop(self):
        if os.getenv('CAPTURE_BACKFILL','1')!='1' or self.transfer_mode!='download_upload':return
        while True:
            await asyncio.sleep(1)
            if self.active or not self.jobs.empty() or self.cooldown.remaining():continue
            row=self.capture_candidate()
            if not row:
                await asyncio.sleep(30);continue
            row=dict(row)
            async with source_lease(self.source_lock):
                if self.active or not self.jobs.empty():continue
                await self.capture_one_safely(row)
