import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,AsyncMock
from PIL import Image
from capture_time import normalize,from_probe,photo_date,inspect,from_exiftool,original_source,epoch
from capture_backfill import ranges,sample,CHUNK,SEGMENT,CaptureBackfill
from delivery import display_order
from core import Store


class Dates(unittest.TestCase):
    def test_camera_offset_and_naive_camera_timezone(self):
        value=normalize('2026:10:23 12:00:00','exif.DateTimeOriginal','+08:00')
        self.assertEqual(value['captured_at'],'2026-10-23T04:00:00Z')
        self.assertFalse(value['capture_timezone_assumed'])
        with patch.dict(os.environ,{'MEDIA_TIMEZONE':'Asia/Shanghai'}):
            self.assertEqual(normalize('2026:10:23 12:00:00','exif.DateTimeOriginal')['captured_at'],value['captured_at'])

    def test_original_camera_tag_precedes_media_creation_and_placeholders_are_unknown(self):
        result=from_probe({'format':{'tags':{'creation_time':'2026-10-23T00:00:00Z',
            'com.apple.quicktime.creationdate':'2025-05-04T12:00:00+0800'}}})
        self.assertEqual(result['captured_at'],'2025-05-04T04:00:00Z')
        self.assertEqual(from_probe({'format':{'tags':{'creation_time':'1904-01-01T00:00:00Z'}}}),{})
        self.assertEqual(normalize('2026:99:23 00:00:00','exif.DateTimeOriginal'),{})

    def test_user_dates_descend_within_each_type_and_unknowns_keep_source_order(self):
        dates=['2026-10-06T00:00:00Z','2025-05-04T00:00:00Z','2026-10-23T00:00:00Z','2026-10-17T00:00:00Z']
        items=[dict(kind=k,file_id=f'{k}{i}',captured_at=d,capture_time_source='exif.DateTimeOriginal')
               for k in ('video','document') for i,d in enumerate(dates)]
        items += [dict(kind='document',file_id='unknown1',source_name='20261025.mp4'),
                  dict(kind='document',file_id='unknown2',upload_date='2026-11-17')]
        ordered=display_order(items)
        self.assertEqual([x['file_id'] for x in ordered],['document2','document3','document0','document1',
            'unknown1','unknown2','video2','video3','video0','video1'])
        self.assertEqual(items[0]['file_id'],'video0')

    def test_export_time_is_diagnostic_only_and_never_used_for_capture_order(self):
        weak=dict(kind='video',file_id='export',captured_at='2026-11-17T00:00:00Z',capture_time_source='video.creation_time')
        original=dict(kind='video',file_id='camera',captured_at='2025-05-04T00:00:00Z',capture_time_source='video.com.apple.quicktime.creationdate')
        self.assertIsNone(epoch(weak))
        self.assertEqual([r['file_id'] for r in display_order([weak,original])],['camera','export'])

    def test_exiftool_subsecond_xmp_and_gps_tags_ignore_file_timestamps(self):
        result=from_exiftool({'File:FileModifyDate':'2026:11:17 00:00:00+00:00',
            'ExifIFD:DateTimeOriginal':'2026:10:23 12:00:00','ExifIFD:OffsetTimeOriginal':'+08:00',
            'Composite:SubSecDateTimeOriginal':'2026:10:23 12:00:00.123+08:00'})
        self.assertEqual(result['captured_at'],'2026-10-23T04:00:00.123000Z')
        self.assertTrue(original_source(result['capture_time_source']))
        self.assertEqual(from_exiftool({'XMP-exif:DateTimeOriginal':'2025:05:04 12:00:00+08:00'})['captured_at'],
                         '2025-05-04T04:00:00Z')
        self.assertEqual(from_exiftool({'Composite:GPSDateTime':'2025:05:04 12:00:00'})['captured_at'],
                         '2025-05-04T12:00:00Z')
        self.assertEqual(from_exiftool({'File:FileModifyDate':'2026:10:23 12:00:00',
                                       'QuickTime:CreateDate':'2026:10:23 12:00:00'}),{})

    def test_photo_reads_original_exif_without_changing_bytes_or_using_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'image.jpg';exif=Image.Exif();exif[36867]='2026:10:23 12:00:00';exif[36881]='+08:00'
            Image.new('RGB',(2,2)).save(path,exif=exif)
            before=hashlib.sha256(path.read_bytes()).hexdigest();os.utime(path,(1,1))
            self.assertEqual(photo_date(path)['captured_at'],'2026-10-23T04:00:00Z')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            self.assertEqual(path.stat().st_mtime,1)
            plain=Path(tmp)/'plain.jpg';exif=Image.Exif();exif[306]='2026:10:23 12:00:00'
            Image.new('RGB',(2,2)).save(plain,exif=exif)
            self.assertEqual(photo_date(plain),{})

    def test_capture_metadata_persists_without_changing_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db');store.create('job',7,'link')
            store.add_file('job',(1,2),NS(chat_id=7,message_id=3),'original','document')
            result=dict(content_sha256='same',stream_sha256='stream',quality=dict(capture_checked=True,
                captured_at='2026-10-23T00:00:00Z',capture_time_source='exif.DateTimeOriginal'))
            store.set_analysis('job',(1,2),result)
            row=store.files('job')[0]
            self.assertEqual(row['captured_at'],'2026-10-23T00:00:00Z')
            self.assertEqual(row['file_id'],'original');self.assertEqual(row['content_sha256'],'same')
            store.set_capture_time('job',(1,2),{})
            self.assertEqual(store.files('job')[0]['captured_at'],row['captured_at'])
            store.set_capture_time('job',(1,2),dict(captured_at='2026-11-17T00:00:00Z',capture_time_source='video.creation_time'))
            self.assertEqual(store.files('job')[0]['captured_at'],row['captured_at'])
            self.assertEqual(store.files('job')[0]['capture_version'],2)
            store.db.close()


class Sampling(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which('exiftool'),'ExifTool required')
    async def test_real_subsecond_exif_is_read_without_modifying_the_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'original.jpg';Image.new('RGB',(2,2)).save(path)
            p=await asyncio.create_subprocess_exec(shutil.which('exiftool'),'-overwrite_original',
                '-DateTimeOriginal=2026:10:23 12:00:00','-OffsetTimeOriginal=+08:00',
                '-SubSecTimeOriginal=123',str(path),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            self.assertEqual(await p.wait(),0)
            before=path.read_bytes();result=await inspect(path)
            self.assertEqual(result['captured_at'],'2026-10-23T04:00:00.123000Z')
            self.assertFalse(result['capture_timezone_assumed'])
            self.assertTrue(original_source(result['capture_time_source']))
            self.assertEqual(path.read_bytes(),before)

    @unittest.skipUnless(shutil.which('ffprobe') and shutil.which('exiftool'),'metadata readers required')
    async def test_real_quicktime_camera_date_precedes_export_creation_time(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'original.mov'
            p=await asyncio.create_subprocess_exec(get_ffmpeg_exe(),'-nostdin','-v','error','-f','lavfi',
                '-i','color=c=blue:s=64x64:r=5:d=0.4','-metadata','creation_time=2026-10-23T00:00:00Z',
                '-metadata','com.apple.quicktime.creationdate=2025-05-04T12:00:00+0800',
                '-movflags','use_metadata_tags',str(path),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            self.assertEqual(await p.wait(),0)
            before=path.read_bytes();result=await inspect(path)
            self.assertEqual(result['captured_at'],'2025-05-04T04:00:00Z')
            self.assertTrue(original_source(result['capture_time_source']))
            self.assertEqual(path.read_bytes(),before)

    async def test_samples_only_header_tail_and_leaves_sparse_middle(self):
        size=32*1024**2+123
        class Iterator:
            def __init__(self,offset):self.offset=offset
            async def __anext__(self):
                length=min(CHUNK,size-self.offset);self.offset+=length;return b'x'*length
            async def close(self):pass
        client=NS(iter_download=lambda message,**kw:Iterator(kw['offset']))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'sample.mp4';used=await sample(client,NS(),path,size)
            self.assertLessEqual(used,2*SEGMENT+CHUNK)
            self.assertEqual(path.stat().st_size,size)
            with path.open('rb') as stream:
                self.assertEqual(stream.read(1),b'x');stream.seek(12*1024**2);self.assertEqual(stream.read(1),b'\0')
                stream.seek(size-1);self.assertEqual(stream.read(1),b'x')
            self.assertLessEqual(path.stat().st_blocks*512,used+CHUNK)
        self.assertEqual(ranges(100),[(0,100)])

    @unittest.skipUnless(shutil.which('ffprobe'),'ffprobe required')
    async def test_real_video_creation_tag_is_detected_without_transcoding(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'clip.mp4'
            p=await asyncio.create_subprocess_exec(get_ffmpeg_exe(),'-nostdin','-v','error','-f','lavfi',
                '-i','color=c=blue:s=64x64:r=5:d=0.4','-metadata','creation_time=2026-10-23T00:00:00Z',
                str(path),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            self.assertEqual(await p.wait(),0)
            before=path.read_bytes();result=await inspect(path)
            self.assertEqual(result['captured_at'],'2026-10-23T00:00:00Z')
            self.assertEqual(path.read_bytes(),before)


class Backfill(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=CaptureBackfill()
        self.app.store=Store(Path(self.tmp.name)/'index.sqlite3')
        self.app.store.create('old',7,'link');self.app.store.update('old','complete',1)
        self.app.store.add_file('old',(1,2),NS(chat_id=7,message_id=3),'original','document')
        self.app.store.create('active',7,'link');self.app.store.update('active','running',1)
        self.app.transfer_mode='download_upload';self.app.blocked=False;self.app.pending=None
        self.app.cooldown=NS(remaining=lambda:0);self.app.reconnect_required=False
        self.app.backfill_one=AsyncMock()
        self.job=dict(id='active',expected=1)

    async def asyncTearDown(self):
        self.app.store.db.close();self.tmp.cleanup()

    async def test_metadata_progresses_during_continuous_imports_but_is_rate_limited(self):
        await self.app.capture_between_files(self.job)
        self.app.backfill_one.assert_awaited_once()
        await self.app.capture_between_files(self.job)
        self.app.backfill_one.assert_awaited_once()
        self.assertEqual(self.app.store.get('active')['state'],'running')

    async def test_never_samples_during_unconfirmed_upload_or_global_cooldown(self):
        self.app.pending={'future':'unconfirmed'}
        await self.app.capture_between_files(self.job)
        self.app.pending=None;self.app.cooldown=NS(remaining=lambda:10)
        await self.app.capture_between_files(self.job)
        self.app.backfill_one.assert_not_awaited()

    async def test_versioned_completed_folder_candidates_and_view_priority(self):
        self.app.store.create('viewed',7,'link');self.app.store.update('viewed','complete',1)
        self.app.store.add_file('viewed',(1,4),NS(chat_id=7,message_id=5),'other','document')
        self.app.store.add_file('active',(1,6),NS(chat_id=7,message_id=7),'active','document')
        with self.app.store.db:
            self.app.store.db.execute("insert into capture_priorities values('old',10)")
            self.app.store.db.execute("update files set capture_checked=1 where job='old'")
        self.assertEqual(self.app.capture_candidate()['job'],'old')
        self.app.store.set_capture_time('old',(1,2),{})
        self.assertEqual(self.app.capture_candidate()['job'],'viewed')

    async def test_optional_probe_errors_do_not_fail_import_or_retry_forever(self):
        row=dict(self.app.capture_candidate());self.app.backfill_one.side_effect=ValueError('bad media')
        for _ in range(3):await self.app.capture_one_safely(row)
        self.assertEqual(self.app.store.files('old')[0]['capture_version'],2)
        self.assertIsNone(self.app.capture_candidate())
