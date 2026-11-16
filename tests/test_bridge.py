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
