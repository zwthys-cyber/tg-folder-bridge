import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from core import Store
from content_identity import analyze, preference


class Index(unittest.TestCase):
    def test_aliases_preserve_source_counts_and_survive_canonical_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'index.sqlite3'
            store = Store(path); store.create('job', 7, 'link')
            for n in (1, 2, 3):
                store.add_file('job', (10, n), NS(chat_id=7, message_id=n), f'file{n}', 'document', source_media_key=f'doc{n}')
            store.merge_alias('job', (10, 1), (10, 2), 'doc1')
            store.merge_alias('job', (10, 2), (10, 3), 'doc2')
            self.assertEqual(len(store.files('job')), 1)
            self.assertEqual(store.processed_count('job'), 3)
            self.assertEqual(len(store.resume_entries('job')), 3)
            self.assertTrue(all(x['file_id']=='file3' for x in store.resume_entries('job')))
            store.db.close(); store = Store(path)
            self.assertEqual(store.processed_count('job'), 3)
            store.db.close()

    def test_size_or_name_alone_is_never_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'db'); store.create('job', 7, 'link')
            store.add_file('job', (1, 1), NS(chat_id=7, message_id=1), 'file', 'document')
            a = dict(content_sha256='abc', stream_sha256='', quality={})
            store.set_analysis('job', (1, 1), a)
            self.assertIsNone(store.duplicate('job', (1, 2), dict(content_sha256='different', stream_sha256='')))
            self.assertIsNotNone(store.duplicate('job', (1, 2), a))
            store.db.close()

    def test_audit_keeps_previously_completed_folder_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'db'); store.create('job', 7, 'link')
            store.add_file('job', (1, 1), NS(chat_id=7, message_id=1), 'file', 'document')
            store.update('job', 'complete', 1); store.request_audit('job'); store.update('job', 'running', 1)
            self.assertEqual(len(store.catalog()), 1)
            store.finish_audit('job')
            self.assertEqual(len(store.catalog()), 0)
            store.db.close()


class Fingerprints(unittest.IsolatedAsyncioTestCase):
    async def test_identical_bytes_have_same_hash_without_external_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,b=Path(tmp)/'a.bin',Path(tmp)/'b.bin'
            a.write_bytes(b'unchanged original'); b.write_bytes(a.read_bytes())
            first,second=await analyze(a),await analyze(b)
            self.assertEqual(first['content_sha256'],second['content_sha256'])
            self.assertEqual(a.read_bytes(),b'unchanged original')
            b.write_bytes(b'changed original!!')
            self.assertNotEqual(first['content_sha256'],(await analyze(b))['content_sha256'])

    @unittest.skipUnless(shutil.which('ffprobe'), 'ffprobe required for metadata verification')
    async def test_same_encoded_video_with_location_metadata_is_preferred_without_transcoding(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        with tempfile.TemporaryDirectory() as tmp:
            a,b=Path(tmp)/'a.mov',Path(tmp)/'b.mov'
            async def command(*args):
                p=await asyncio.create_subprocess_exec(get_ffmpeg_exe(),'-nostdin','-v','error',*args,
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                self.assertEqual(await p.wait(),0)
            await command('-f','lavfi','-i','color=c=blue:s=128x72:r=10:d=0.3',
                          '-c:v','libx264','-pix_fmt','yuv420p','-y',str(a))
            await command('-i',str(a),'-c','copy','-metadata','location=+00.0000+000.0000/',
                          '-movflags','use_metadata_tags','-y',str(b))
            before=a.read_bytes(),b.read_bytes()
            first,second=await analyze(a,native=True),await analyze(b)
            self.assertNotEqual(first['content_sha256'],second['content_sha256'])
            self.assertTrue(first['stream_sha256'])
            self.assertEqual(first['stream_sha256'],second['stream_sha256'])
            self.assertTrue(second['quality']['has_location'])
            self.assertGreater(preference(second['quality']),preference(first['quality']))
            self.assertNotIn('+00.',json.dumps(second))
            self.assertEqual((a.read_bytes(),b.read_bytes()),before)
