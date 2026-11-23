import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from telethon import functions,types,errors
from core import Store
from remote_identity import compare_hashes,recover_identical_copy,HashRequestGate


def ranges(data,chunk=128*1024):
    return [types.FileHash(offset,min(chunk,len(data)-offset),
        hashlib.sha256(data[offset:offset+chunk]).digest()) for offset in range(0,len(data),chunk)]


def fetcher(items):
    async def fetch(offset):
        return [h for h in items if h.offset>=offset][:2]
    return fetch


class HashComparison(unittest.IsolatedAsyncioTestCase):
    async def test_short_wait_retries_same_hash_request_with_shared_bounded_budget(self):
        flood = errors.FloodWaitError(request=None,capture=5)
        operation = AsyncMock(side_effect=[flood,b'valid',flood,flood])
        gate = HashRequestGate()
        with patch('remote_identity.asyncio.sleep',new_callable=AsyncMock) as sleep:
            self.assertEqual(await gate.run(operation),b'valid')
            with self.assertRaises(errors.FloodWaitError):
                await gate.run(operation)
        self.assertEqual(operation.await_count,4)
        self.assertEqual(sum(c.args[0]==5 for c in sleep.await_args_list),2)

    async def test_long_wait_does_not_hold_hash_recovery_loop(self):
        operation = AsyncMock(side_effect=errors.FloodWaitError(request=None,capture=60))
        with patch('remote_identity.asyncio.sleep',new_callable=AsyncMock):
            with self.assertRaises(errors.FloodWaitError):
                await HashRequestGate().run(operation)
        operation.assert_awaited_once()

    async def test_equal_proof_covers_every_byte_across_response_pages(self):
        data = bytes(range(256))*2400+b'GPS metadata at the very end'
        proof = await compare_hashes(len(data),fetcher(ranges(data)),fetcher(ranges(data)))
        self.assertEqual(proof['verified_bytes'],len(data))
        self.assertEqual(proof['hash_ranges'],len(ranges(data)))
        self.assertGreater(proof['hash_requests'],2)
        self.assertEqual(len(proof['proof_sha256']),64)

    async def test_changed_final_metadata_does_not_merge_matching_prefixes(self):
        data = bytes(range(256))*2400+b'GPS data 1'
        other = data[:-1]+b'2'
        self.assertIsNone(await compare_hashes(len(data),fetcher(ranges(data)),fetcher(ranges(other))))

    async def test_missing_tail_hash_or_request_budget_cannot_prove_identity(self):
        data = bytes(range(256))*2400
        hashes = ranges(data)
        self.assertIsNone(await compare_hashes(len(data),fetcher(hashes),fetcher(hashes[:-1])))
        self.assertIsNone(await compare_hashes(len(data),fetcher(hashes),fetcher(hashes),max_requests=2))

    async def test_invalid_hash_range_does_not_loop_or_prove_identity(self):
        bad = [types.FileHash(0,0,b'x'*32)]
        self.assertIsNone(await compare_hashes(128,fetcher(bad),fetcher(bad)))


class Reuse(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/'index.sqlite3')
        self.store.create('target',7,'target-link');self.store.update('target','partial',1)
        self.store.create('origin',7,'origin-link');self.store.update('origin','complete',1)
        self.data = bytes(range(256))*240+b'GPS unchanged'
        def message(identifier,document_id,name):
            doc=types.Document(document_id,456,b'ref',datetime.now(timezone.utc),
                'video/quicktime',len(self.data),5,[types.DocumentAttributeFilename(name)])
            return NS(chat_id=101,id=identifier,raw_text='source caption',document=doc,photo=None)
        self.bad=message(10,100,'renamed.mov');self.good=message(20,200,'IMG_1234.MOV')
        self.store.set_source_entries('target',[dict(source_chat=101,source_message=10,
            position=1,name='renamed.mov',size=len(self.data),kind='video')])
        self.store.set_source_entries('origin',[dict(source_chat=101,source_message=20,
            position=1,name='IMG_1234.MOV',size=len(self.data),kind='video')])
        self.add_candidate('origin')
        messages={10:self.bad,20:self.good};data_by_document={100:self.data,200:self.data}
        class Client:
            session=NS(dc_id=5)
            async def __call__(inner,request):
                self.assertIsInstance(request,functions.upload.GetFileHashesRequest)
                return await fetcher(ranges(data_by_document[request.location.id]))(request.offset)
        self.client=Client()
        async def get(peer,ids):return [messages[i] for i in ids]
        self.client.get_messages=AsyncMock(side_effect=get)
        self.bridge=NS(store=self.store,client=self.client,await_cooldown=AsyncMock(),
            cooldown=NS(defer=lambda *args:None))
        self.job=dict(id='target',expected=1)
        self.data_by_document=data_by_document

    def add_candidate(self,job):
        self.store.add_file(job,(101,20),NS(chat_id=7,message_id=88),
            'already-saved-file','video',source_media_key='document:200')
        self.store.set_analysis(job,(101,20),dict(content_sha256=hashlib.sha256(self.data).hexdigest(),
                                                stream_sha256='',quality={}))

    async def asyncTearDown(self):
        self.store.db.close();self.tmp.cleanup()

    async def test_verified_cross_folder_copy_uses_existing_file_id_and_target_identity(self):
        self.assertTrue(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        saved=self.store.files('target')
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0]['file_id'],'already-saved-file')
        self.assertEqual(saved[0]['media_key'],'document:100')
        self.assertEqual(saved[0]['content_sha256'],hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.store.transfer_history(1)[0]['payload']['verified_bytes'],len(self.data))

    async def test_same_folder_duplicate_becomes_alias_without_an_extra_file(self):
        self.store.db.execute("DELETE FROM files WHERE job='origin'");self.store.db.commit()
        self.store.set_source_entries('target',[dict(source_chat=101,source_message=20,
            position=2,name='IMG_1234.MOV',size=len(self.data),kind='video')])
        self.add_candidate('target');self.job['expected']=2;self.store.update('target','partial',2)
        self.assertTrue(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.assertEqual(len(self.store.files('target')),1)
        self.assertEqual(self.store.processed_count('target'),2)

    async def test_same_size_different_bytes_is_not_reused(self):
        self.data_by_document[100]=self.data[:-1]+b'!'
        self.assertFalse(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.assertEqual(self.store.files('target'),[])

    async def test_other_owner_candidate_never_causes_source_requests(self):
        self.store.db.execute("UPDATE jobs SET owner=8 WHERE id='origin'");self.store.db.commit()
        self.assertFalse(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.client.get_messages.assert_not_awaited()

    async def test_large_original_does_not_spend_minutes_in_optional_hash_probe(self):
        self.bad.document.size=129*1024**2
        self.assertFalse(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.client.get_messages.assert_not_awaited()

    async def test_edited_source_position_is_not_treated_as_the_original(self):
        self.good.document.id=300
        self.assertFalse(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.assertEqual(self.store.files('target'),[])

    async def test_failed_identity_check_is_not_repeated_after_download_failure(self):
        self.data_by_document[100]=self.data[:-1]+b'!'
        self.assertFalse(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.assertFalse(await recover_identical_copy(self.bridge,self.bad,(101,10),self.job))
        self.client.get_messages.assert_awaited_once()

