import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlparse, unquote
from unittest.mock import AsyncMock, patch
from telegram.error import BadRequest, TimedOut, RetryAfter
from telegram.ext import Application
from bot_api_config import configure, endpoint, LOCAL
from core import Store
from local_upload import (eligible, safe_filename, send_local, DeliveryUnknownError,
                          LocalBotRateLimitError, LocalBotUploadRejected)


class Config(unittest.TestCase):
    def test_exact_boundary_and_untrusted_endpoint(self):
        self.assertTrue(eligible(2_000_000_000))
        self.assertFalse(eligible(2_000_000_001))
        self.assertFalse(eligible(0))
        with self.assertRaises(ValueError):
            endpoint('http://other.example')

    def test_real_builder_uses_same_local_api_and_file_endpoint(self):
        with patch.dict(os.environ, BOT_API_BASE=LOCAL):
            app = configure(Application.builder().token('123456:TEST_TOKEN').updater(None)).build()
            self.assertTrue(app.bot.local_mode)
            self.assertTrue(app.bot.base_url.startswith(LOCAL+'/bot'))
            self.assertTrue(app.bot.base_file_url.startswith(LOCAL+'/file/bot'))


class Delivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'media.bin'; self.path.write_bytes(b'original bytes')
        self.store = Store(self.root/'index.sqlite3')
        self.store.create('job', 7, 'link')
        self.media = NS(file_id='this-bots-own-id', file_size=self.path.stat().st_size, thumbnail=True)
        self.reply = NS(chat_id=7, message_id=100, document=self.media, photo=[])
        self.bot = NS(local_mode=True, base_url=LOCAL+'/botTOKEN', send_document=AsyncMock())
        self.saved_path = None
        async def send(**kw):
            self.assertIsInstance(kw['document'], str)
            self.saved_path = Path(unquote(urlparse(kw['document']).path))
            self.assertEqual(self.saved_path.read_bytes(), self.path.read_bytes())
            self.assertEqual(self.saved_path.stat().st_ino, self.path.stat().st_ino)
            self.assertEqual(self.saved_path.name, 'original.mov')
            return self.reply
        self.bot.send_document.side_effect = send

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def deliver(self):
        return await send_local(self.bot, self.store, 'job', (101, 3), 7, self.path,
            'nonce', 'original.mov', save=lambda reply, kind, media: self.store.add_file(
                'job', (101, 3), reply, media.file_id, kind))

    async def test_uri_upload_returns_own_file_id_and_commits_exactly_one_record(self):
        await self.deliver()
        self.assertFalse(self.saved_path.exists())
        self.assertEqual(self.path.read_bytes(), b'original bytes')
        self.assertEqual(len(self.store.files('job')), 1)
        self.assertFalse(self.store.unknown_local_send('job'))
        self.assertEqual(self.store.files('job')[0]['file_id'], 'this-bots-own-id')

    async def test_network_error_is_durable_unknown_and_never_automatically_resubmitted(self):
        self.bot.send_document.side_effect = TimedOut()
        with self.assertRaises(DeliveryUnknownError):
            await self.deliver()
        self.assertTrue(self.store.unknown_local_send('job'))
        with self.assertRaises(RuntimeError):
            await self.deliver()
        self.assertEqual(self.bot.send_document.await_count, 1)
        self.assertEqual(self.store.files('job'), [])
        self.store.authorize_local_retry('job')
        self.assertFalse(self.store.unknown_local_send('job'))

    async def test_wrong_receipt_and_db_failure_preserve_uncertainty(self):
        self.media.file_size = 1
        with self.assertRaises(DeliveryUnknownError):
            await self.deliver()
        self.assertTrue(self.store.unknown_local_send('job'))
        self.store.authorize_local_retry('job')
        self.media.file_size = self.path.stat().st_size
        with patch.object(self.store, 'add_file', side_effect=RuntimeError('db failed')):
            with self.assertRaises(DeliveryUnknownError):
                await self.deliver()
        self.assertTrue(self.store.unknown_local_send('job'))

    async def test_explicit_api_rejections_are_known_failures_and_rate_wait_is_preserved(self):
        for exc in (BadRequest('rejected'), RetryAfter(17)):
            self.bot.send_document.side_effect = exc
            with self.assertRaises((BadRequest, LocalBotRateLimitError)) as error:
                await self.deliver()
            self.assertFalse(self.store.unknown_local_send('job'))
            if isinstance(error.exception, LocalBotRateLimitError):
                self.assertEqual(error.exception.seconds, 17)

    async def test_upload_rejection_exposes_only_the_method_and_fixed_reason(self):
        self.bot.send_document.side_effect = BadRequest('realpath failed for /private/file.mov')
        with self.assertRaises(LocalBotUploadRejected) as rejected:
            await self.deliver()
        self.assertEqual(rejected.exception.method, 'sendDocument')
        self.assertEqual(rejected.exception.reason, 'local_file')
        self.assertNotIn('/private', str(rejected.exception))
        self.assertFalse(self.store.unknown_local_send('job'))

    async def test_rejection_from_index_write_cannot_authorize_duplicate_upload(self):
        with patch.object(self.store, 'add_file', side_effect=BadRequest('video_file_invalid')):
            with self.assertRaises(DeliveryUnknownError):
                await self.deliver()
        self.assertTrue(self.store.unknown_local_send('job'))

    async def test_cancel_after_submission_blocks_duplicate_and_cleans_handoff(self):
        gate = asyncio.Event()
        async def wait(**kwargs):
            gate.set()
            await asyncio.Future()
        self.bot.send_document.side_effect = wait
        task = asyncio.create_task(self.deliver())
        await gate.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.store.unknown_local_send('job'))
        self.assertEqual(list(self.root.glob('upload-*')), [])

    async def test_native_video_is_sent_as_video_without_copying_or_converting_bytes(self):
        self.reply.video = self.media
        self.bot.send_video = AsyncMock(return_value=self.reply)
        await send_local(self.bot,self.store,'job',(101,3),7,self.path,'nonce','original.mov',
            video={'duration':1,'width':128,'height':72,'supports_streaming':True},
            save=lambda reply,kind,media:self.store.add_file('job',(101,3),reply,media.file_id,kind))
        self.bot.send_document.assert_not_awaited()
        self.assertEqual(self.store.files('job')[0]['kind'],'video')
        self.assertTrue(self.bot.send_video.call_args.kwargs['supports_streaming'])
        self.assertEqual(self.path.read_bytes(),b'original bytes')

    def test_filename_cannot_escape_handoff_directory(self):
        for name in ('../../secret', '\\root\\secret', '\x00bad', '..', ''):
            self.assertNotIn('/', safe_filename(name))
            self.assertNotIn('\\', safe_filename(name))
            self.assertNotIn('\x00', safe_filename(name))
