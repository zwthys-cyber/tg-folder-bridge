import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from download_pipeline import DownloadPipeline
from media_spool import (MediaSpool, InsufficientDiskError, MediaTooLargeError,
                         IncompleteMediaError, SpoolBusyError)


class Spool(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'spool'
        self.spool = MediaSpool(self.root, max_bytes=16, reserve_bytes=1)
        self.message = NS(file=NS(size=4, ext='.mp4'), photo=None)

    async def asyncTearDown(self):
        self.spool.close()
        self.tmp.cleanup()

    def assert_empty(self):
        self.assertEqual(list(self.root.glob('transfer-*')), [])

    async def test_pipeline_retry_after_restart_reuses_complete_file_and_removes_it_after_receipt(self):
        self.message.chat_id, self.message.id, self.message.document = 1, 2, NS(id=3)

        async def download(message, file):
            file.write(b'data')

        client = NS(download_media=AsyncMock(side_effect=download))

        def stage(message, stats, progress):
            return self.spool.stage(client, message, stats=stats, checkpoint_scope='job')

        with patch.object(MediaSpool, 'checkpoint_minimum', 1):
            pipeline = DownloadPipeline([self.message], {(1, 2)}, stage)
            with self.assertRaises(ConnectionError):
                async with pipeline.stage(self.message, lambda stats: None, None) as path:
                    (path.parent / 'preview.jpg').write_bytes(b'old preview')
                    raise ConnectionError('upload connection interrupted')
            await pipeline.close()
            [ready] = self.root.glob('ready-*')
            self.spool.close()
            self.spool = MediaSpool(self.root, 16, 1)
            stats = {}
            async with self.spool.stage(client, self.message, stats=stats, checkpoint_scope='job') as path:
                self.assertEqual(path.read_bytes(), b'data')
                self.assertEqual(path.parent, ready)
                self.assertFalse((ready / 'preview.jpg').exists())
                self.assertEqual(stats['transport'], 'disk_checkpoint')
            client.download_media.assert_awaited_once()
            self.assertFalse(ready.exists())

    async def test_partial_download_does_not_create_checkpoint(self):
        self.message.chat_id, self.message.id, self.message.document = 1, 2, NS(id=3)

        async def download(message, file):
            file.write(b'pa')
            raise ConnectionError('download connection interrupted')

        with patch.object(MediaSpool, 'checkpoint_minimum', 1):
            with self.assertRaises(ConnectionError):
                async with self.spool.stage(NS(download_media=download), self.message, checkpoint_scope='job'):
                    self.fail('partial file must not be yielded')
        self.assertEqual(list(self.root.glob('ready-*')), [])
        self.assert_empty()

    async def test_parallel_resume_verifies_each_chunk_and_only_requests_missing_offsets(self):
        chunk = 512 * 1024
        data = bytes(range(256)) * 45000 + b'tail'
        self.message = NS(chat_id=1, id=2, document=NS(id=3), photo=None,
                          file=NS(size=len(data), ext='.mp4'))
        self.spool.close()
        self.spool = MediaSpool(self.root, max_bytes=len(data), reserve_bytes=1)
        waiting = asyncio.Event()
        attempts = []
        blocked = True

        class Parts:
            def __init__(self, offset, stride, **unused):
                self.offset, self.stride = offset, stride

            async def __anext__(self):
                offset = self.offset
                if blocked and offset >= 4 * chunk:
                    waiting.set()
                    await asyncio.Future()
                attempts.append(offset)
                self.offset += self.stride
                return data[offset:offset+chunk]

            async def close(self):
                pass

        client = NS(iter_download=lambda message, **kwargs: Parts(**kwargs))

        async def stage():
            async with self.spool.stage(client, self.message, workers=2, checkpoint_scope='job'):
                self.fail('interrupted download reached delivery')

        with patch.object(MediaSpool, 'checkpoint_minimum', 1):
            task = asyncio.create_task(stage())
            await waiting.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            [partial] = self.root.glob('partial-*')
            saved = {int(offset) for offset in json.loads((partial/'parts.json').read_text())['parts']}
            self.assertGreaterEqual(len(saved), 2)
            corrupted = min(saved)
            with (partial/'media.mp4').open('r+b') as file:
                file.seek(corrupted)
                file.write(b'bad')
            saved.remove(corrupted)
            self.spool.close()
            self.spool = MediaSpool(self.root, max_bytes=len(data), reserve_bytes=1)
            blocked = False
            attempts.clear()
            stats = {}
            async with self.spool.stage(client, self.message, workers=1, stats=stats,
                                        checkpoint_scope='job') as path:
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(stats['resumed_bytes'], len(saved) * chunk)
            self.assertEqual(set(attempts), set(range(0, len(data), chunk)) - saved)
            self.assertEqual(list(self.root.glob('partial-*')), [])

    async def test_file_survives_until_upload_confirmed_then_is_deleted(self):
        async def download(message, file):
            file.write(b'data')
        client = NS(download_media=AsyncMock(side_effect=download))
        async with self.spool.stage(client, self.message) as path:
            self.assertEqual(path.read_bytes(), b'data')
            self.assertEqual(len(list(self.root.glob('transfer-*'))), 1)
        self.assertFalse(path.exists())
        self.assert_empty()

    async def test_upload_failure_still_deletes_file(self):
        async def download(message, file):
            file.write(b'data')
        with self.assertRaises(RuntimeError):
            async with self.spool.stage(NS(download_media=download), self.message) as path:
                raise RuntimeError('upload failed')
        self.assertFalse(path.exists())
        self.assert_empty()

    async def test_cancelled_download_is_cleaned(self):
        started = asyncio.Event()

        async def download(message, file):
            file.write(b'pa')
            started.set()
            await asyncio.Future()

        async def transfer():
            async with self.spool.stage(NS(download_media=download), self.message):
                self.fail('download must be cancelled')

        task = asyncio.create_task(transfer())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_empty()

    async def test_low_space_prevents_download(self):
        client = NS(download_media=AsyncMock())
        with patch('media_spool.shutil.disk_usage', return_value=NS(free=4)):
            with self.assertRaises(InsufficientDiskError):
                async with self.spool.stage(client, self.message):
                    self.fail('must not stage')
        client.download_media.assert_not_awaited()
        self.assert_empty()

    async def test_unknown_size_download_cannot_exceed_limit(self):
        async def download(message, file):
            file.write(b'x' * 17)
        self.message.file.size = None
        with self.assertRaises(MediaTooLargeError):
            async with self.spool.stage(NS(download_media=download), self.message):
                self.fail('must not stage')
        self.assert_empty()

    async def test_known_oversized_file_is_rejected_before_download(self):
        self.message.file.size = 17
        client = NS(download_media=AsyncMock())
        with self.assertRaises(MediaTooLargeError):
            async with self.spool.stage(client, self.message):
                self.fail('must not stage')
        client.download_media.assert_not_awaited()
        self.assert_empty()

    async def test_disk_reserve_checked_while_writing(self):
        async def download(message, file):
            file.write(b'data')
        with patch('media_spool.shutil.disk_usage', side_effect=[NS(free=100), NS(free=4)]):
            with self.assertRaises(InsufficientDiskError):
                async with self.spool.stage(NS(download_media=download), self.message):
                    self.fail('must not stage')
        self.assert_empty()

    async def test_truncated_download_never_uploaded(self):
        async def download(message, file):
            file.write(b'bad')
        with self.assertRaises(IncompleteMediaError):
            async with self.spool.stage(NS(download_media=download), self.message):
                self.fail('must not stage')
        self.assert_empty()

    async def test_restart_removes_only_abandoned_transfer_directories(self):
        abandoned = self.root / 'transfer-abandoned'
        abandoned.mkdir()
        (abandoned / 'media.mp4').write_bytes(b'data')
        (self.root / 'keep.txt').write_text('preserve')
        self.spool.close()
        self.spool = MediaSpool(self.root, 16, 1)
        self.assert_empty()
        self.assertEqual((self.root / 'keep.txt').read_text(), 'preserve')

    async def test_second_process_cannot_clean_active_spool(self):
        active = self.root / 'transfer-active'
        active.mkdir()
        with self.assertRaises(SpoolBusyError):
            MediaSpool(self.root, 16, 1)
        self.assertTrue(active.exists())

    async def test_parallel_download_keeps_disk_guard_and_cleans_on_low_space(self):
        self.spool.close()
        self.spool = MediaSpool(self.root, max_bytes=12*1024**2, reserve_bytes=1)
        self.message.file.size = 11*1024**2
        self.message.document = True
        iterators = []

        def iterate(*args, **kwargs):
            iterator = NS(__anext__=AsyncMock(return_value=b'x'*(512*1024)), close=AsyncMock())
            iterators.append(iterator)
            return iterator

        calls = 0

        def space(*args):
            nonlocal calls
            calls += 1
            return NS(free=50*1024**2 if calls == 1 else 0)

        with patch('media_spool.shutil.disk_usage', side_effect=space):
            with self.assertRaises(InsufficientDiskError):
                async with self.spool.stage(NS(iter_download=iterate), self.message, workers=4):
                    self.fail('disk reserve must not be exceeded')
        self.assertTrue(iterators)
        for iterator in iterators:
            iterator.close.assert_awaited()
        self.assert_empty()
