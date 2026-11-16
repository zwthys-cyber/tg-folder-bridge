import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.ext import ApplicationHandlerStop
from telegram.error import RetryAfter
from bridge import Bridge
from core import Store, media_count, parse_link


class Parsing(unittest.TestCase):
    def test_link_and_target_allowlist(self):
        self.assertEqual(parse_link('https://t.me/epan?start=s_abc', {'epan'}), ('epan', 's_abc'))
        for url in ('https://t.me/other_bot?start=x', 'https://evil.test/epan?start=x',
                    'https://t.me/epan?start=x&start=y', 'https://t.me/epan?start='):
            with self.assertRaises(ValueError):
                parse_link(url, {'epan'})

    def test_count_media_not_total_messages(self):
        self.assertEqual(media_count('消息数：64 条\n图片数：2 张\n视频数：46 个\n文档数：15 个'), 63)
        self.assertIsNone(media_count('请先加入频道'))

    def test_persistence_restart_and_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'index.db'
            store = Store(path)
            store.create('job', 7, 'link')
            msg = NS(chat_id=7, message_id=11)
            store.add_file('job', (9, 10), msg, 'file-1', 'video')
            store.add_file('job', (9, 10), msg, 'file-1', 'video')
            self.assertEqual(len(store.files('job')), 1)
            store.db.close()
            store = Store(path)
            self.assertEqual(store.get('job')['state'], 'interrupted')
            self.assertEqual(store.files('job')[0]['file_id'], 'file-1')
            store.db.close()


    def test_migrate_existing_file_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.db'
            db = sqlite3.connect(path)
            db.execute('''CREATE TABLE files (job TEXT, source_chat INTEGER,
                source_message INTEGER, chat INTEGER, message INTEGER,
                file_id TEXT, kind TEXT, PRIMARY KEY(job,source_chat,source_message))''')
            db.execute("INSERT INTO files VALUES('old',1,2,3,4,'existing-id','video')")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.files('old')[0]['file_id'], 'existing-id')
            self.assertEqual(store.files('old')[0]['caption'], '')
            store.db.close()


class CloudTransfer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'OWNER_IDS': '7', 'RELAY_USER_ID': '7', 'API_ID': '1234',
            'API_HASH': 'a' * 32, 'DATA_DIR': self.tmp.name,
            'IDLE_SECONDS': '0.02', 'JOB_TIMEOUT_SECONDS': '5'})
        self.env.start()
        self.bridge = Bridge()
        self.bridge.destination = 'my_bot'
        self.bridge.store.create('job', 7, 'https://t.me/epan?start=s_abc')
        self.job = dict(id='job', expected=None, chats=set(), baselines={},
                        events=asyncio.Queue(maxsize=100), overflow=False)
        self.bridge.active = self.job

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        await self.bridge.client.disconnect()
        if self.bridge.spool:
            self.bridge.spool.close()
        self.env.stop()
        self.tmp.cleanup()

    async def test_follow_url_forward_cloud_and_record_bot_file_id(self):
        target = NS(url='https://t.me/SPXM111_bot?start=s_files', data=None, text='📥 获取文件')
        root = NS(chat_id=101, id=2, raw_text='图片数：1 张', photo=None,
                  document=None, buttons=[[target]])
        media = NS(chat_id=102, id=3, raw_text='', photo=True, document=None, buttons=[])

        async def send(entity, text):
            await self.job['events'].put(root if entity.id == 101 else media)

        async def forward(destination, message):
            self.assertEqual(destination, 'my_bot')
            received = NS(chat_id=7, message_id=99, forward_origin=NS(),
                          photo=[NS(file_id='own-bot-file-id')])
            update = NS(effective_message=received, effective_chat=NS(type='private'))
            with self.assertRaises(ApplicationHandlerStop):
                await self.bridge.receive_file(update, None)

        client = NS(
            get_entity=AsyncMock(side_effect=lambda name: NS(id=101 if name == 'epan' else 102, bot=True)),
            get_messages=AsyncMock(return_value=[]), send_message=AsyncMock(side_effect=send),
            forward_messages=AsyncMock(side_effect=forward), disconnect=AsyncMock())
        self.bridge.client = client
        await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.assertEqual(self.job['expected'], 1)
        self.assertEqual(client.send_message.await_count, 2)
        client.forward_messages.assert_awaited_once_with('my_bot', media)
        self.assertEqual(self.bridge.store.files('job')[0]['file_id'], 'own-bot-file-id')
        # No media download/upload methods exist on this fake client.
        self.bridge.client = NS(disconnect=AsyncMock())

    async def test_protected_forward_failure_blocks_next_job(self):
        media = NS(chat_id=101, id=3, raw_text='', photo=True, document=None, buttons=[])

        async def send(*args):
            await self.job['events'].put(media)

        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
                                get_messages=AsyncMock(return_value=[]),
                                send_message=AsyncMock(side_effect=send),
                                forward_messages=AsyncMock(side_effect=RuntimeError('protected')),
                                disconnect=AsyncMock())
        with self.assertRaises(RuntimeError):
            await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.assertTrue(self.bridge.blocked)
        self.assertEqual(self.bridge.store.files('job'), [])

    async def test_retrieve_by_own_file_id_without_original_message(self):
        bot = NS(send_video=AsyncMock())
        await self.bridge.send_stored(bot, 7, dict(
            kind='video', file_id='own-id', caption='saved caption'))
        bot.send_video.assert_awaited_once_with(chat_id=7, video='own-id',
                                               caption='saved caption', parse_mode=None)

    async def test_video_note_has_no_caption_parameter(self):
        bot = NS(send_video_note=AsyncMock())
        await self.bridge.send_stored(bot, 7, dict(kind='video_note', file_id='note-id'))
        bot.send_video_note.assert_awaited_once_with(chat_id=7, video_note='note-id')

    async def test_retrieve_respects_rate_limit(self):
        bot = NS(send_photo=AsyncMock(side_effect=[RetryAfter(1), NS()]))
        with patch('bridge.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.bridge.send_stored(bot, 7, dict(kind='photo', file_id='photo-id'))
            sleep.assert_awaited_once_with(2)
        self.assertEqual(bot.send_photo.await_count, 2)

    async def test_shutdown_preserves_interrupted_state(self):
        started = asyncio.Event()

        async def collect(*args):
            started.set()
            await asyncio.Future()

        self.bridge.collect = collect
        self.bridge.jobs.put_nowait('job')
        worker = asyncio.create_task(self.bridge.work())
        await started.wait()
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(self.bridge.store.get('job')['state'], 'interrupted')

    async def test_other_owner_cannot_retrieve_folder(self):
        self.bridge.owners.add(8)
        update = NS(effective_chat=NS(type='private', id=8),
                    effective_user=NS(id=8), message=NS(reply_text=AsyncMock()))
        bot = NS(send_photo=AsyncMock(), send_video=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.folder(update, NS(args=['job'], bot=bot))
        bot.send_video.assert_not_awaited()
        bot.send_photo.assert_not_awaited()

    async def test_reupload_receipt_records_own_id_and_removes_local_media(self):
        from media_spool import MediaSpool
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.spool = MediaSpool(Path(self.tmp.name) / 'spool', 1024, 1)
        media = NS(chat_id=101, id=3, raw_text='原始说明', photo=None,
                   document=NS(attributes=[]), file=NS(size=4, ext='.mp4'))
        local_paths = []

        async def download(message, file, **kwargs):
            file.write(b'data')

        async def upload(destination, file, caption, **kwargs):
            path = Path(file)
            local_paths.append(path)
            self.assertEqual(path.read_bytes(), b'data')
            received = NS(chat_id=7, message_id=100, forward_origin=None,
                          caption=caption, photo=None, video=NS(file_id='uploaded-own-id'))
            update = NS(effective_message=received, effective_chat=NS(type='private'))
            with self.assertRaises(ApplicationHandlerStop):
                await self.bridge.receive_file(update, None)
            # Cleanup waits until the complete delivery/receipt flow exits.
            self.assertTrue(path.exists())

        self.bridge.client = NS(download_media=AsyncMock(side_effect=download),
                                upload_file=AsyncMock(side_effect=lambda file, **kwargs: file),
                                send_file=AsyncMock(side_effect=upload),
                                disconnect=AsyncMock())
        await self.bridge.transfer_media(media, (101, 3), self.job)
        self.assertFalse(local_paths[0].exists())
        self.assertEqual(list(self.bridge.spool.root.glob('transfer-*')), [])
        saved = self.bridge.store.files('job')[0]
        self.assertEqual(saved['file_id'], 'uploaded-own-id')
        self.assertEqual(saved['caption'], '原始说明')
        self.bridge.client.upload_file.assert_awaited_once()
        self.assertEqual(self.bridge.client.upload_file.call_args.kwargs['part_size_kb'], 512)

    async def test_wrong_upload_marker_cannot_confirm_receipt(self):
        future = asyncio.get_running_loop().create_future()
        self.bridge.pending = dict(source=(101, 3), future=future,
                                   marker='expected-marker', caption='original')
        update = NS(effective_chat=NS(type='private'), effective_message=NS(
            caption='other-marker', forward_origin=None, photo=[NS(file_id='wrong-id')]))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.receive_file(update, None)
        self.assertFalse(future.done())
        self.assertEqual(self.bridge.store.files('job'), [])
        future.cancel()

    async def test_preview_receipt_does_not_increase_original_count(self):
        original = NS(chat_id=7, message_id=99, caption='说明')
        self.bridge.store.add_file('job', (101, 3), original, 'heic-original', 'document')
        future = asyncio.get_running_loop().create_future()
        self.bridge.pending = dict(source=(101, 3), future=future,
                                   marker='preview-token', preview=True)
        update = NS(effective_chat=NS(type='private'), effective_message=NS(
            caption='preview-token', photo=[NS(file_id='jpeg-preview')],
            chat_id=7, message_id=100))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.receive_file(update, None)
        files = self.bridge.store.files('job')
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['file_id'], 'heic-original')
        self.assertEqual(files[0]['preview_file_id'], 'jpeg-preview')
        self.assertTrue(future.result())

    async def test_retrieve_preview_with_original_button_and_original_download(self):
        bot = NS(send_photo=AsyncMock(), send_document=AsyncMock())
        item = dict(job='job', source_chat=101, source_message=3, kind='document',
                    file_id='heic-original', preview_file_id='jpeg-preview', caption='说明')
        await self.bridge.send_stored(bot, 7, item)
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'], 'jpeg-preview')
        markup = bot.send_photo.call_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, 'bridge_original:job:101:3')
        await self.bridge.send_stored(bot, 7, item, use_preview=False)
        self.assertEqual(bot.send_document.call_args.kwargs['document'], 'heic-original')

    async def test_progress_updates_persistent_status_without_sending_files(self):
        with patch('bridge.time.monotonic', side_effect=[100, 102]):
            progress = self.bridge.progress(self.job, '下载')
            progress(1024**2, 2 * 1024**2)
        row = self.bridge.store.get('job')
        self.assertIn('50.0%', row['detail'])
        self.assertIn('0.50 MiB/s', row['detail'])

    async def test_resume_matches_media_ids_even_when_source_message_id_changes(self):
        saved = NS(chat_id=7, message_id=99, caption='')
        self.bridge.store.add_file('job', (101, 3), saved, 'stored-video', 'video')
        old = NS(document=NS(id=12345), photo=None)
        incoming = NS(chat_id=101, id=50, raw_text='', photo=None,
                      document=NS(id=12345), buttons=[])

        async def get_messages(entity, **kwargs):
            return old if 'ids' in kwargs else [NS(id=49)]

        async def send(*args):
            await self.job['events'].put(incoming)

        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
                                get_messages=AsyncMock(side_effect=get_messages),
                                send_message=AsyncMock(side_effect=send), disconnect=AsyncMock())
        self.bridge.transfer_media = AsyncMock()
        await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.bridge.transfer_media.assert_not_awaited()
        self.assertEqual(len(self.bridge.store.files('job')), 1)
        self.assertEqual(self.bridge.store.files('job')[0]['media_key'], 'document:12345')

    async def test_resume_heic_generates_only_preview_for_existing_original(self):
        self.bridge.transfer_mode = 'download_upload'
        saved = NS(chat_id=7, message_id=99, caption='')
        self.bridge.store.add_file('job', (101, 3), saved, 'stored-heic', 'document',
                                   source_media_key='document:12345')
        incoming = NS(chat_id=101, id=50, raw_text='', photo=None,
                      document=NS(id=12345), file=NS(ext='.heic'), buttons=[])

        async def send(*args):
            await self.job['events'].put(incoming)

        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
                                get_messages=AsyncMock(return_value=[NS(id=49)]),
                                send_message=AsyncMock(side_effect=send), disconnect=AsyncMock())
        self.bridge.transfer_media = AsyncMock()
        await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.bridge.transfer_media.assert_awaited_once_with(incoming, (101, 3), self.job, preview_only=True)

    async def test_original_callback_checks_task_ownership(self):
        self.bridge.owners.add(8)
        query = NS(data='bridge_original:job:101:3', answer=AsyncMock())
        update = NS(callback_query=query, effective_user=NS(id=8), effective_chat=NS(type='private', id=8))
        bot = NS(send_document=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.original(update, NS(bot=bot))
        bot.send_document.assert_not_awaited()
        query.answer.assert_awaited_once_with('文件不存在或无权查看', show_alert=True)
