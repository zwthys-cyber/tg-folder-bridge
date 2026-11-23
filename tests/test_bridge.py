import asyncio
import os
import sqlite3
import tempfile
import unittest
import transfer_settings
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.ext import ApplicationHandlerStop
from telegram.error import BadRequest, RetryAfter, TimedOut
from bridge import Bridge
from core import Store, media_count, parse_link
from telethon.tl.types import DocumentAttributeVideo
from bot_api_config import LOCAL
from local_upload import DeliveryUnknownError, LocalBotUploadRejected


class Parsing(unittest.TestCase):
    def test_restart_marker_survives_graceful_stop_but_not_completion_or_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'index.sqlite3'
            store = Store(path)
            for name, state in [('running', 'running'), ('stopped', 'interrupted'),
                                ('done', 'complete'), ('failed', 'partial')]:
                store.create(name, 7, 'link')
                store.update(name, state, 63)
            store.db.close()
            store = Store(path)
            self.assertEqual(set(store.recoverable_jobs), {'running', 'stopped'})
            self.assertEqual(store.get('running')['state'], 'interrupted')
            self.assertEqual(store.get('stopped')['expected'], 63)
            self.assertEqual(store.get('done')['restart_pending'], 0)
            store.db.close()
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
    async def test_open_dismisses_only_matching_catalog_pair_after_card_succeeds(self):
        self.bridge.store.update('job','complete',0)
        self.bridge.store.register_catalog_panel(7,102,100)
        self.bridge.store.register_catalog_panel(8,102,99)
        query=NS(data='bridge_open:job:1',answer=AsyncMock(),
                 message=NS(text='📁 文件夹列表\n共 1 个文件夹',message_id=102,photo=None))
        update=NS(callback_query=query,effective_chat=NS(type='private',id=7),effective_user=NS(id=7))
        self.bridge.show_folder_card=AsyncMock()
        bot=NS(delete_message=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.catalog_callback(update,NS(bot=bot))
        self.assertEqual([c.kwargs['message_id'] for c in bot.delete_message.await_args_list],[100,102])
        self.assertEqual(self.bridge.store.catalog_panel_stats(8,102),99)
        self.assertIsNone(self.bridge.store.catalog_panel_stats(7,102))
        bot.delete_message.reset_mock()
        self.bridge.show_folder_card.side_effect=RuntimeError('cover send failed')
        query.message.message_id=103
        with self.assertRaises(RuntimeError):
            await self.bridge.catalog_callback(update,NS(bot=bot))
        bot.delete_message.assert_not_awaited()

    async def test_view_retires_only_an_accepted_delivery_card(self):
        self.bridge.store.update('job','complete',0)
        query=NS(data='bridge_view:job:1',answer=AsyncMock(),message=NS(message_id=901))
        update=NS(callback_query=query,effective_chat=NS(type='private',id=7),effective_user=NS(id=7))
        bot=NS(edit_message_reply_markup=AsyncMock())
        self.bridge.queue_whole_folder=AsyncMock(return_value=None)
        with self.assertRaises(ApplicationHandlerStop):await self.bridge.catalog_callback(update,NS(bot=bot))
        bot.edit_message_reply_markup.assert_not_awaited()
        self.bridge.queue_whole_folder.return_value=True
        with self.assertRaises(ApplicationHandlerStop):await self.bridge.catalog_callback(update,NS(bot=bot))
        bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=7,message_id=901,reply_markup=None)
        with self.assertRaises(ApplicationHandlerStop):await self.bridge.catalog_callback(update,NS(bot=bot))
        self.assertEqual(self.bridge.queue_whole_folder.await_count,2)

    async def test_photo_back_keeps_cover_and_cleans_old_buttons(self):
        query=NS(data='bridge_catalog:1',answer=AsyncMock(),message=NS(message_id=902,photo=[NS(file_id='cover')]))
        update=NS(callback_query=query,effective_chat=NS(type='private',id=7),effective_user=NS(id=7))
        bot=NS(send_message=AsyncMock(return_value=NS(message_id=903)),edit_message_reply_markup=AsyncMock(),delete_message=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):await self.bridge.catalog_callback(update,NS(bot=bot))
        bot.send_message.assert_awaited_once()
        bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=7,message_id=902,reply_markup=None)
        bot.delete_message.assert_not_awaited()

    async def test_catalog_pair_is_recorded_and_survives_store_reopen(self):
        bot=NS(send_message=AsyncMock(side_effect=[NS(message_id=201),NS(message_id=205)]))
        update=NS(effective_chat=NS(type='private',id=7))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.start_command(update,NS(bot=bot))
        self.assertEqual(self.bridge.store.catalog_panel_stats(7,205),201)
        path=self.bridge.store.db.execute('PRAGMA database_list').fetchone()[2]
        self.bridge.store.db.close();self.bridge.store=Store(path)
        self.assertEqual(self.bridge.store.catalog_panel_stats(7,205),201)

    async def test_folder_details_and_unknown_stats_are_not_deleted(self):
        bot=NS(delete_message=AsyncMock())
        await self.bridge.dismiss_catalog(bot,7,NS(message_id=20,text='📁 测试文件夹\n📖 内容统计：'))
        bot.delete_message.assert_not_awaited()
        await self.bridge.dismiss_catalog(bot,7,NS(message_id=22,text='📁 文件夹列表'))
        bot.delete_message.assert_awaited_once_with(chat_id=7,message_id=22)

    async def test_start_sends_statistics_then_catalog(self):
        bot=NS(send_message=AsyncMock())
        update=NS(effective_chat=NS(type='private',id=7))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.start_command(update,NS(bot=bot))
        messages=[c.args[1] for c in bot.send_message.await_args_list]
        self.assertEqual(len(messages),2)
        self.assertTrue(messages[0].startswith('📊 资源统计'))
        self.assertIn('文件夹：0 个',messages[0])
        self.assertTrue(messages[1].startswith('📁 文件夹列表'))

    async def test_upload_order_matches_source_even_when_native_video_arrives_later(self):
        media=[NS(chat_id=101,id=n,raw_text='',photo=None,document=NS(id=n),
                  media=NS(video=n==6),buttons=[]) for n in (5,6,7)]
        async def send(*args):
            for message in media:
                await self.job['events'].put(message)
        self.bridge.client=NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
            get_messages=AsyncMock(return_value=[]),send_message=AsyncMock(side_effect=send),
            disconnect=AsyncMock())
        self.bridge.transfer_media=AsyncMock()
        self.job['expected']=3
        await self.bridge.collect('https://t.me/epan?start=s_abc',self.job)
        self.assertEqual([call.args[1] for call in self.bridge.transfer_media.await_args_list],
                         [(101,5),(101,6),(101,7)])
        self.assertEqual([tuple(row) for row in self.bridge.store.db.execute(
            'SELECT source_message,position FROM source_entries WHERE job=? ORDER BY position',('job',))],
            [(5,1),(6,2),(7,3)])

    async def test_announced_source_waits_one_extra_window_for_first_media(self):
        detail=NS(chat_id=101,id=1,raw_text='开始发送内容',photo=None,document=None,buttons=[])
        media=NS(chat_id=101,id=2,raw_text='',photo=None,document=NS(id=123),media=NS(video=False),buttons=[])
        self.bridge.client=NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
            get_messages=AsyncMock(return_value=[]),send_message=AsyncMock(),disconnect=AsyncMock())
        self.bridge.transfer_media=AsyncMock()
        self.job['expected']=1
        self.job['events']=NS(get=AsyncMock(side_effect=[detail,asyncio.TimeoutError(),media]))
        await self.bridge.collect('https://t.me/epan?start=s_abc',self.job)
        self.assertEqual(self.job['events'].get.await_count,3)
        self.bridge.transfer_media.assert_awaited_once()

    async def test_explicit_fresh_manifest_skips_existing_media_at_new_source_positions(self):
        old = [NS(chat_id=101,id=i,document=NS(id=k),photo=None)
               for i,k in ((5,100),(6,200))]
        self.bridge.store.update('job','needs_review',2)
        self.bridge.store.save_source_plan('job',old)
        self.bridge.store.add_file('job',(101,5),NS(chat_id=7,message_id=88),
            'confirmed-original','document',source_media_key='document:100')
        fresh = [NS(chat_id=101,id=i,raw_text='',document=NS(id=k),photo=None,
                    media=NS(video=False),buttons=[]) for i,k in ((11,100),(12,200))]
        async def send(*args):
            for message in fresh:
                await self.job['events'].put(message)
        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
            get_messages=AsyncMock(return_value=[]),send_message=AsyncMock(side_effect=send),
            disconnect=AsyncMock())
        self.bridge.transfer_media = AsyncMock()
        self.job.update(expected=2,refresh_source=True)
        await self.bridge.collect('https://t.me/epan?start=s_abc',self.job)
        self.assertEqual(self.bridge.transfer_media.await_count,1)
        self.assertEqual(self.bridge.transfer_media.call_args.args[1],(101,12))
        self.assertEqual(self.bridge.store.files('job')[0]['file_id'],'confirmed-original')
        self.bridge.client.send_message.assert_awaited_once()
        self.assertEqual(self.bridge.store.saved_source_plan('job',2),[(101,11),(101,12)])

    async def test_exhausted_download_flood_skips_files_without_replaying_source(self):
        from telethon.errors import FloodWaitError
        media=[NS(chat_id=101,id=n,raw_text='',photo=None,document=NS(id=n),
                  media=NS(video=False),buttons=[]) for n in (11,12,13)]
        async def send(*args):
            for message in media:
                await self.job['events'].put(message)
        self.bridge.client=NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
            get_messages=AsyncMock(return_value=[]),send_message=AsyncMock(side_effect=send),
            disconnect=AsyncMock())
        async def transfer(message,key,job):
            job['phase']='下载'
            if message.id in (11,13):
                raise FloodWaitError(None,capture=1)
        self.bridge.transfer_media=AsyncMock(side_effect=transfer)
        self.bridge.await_cooldown=AsyncMock()
        self.job['expected']=3
        await self.bridge.collect_with_recovery('https://t.me/epan?start=s_abc',self.job)
        self.assertEqual(self.bridge.transfer_media.await_count,3)
        self.assertEqual(self.job['download_limited'],2)
        self.bridge.client.send_message.assert_awaited_once()

    async def test_source_history_recovers_media_missing_from_updates(self):
        detail=NS(chat_id=101,id=2,raw_text='开始发送内容',photo=None,document=None,buttons=[])
        media=NS(chat_id=101,id=3,raw_text='',photo=None,document=NS(id=123),
                 media=NS(video=False),buttons=[],out=False)
        self.bridge.client=NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
            get_messages=AsyncMock(side_effect=[[],[media]]),
            send_message=AsyncMock(),disconnect=AsyncMock())
        self.bridge.transfer_media=AsyncMock()
        self.job['expected']=1
        calls=0
        original_get=self.job['events'].get
        async def receive():
            nonlocal calls
            calls+=1
            if calls==1:return detail
            if calls in (2,3):raise asyncio.TimeoutError()
            return await original_get()
        self.job['events'].get=receive
        await self.bridge.collect('https://t.me/epan?start=s_abc',self.job)
        self.bridge.transfer_media.assert_awaited_once()
        self.assertEqual(self.bridge.client.get_messages.await_args.kwargs['min_id'],0)

    async def test_resume_uses_saved_complete_plan_and_only_transfers_missing_media(self):
        self.bridge.allowed={'epan'}
        media=[NS(chat_id=101,id=n,raw_text='',photo=None,document=NS(id=n),media=NS(video=False),buttons=[])
               for n in (11,12,13)]
        self.bridge.store.set_source_entries('job',[dict(source_chat=101,source_message=m.id,
            position=i,name=str(m.id),size=1,kind='document') for i,m in enumerate(media,1)])
        self.bridge.store.add_file('job',(101,1),NS(chat_id=7,message_id=1),'old','document',source_media_key='document:11')
        self.job['expected']=3
        self.bridge.client=NS(get_entity=AsyncMock(return_value=NS(id=101,bot=True)),
            get_messages=AsyncMock(return_value=media),send_message=AsyncMock(),disconnect=AsyncMock())
        self.bridge.transfer_media=AsyncMock()
        await self.bridge.collect('https://t.me/epan?start=s_abc',self.job)
        self.bridge.client.send_message.assert_not_awaited()
        self.assertEqual([call.args[1] for call in self.bridge.transfer_media.await_args_list],[(101,12),(101,13)])
        self.assertEqual(self.bridge.store.saved_source_plan('job',3),[(101,11),(101,12),(101,13)])

    async def test_foreign_view_session_and_unavailable_favorite_are_rejected(self):
        progress=self.bridge.store.start_view(7,'job')
        query=NS(data=f'bridge_page:{progress.id}:2',answer=AsyncMock(),message=NS(photo=None))
        update=NS(callback_query=query,effective_chat=NS(type='private',id=8),effective_user=NS(id=8))
        with patch('bridge.send_file_page',new_callable=AsyncMock) as send_page:
            with self.assertRaises(ApplicationHandlerStop):
                await self.bridge.catalog_callback(update,NS(bot=NS()))
            send_page.assert_not_awaited()
        query.data='bridge_fav:job'
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.catalog_callback(update,NS(bot=NS()))
        self.assertFalse(self.bridge.store.is_favorite(8,'job'))
        self.bridge.store.update('job','complete',0)
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.catalog_callback(update,NS(bot=NS()))
        self.assertTrue(self.bridge.store.is_favorite(8,'job'))
        self.assertFalse(self.bridge.store.is_favorite(7,'job'))

    async def test_open_shows_card_view_sends_files_and_photo_back_returns_catalog(self):
        self.bridge.store.update('job','complete',0)
        query=NS(data='bridge_open:job:1',answer=AsyncMock(),message=NS(photo=None,message_id=99),edit_message_text=AsyncMock())
        update=NS(callback_query=query,effective_chat=NS(type='private',id=7),effective_user=NS(id=7))
        bot=NS(send_message=AsyncMock())
        self.bridge.show_folder_card=AsyncMock()
        with patch('bridge.send_file_page',new_callable=AsyncMock) as send_page:
            with self.assertRaises(ApplicationHandlerStop):
                await self.bridge.catalog_callback(update,NS(bot=bot))
            self.bridge.show_folder_card.assert_awaited_once()
            send_page.assert_not_awaited()
            self.bridge.queue_whole_folder=AsyncMock()
            query.message.message_id=100
            query.data='bridge_view:job:1'
            with self.assertRaises(ApplicationHandlerStop):
                await self.bridge.catalog_callback(update,NS(bot=bot))
            self.bridge.queue_whole_folder.assert_awaited_once()
            send_page.assert_not_awaited()
            query.message.message_id=101
            query.data='bridge_catalog:1';query.message.photo=[NS(file_id='cover')]
            with self.assertRaises(ApplicationHandlerStop):
                await self.bridge.catalog_callback(update,NS(bot=bot))
            bot.send_message.assert_awaited_once()
            query.edit_message_text.assert_not_awaited()

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

    async def test_index_audit_finishes_cached_merge_and_never_uploads_unmatched_media(self):
        media = [NS(chat_id=101, id=n, raw_text='', photo=None,
                    document=NS(id=n), media=NS(video=n == 2), buttons=[]) for n in (1, 2, 3)]
        for n in (1, 2):
            self.bridge.store.add_file('job', (101, n), NS(chat_id=7, message_id=n),
                                       f'file{n}', 'document', source_media_key=f'document:{n}')
            self.bridge.store.set_analysis('job', (101, n), dict(content_sha256='same',
                stream_sha256='', quality=dict(native_source=n == 2, has_location=False)))
        async def send(*args):
            for message in media:
                await self.job['events'].put(message)
        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
            get_messages=AsyncMock(return_value=[]), send_message=AsyncMock(side_effect=send),
            disconnect=AsyncMock())
        self.bridge.transfer_media = AsyncMock()
        self.job.update(expected=3, dedup_audit=True)
        await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.bridge.transfer_media.assert_not_awaited()
        self.assertTrue(self.job['audit_unmatched'])
        self.assertEqual(self.bridge.store.files('job')[0]['file_id'], 'file2')
        self.assertEqual(self.bridge.store.processed_count('job'), 2)

    async def test_complete_folder_receipt_reuses_same_document_without_spooling(self):
        store = self.bridge.store
        store.create('origin', 7, 'https://t.me/epan?start=s_old')
        store.add_file('origin', (101, 10), NS(chat_id=7, message_id=77),
                       'confirmed-file', 'document', source_media_key='document:42')
        store.set_analysis('origin', (101, 10), dict(content_sha256='a'*64,
                           stream_sha256='', quality={'native_source': False}))
        size = 100 * 1024**2
        store.set_source_entries('origin', [dict(source_chat=101, source_message=10,
            position=1, name='same.mov', size=size, kind='video')])
        intent = store.begin_local_send('origin', (101, 10), 'bridge:origin:token')
        store.confirm_local_send(intent, store.db.commit)
        store.update('origin', 'complete', 1)
        media = NS(chat_id=101, id=20, raw_text='new caption', photo=None,
                   document=NS(id=42), media=NS(video=False), buttons=[],
                   file=NS(name='same.mov', size=size, mime_type='video/quicktime'))

        async def send(*args):
            await self.job['events'].put(media)

        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
            get_messages=AsyncMock(return_value=[]), send_message=AsyncMock(side_effect=send),
            disconnect=AsyncMock())
        self.bridge.spool = NS(stage=AsyncMock(), close=lambda: None)
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.upload_backend = 'hybrid'
        self.bridge.deliver = AsyncMock()
        self.job['expected'] = 1
        await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.bridge.spool.stage.assert_not_called()
        self.bridge.deliver.assert_not_awaited()
        [saved] = store.files('job')
        self.assertEqual((saved['file_id'], saved['message'], saved['caption']),
                         ('confirmed-file', 77, 'new caption'))
        store.create('other-owner', 8, 'https://t.me/epan?start=s_other')
        self.assertIsNone(store.reusable_document('other-owner', store.files('origin')[0] | {
            'name': 'same.mov', 'size': size, 'kind': 'video'}, 'document:42', 'document'))

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
        self.bridge.transfer_media.assert_awaited_once_with(incoming, (101, 3), self.job, replace_original=True)

    async def test_original_callback_checks_task_ownership(self):
        self.bridge.owners.add(8)
        query = NS(data='bridge_original:job:101:3', answer=AsyncMock())
        update = NS(callback_query=query, effective_user=NS(id=8), effective_chat=NS(type='private', id=8))
        bot = NS(send_document=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.original(update, NS(bot=bot))
        bot.send_document.assert_not_awaited()
        query.answer.assert_awaited_once_with('文件不存在或无权查看', show_alert=True)

    async def test_public_catalog_exposes_only_completed_folders(self):
        self.bridge.store.set_title('job', '测试文件夹')
        self.bridge.store.add_file('job', (1, 2), NS(chat_id=7, message_id=9), 'file', 'document')
        self.assertEqual(self.bridge.store.catalog(), [])
        self.assertFalse(self.bridge.can_view(self.bridge.store.get('job'), 999))
        self.bridge.store.update('job', 'complete', 1)
        rows = self.bridge.store.catalog()
        self.assertEqual(rows[0]['title'], '测试文件夹')
        self.assertEqual(rows[0]['file_count'], 1)
        self.assertTrue(self.bridge.can_view(self.bridge.store.get('job'), 999))

    async def test_menu_has_public_browse_and_private_admin_commands(self):
        bot = NS(set_my_commands=AsyncMock())
        await self.bridge.update_menu(bot)
        calls = bot.set_my_commands.call_args_list
        self.assertEqual([x.command for x in calls[0].args[0]],
                         ['start','page','favorites','notifications','help'])
        self.assertIn('fetch', [x.command for x in calls[-1].args[0]])

    async def test_next_group_reuses_button_and_message_id_without_repeat_clicks(self):
        root = NS(chat_id=101, id=1, raw_text='📁 测试文件夹\n图片数：3',
                  photo=None, document=None, buttons=[])
        media = [NS(chat_id=101, id=i+2, raw_text='', photo=None,
                    document=NS(id=100+i), buttons=[]) for i in range(3)]
        footer = []
        button = NS(text='➡️ 点击查看下一组', url=None, data=b'next', click=AsyncMock())
        for count in (22, 44, 64):
            footer.append(NS(chat_id=101, id=99, raw_text=f'当前批次发送完成\n已发送：{count}/64 条消息',
                             photo=None, document=None, buttons=[[button]]))

        async def start(*args):
            for item in (root, media[0], footer[0], footer[0]):
                await self.job['events'].put(item)

        async def click():
            index = button.click.await_count
            await self.job['events'].put(media[index])
            await self.job['events'].put(footer[index])

        async def save(message, source, job, **kwargs):
            self.bridge.store.add_file('job', source, NS(chat_id=7, message_id=message.id),
                                       f'file-{message.id}', 'document')

        button.click.side_effect = click
        self.bridge.client = NS(get_entity=AsyncMock(return_value=NS(id=101, bot=True)),
                                get_messages=AsyncMock(return_value=[]), send_message=AsyncMock(side_effect=start),
                                disconnect=AsyncMock())
        self.bridge.transfer_media = AsyncMock(side_effect=save)
        await self.bridge.collect('https://t.me/epan?start=s_abc', self.job)
        self.assertEqual(button.click.await_count, 2)
        self.assertEqual(len(self.bridge.store.files('job')), 3)
        self.assertEqual(self.bridge.store.get('job')['title'], '测试文件夹')

    async def test_source_prefetches_next_group_before_media_processing(self):
        handler = AsyncMock()
        self.job['handle_next_groups'] = handler
        self.job['chats'].add(101)
        self.job['baselines'][101] = 0
        message = NS(id=5, chat_id=101)
        await self.bridge.source_message(NS(id=5, chat_id=101, message=message))
        handler.assert_awaited_once_with(message, next_groups_only=True)
        self.assertIs(self.job['events'].get_nowait(), message)

    async def test_folder_search_includes_names_but_hides_unfinished_from_public(self):
        self.bridge.store.set_title('job', '测试文件夹1047')
        self.bridge.store.add_file('job', (1, 2), NS(chat_id=7, message_id=9), 'file', 'document')
        self.assertEqual(self.bridge.store.search_catalog('测试', 999), [])
        self.assertEqual(len(self.bridge.store.search_catalog('1047', 7, owned=True)), 1)
        self.bridge.store.update('job', 'complete', 1)
        self.assertEqual(len(self.bridge.store.search_catalog('测试', 999)), 1)
        self.bridge.app = NS(bot=NS(send_message=AsyncMock()))
        self.assertTrue(await self.bridge.search_text(999, 999, '1047'))
        self.assertIn('测试文件夹1047', self.bridge.app.bot.send_message.call_args.args[1])
        self.assertFalse(await self.bridge.search_text(999, 999, '其他关键词'))

    async def test_admin_can_change_both_live_limits_and_persist_them(self):
        from transfer_settings import load
        update = NS(effective_chat=NS(type='private'), effective_user=NS(id=7),
                    message=NS(reply_text=AsyncMock()))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.speed(update, NS(args=['32', '32']))
        self.assertEqual((self.bridge.download_workers, self.bridge.upload_workers), (32, 32))
        self.assertEqual(load(self.bridge.settings_file, 1, 1), (32, 32))

    async def test_public_user_cannot_change_limits(self):
        update = NS(effective_chat=NS(type='private'), effective_user=NS(id=999),
                    message=NS(reply_text=AsyncMock()))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.speed(update, NS(args=['32', '32']))
        self.assertFalse(self.bridge.settings_file.exists())

    async def test_transport_settings_require_owner_and_survive_speed_changes(self):
        update = NS(effective_chat=NS(type='private'), effective_user=NS(id=999),
                    message=NS(reply_text=AsyncMock()))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.transport_command(update, NS(args=['2']))
        self.assertEqual(self.bridge.upload_connections, 1)
        update.effective_user.id = 7
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.transport_command(update, NS(args=['2']))
        self.assertEqual(self.bridge.upload_connections, 2)
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.speed(update, NS(args=['8', '8']))
        import transfer_settings
        self.assertEqual(transfer_settings.connections(self.bridge.settings_file), 2)

    async def test_diagnostics_are_private_and_do_not_disclose_captions(self):
        self.bridge.store.record_transfer('job', '上传', dict(bytes=100, caption='private caption'))
        update = NS(effective_chat=NS(type='private'), effective_user=NS(id=999),
                    message=NS(reply_text=AsyncMock()))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.diagnostics(update, NS(args=[]))
        update.message.reply_text.assert_not_awaited()
        update.effective_user.id = 7
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.diagnostics(update, NS(args=[]))
        text = update.message.reply_text.call_args.args[0]
        self.assertIn('上传', text)
        self.assertNotIn('private caption', text)

    async def test_deployment_resume_is_one_time_deduplicated_and_retains_total(self):
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.store.update('job', 'interrupted', 63)
        request = self.bridge.settings_file.parent/'resume-once.json'
        request.write_text('["job", "job", "unknown"]')
        self.bridge.resume_after_upgrade()
        self.assertEqual(self.bridge.jobs.qsize(), 1)
        self.assertEqual(self.bridge.jobs.get_nowait(), 'job')
        self.assertEqual(self.bridge.store.get('job')['expected'], 63)
        self.assertFalse(request.exists())

    async def test_auto_command_sets_independent_bounded_controllers_and_persists(self):
        from transfer_settings import automatic
        update = NS(effective_chat=NS(type='private'), effective_user=NS(id=7),
                    message=NS(reply_text=AsyncMock()))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.speed(update, NS(args=['auto', '32', '24']))
        self.assertTrue(automatic(self.bridge.settings_file))
        self.assertEqual(self.bridge.worker_limit('下载'), 8)
        self.assertEqual(self.bridge.worker_limit('上传'), 8)
        self.bridge.tuners['下载'].current = 16
        self.assertEqual(self.bridge.worker_limit('下载'), 16)
        self.assertEqual(self.bridge.worker_limit('上传'), 8)
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.speed(update, NS(args=['4', '3']))
        self.assertFalse(self.bridge.automatic_speed)
        self.assertEqual(self.bridge.worker_limit('下载'), 4)
        self.assertEqual(self.bridge.worker_limit('上传'), 3)

    async def test_429_keeps_limits_and_only_reduces_active_window(self):
        from telethon.errors import InvalidBufferError
        from transfer_settings import automatic
        self.bridge.automatic_speed = True
        self.bridge.download_workers = self.bridge.upload_workers = 32
        transfer_settings.save(self.bridge.settings_file, 32, 32, True)
        self.bridge.tuners['上传'].current = 24
        self.job['phase'] = '上传'
        error = InvalidBufferError((-429).to_bytes(4, 'little', signed=True))
        self.assertTrue(self.bridge.defer_transport(error, 0))
        self.assertTrue(automatic(self.bridge.settings_file))
        self.assertLessEqual(self.bridge.worker_limit('上传'), 8)
        self.assertEqual((self.bridge.download_workers, self.bridge.upload_workers), (32, 32))
        self.assertGreater(self.bridge.cooldown.remaining(), 100)

    async def test_source_flood_wait_does_not_permanently_degrade_media_settings(self):
        from telethon.errors import FloodWaitError
        from transfer_settings import save
        self.bridge.download_workers, self.bridge.upload_workers = 8, 4
        save(self.bridge.settings_file, 8, 4, False, 1)
        original = self.bridge.settings_file.read_bytes()
        self.job['phase'] = '获取文件列表'
        self.assertTrue(self.bridge.defer_transport(FloodWaitError(None, capture=1), 0))
        self.assertEqual(self.bridge.settings_file.read_bytes(), original)
        self.assertEqual((self.bridge.download_workers, self.bridge.upload_workers), (8, 4))

    async def test_progress_feeds_only_its_direction_before_display_throttling(self):
        from unittest.mock import Mock
        self.bridge.automatic_speed = True
        self.bridge.tuners['上传'].observe = Mock()
        self.bridge.tuners['下载'].observe = Mock()
        self.job['upload_stats'] = dict(workers=8, parts=40, wait_seconds=1)
        callback = self.bridge.progress(self.job, '上传')
        callback(100, 1000)
        callback(200, 1000)
        self.assertEqual(self.bridge.tuners['上传'].observe.call_count, 2)
        self.bridge.tuners['下载'].observe.assert_not_called()
        self.assertIn('自动调速', self.bridge.store.get('job')['detail'])

    async def test_429_cools_down_reconnects_then_replays_with_saved_files(self):
        from telethon.errors import InvalidBufferError
        from transport_recovery import Cooldown
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.download_workers = self.bridge.upload_workers = 32
        self.job['expected'] = 63
        self.bridge.store.add_file('job', (101, 3), NS(chat_id=7, message_id=99), 'saved', 'document')
        self.bridge.client = NS(disconnect=AsyncMock())
        operations, gates = [], []

        async def operation(link, job):
            operations.append(job['id'])
            self.assertEqual(job['expected'], 63)
            if len(operations) == 1:
                job['phase'] = '上传'
                self.bridge.blocked = True
                raise InvalidBufferError((-429).to_bytes(4, 'little', signed=True))
            self.assertEqual(len(self.bridge.store.files('job')), 1)

        async def gate(job):
            gates.append(self.bridge.cooldown.remaining())
            self.bridge.cooldown.until = 0  # Simulate elapsed wall time, no real delay.

        async def reconnect():
            self.assertEqual(self.bridge.cooldown.remaining(), 0)
            self.bridge.reconnect_required = False

        self.bridge.collect = AsyncMock(side_effect=operation)
        self.bridge.await_cooldown = AsyncMock(side_effect=gate)
        self.bridge.connect_user = AsyncMock(side_effect=reconnect)
        await self.bridge.collect_with_recovery('link', self.job)
        self.assertEqual(operations, ['job', 'job'])
        self.assertGreater(gates[1], 100)
        self.assertEqual((self.bridge.download_workers, self.bridge.upload_workers), (32, 32))
        self.bridge.client.disconnect.assert_awaited_once()
        self.bridge.connect_user.assert_awaited_once()
        self.assertFalse(self.bridge.blocked)
        self.assertGreater(Cooldown(self.bridge.cooldown.path).remaining(), 100)

    async def test_download_429_does_not_degrade_healthy_upload_settings(self):
        from telethon.errors import InvalidBufferError
        import transfer_settings
        self.bridge.download_workers, self.bridge.upload_workers = 32, 8
        self.bridge.upload_connections = 2
        transfer_settings.save(self.bridge.settings_file, 32, 8, upload_connections=2)
        self.job['phase'] = '下载'
        self.bridge.defer_transport(InvalidBufferError((-429).to_bytes(4, 'little', signed=True)), 0)
        self.assertEqual(self.bridge.download_workers, 32)
        self.assertEqual(self.bridge.upload_workers, 8)
        self.assertEqual(self.bridge.upload_connections, 2)
        self.assertEqual(transfer_settings.connections(self.bridge.settings_file), 2)
        self.assertGreater(self.bridge.cooldown.remaining(), 100)

    async def test_broken_connection_is_rebuilt_before_replaying(self):
        self.bridge.transfer_mode = 'download_upload'
        self.job['expected'] = 63
        self.bridge.client = NS(disconnect=AsyncMock())
        self.bridge.collect = AsyncMock(side_effect=[ConnectionError(), None])
        self.bridge.await_cooldown = AsyncMock()
        self.bridge.connect_user = AsyncMock()
        await self.bridge.collect_with_recovery('link', self.job)
        self.bridge.connect_user.assert_awaited_once()
        self.assertEqual(self.bridge.collect.await_count, 2)
        self.assertEqual(self.job['expected'], 63)

    async def test_auth_transport_error_does_not_delete_or_recreate_session(self):
        from telethon.errors import InvalidBufferError
        self.bridge.collect = AsyncMock(side_effect=InvalidBufferError((-404).to_bytes(4, 'little', signed=True)))
        self.bridge.connect_user = AsyncMock()
        with self.assertRaises(InvalidBufferError):
            await self.bridge.collect_with_recovery('link', self.job)
        self.bridge.collect.assert_awaited_once()
        self.bridge.connect_user.assert_not_awaited()
        self.assertFalse(self.bridge.cooldown.path.exists())

    async def test_known_expected_count_survives_an_early_failure(self):
        self.bridge.store.update('job', 'partial', 63)
        self.bridge.collect = AsyncMock(side_effect=ValueError('early failure'))
        self.bridge.app = NS(bot=NS(send_message=AsyncMock()))
        self.bridge.jobs.put_nowait('job')
        worker = asyncio.create_task(self.bridge.work())
        await self.bridge.jobs.join()
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(self.bridge.store.get('job')['expected'], 63)

    async def test_silent_short_delivery_schedules_recovery_and_completion_cancels_it(self):
        self.bridge.transfer_mode='download_upload'
        self.bridge.store.update('job','partial',3)
        self.bridge.collect=AsyncMock()
        self.bridge.app=NS(bot=NS(send_message=AsyncMock()))
        self.bridge.jobs.put_nowait('job')
        worker=asyncio.create_task(self.bridge.work())
        try:
            await asyncio.wait_for(self.bridge.jobs.join(),2)
            row=self.bridge.store.get('job')
            self.assertEqual(row['state'],'needs_review')
            self.assertIn('自动排队补收',row['detail'])
            self.assertEqual(self.bridge.store.db.execute('select attempts from retry_schedule').fetchone()[0],1)
            with patch.object(self.bridge.store,'processed_count',return_value=3):
                self.bridge.jobs.put_nowait('job')
                await asyncio.wait_for(self.bridge.jobs.join(),2)
            self.assertEqual(self.bridge.store.get('job')['state'],'complete')
            self.assertEqual(self.bridge.store.db.execute('select next_at from retry_schedule').fetchone()[0],0)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):await worker

    async def test_cooling_job_cannot_be_queued_again(self):
        self.bridge.store.update('job', 'cooldown', 63)
        update = NS(effective_chat=NS(type='private'), effective_user=NS(id=7),
                    message=NS(reply_text=AsyncMock()))
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.resume(update, NS(args=['job']))
        self.assertTrue(self.bridge.jobs.empty())

    async def test_restore_only_eligible_owned_jobs_and_deduplicates_requests(self):
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.store.update('job', 'running', 63)
        self.bridge.store.create('other', 999, 'link')
        self.bridge.store.create('finished', 7, 'link')
        self.bridge.store.update('finished', 'complete', 1)
        self.bridge.store.db.close()
        self.bridge.store = Store(Path(self.tmp.name)/'index.sqlite3')
        await self.bridge.restore_jobs()
        await self.bridge.restore_jobs()
        self.assertEqual(self.bridge.jobs.qsize(), 1)
        self.assertEqual(self.bridge.jobs.get_nowait(), 'job')
        self.assertEqual(self.bridge.store.get('job')['expected'], 63)
        self.assertEqual(self.bridge.store.get('other')['state'], 'interrupted')

    async def test_automatic_restore_can_be_disabled_and_never_replays_forwards(self):
        self.bridge.store.recoverable_jobs = ['job']
        self.bridge.store.update('job', 'interrupted', 63)
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.auto_resume = False
        await self.bridge.restore_jobs()
        self.assertTrue(self.bridge.jobs.empty())
        self.bridge.auto_resume = True
        self.bridge.transfer_mode = 'forward'
        await self.bridge.restore_jobs()
        self.assertTrue(self.bridge.jobs.empty())

    async def test_download_flood_retries_only_stage_entry(self):
        from contextlib import asynccontextmanager
        from telethon.errors import FloodWaitError

        job = dict(id='job', expected=1, download_stats={})
        workers = []
        @asynccontextmanager
        async def stage(*args, **kwargs):
            workers.append(kwargs['workers'])
            if len(workers) == 1:
                raise FloodWaitError(None, capture=3)
            yield 'original'
        with patch.object(self.bridge, 'spool', NS(stage=stage)), patch.object(
                self.bridge, 'await_cooldown', new_callable=AsyncMock) as wait:
            with self.assertRaises(FloodWaitError):
                async with self.bridge.staged_download(NS(), job) as path:
                    self.assertEqual(path, 'original')
                    raise FloodWaitError(None, capture=5)
        self.assertEqual(workers, [self.bridge.download_workers, 1])
        wait.assert_awaited_once_with(job)

    async def test_download_refreshes_only_identical_source_media_and_retries_once(self):
        from contextlib import asynccontextmanager
        from telethon.errors import FloodWaitError
        original = NS(chat_id=101, id=7, document=NS(id=55), photo=None)
        fresh = NS(chat_id=101, id=7, document=NS(id=55), photo=None)
        self.bridge.client = NS(get_messages=AsyncMock(return_value=fresh), disconnect=AsyncMock())
        seen = []
        @asynccontextmanager
        async def stage(client, message, **kwargs):
            seen.append(message)
            raise FloodWaitError(None, capture=1)
            yield
        job = dict(id='job', expected=1, download_stats={})
        with patch.object(self.bridge, 'spool', NS(stage=stage)), patch.object(
                self.bridge, 'await_cooldown', new_callable=AsyncMock):
            with self.assertRaises(FloodWaitError):
                async with self.bridge.staged_download(original, job):
                    self.fail('No complete file exists')
        self.assertEqual(seen, [original, fresh])
        self.bridge.client.get_messages.assert_awaited_once_with(101, ids=7)

    async def test_download_refresh_cannot_substitute_different_media(self):
        from contextlib import asynccontextmanager
        from telethon.errors import FloodWaitError
        original = NS(chat_id=101, id=7, document=NS(id=55), photo=None)
        changed = NS(chat_id=101, id=7, document=NS(id=56), photo=None)
        self.bridge.client = NS(get_messages=AsyncMock(return_value=changed), disconnect=AsyncMock())
        seen = []
        @asynccontextmanager
        async def stage(client, message, **kwargs):
            seen.append(message)
            if len(seen) == 1:
                raise FloodWaitError(None, capture=1)
            yield 'original'
        job = dict(id='job', expected=1, download_stats={})
        with patch.object(self.bridge, 'spool', NS(stage=stage)), patch.object(
                self.bridge, 'await_cooldown', new_callable=AsyncMock):
            async with self.bridge.staged_download(original, job) as path:
                self.assertEqual(path, 'original')
        self.assertEqual(seen, [original, original])

    async def test_hybrid_small_file_bypasses_user_relay_and_pending_receipt(self):
        self.bridge.upload_backend = 'hybrid'
        self.bridge.deliver_local = AsyncMock()
        path = Path(self.tmp.name)/'small.bin'; path.write_bytes(b'data')
        await self.bridge.deliver(NS(), (1, 2), marker='nonce', upload_path=path)
        self.bridge.deliver_local.assert_awaited_once()
        self.assertIsNone(self.bridge.pending)

    async def test_hybrid_retries_explicit_video_rejection_as_original_document(self):
        self.bridge.active = dict(id='job', expected=1)
        path = Path(self.tmp.name)/'original.mov'
        path.write_bytes(b'original')
        media = NS(file_id='document-id', file_size=8, thumbnail=None)
        reply = NS(chat_id=7, message_id=4, document=media)
        send_video = AsyncMock(side_effect=BadRequest('VIDEO_FILE_INVALID'))
        send_document = AsyncMock(return_value=reply)
        self.bridge.app = NS(bot=NS(local_mode=True, base_url=LOCAL+'/botTEST',
                                    send_video=send_video, send_document=send_document))
        message = NS(raw_text='', media=NS(video=True), photo=None, document=None)
        attrs = [DocumentAttributeVideo(10, 128, 72, supports_streaming=True)]
        await self.bridge.deliver_local(message, (1, 2), 'nonce', path, attrs, False, None, False)
        send_video.assert_awaited_once()
        send_document.assert_awaited_once()
        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual([row['kind'] for row in self.bridge.store.files('job')], ['document'])
        states = self.bridge.store.db.execute('SELECT state FROM local_send_intents ORDER BY id')
        self.assertEqual([row[0] for row in states], ['rejected', 'confirmed'])

    async def test_hybrid_switches_to_relay_after_confirmed_document_rejection(self):
        self.bridge.upload_backend = 'hybrid'
        path = Path(self.tmp.name)/'original.mov'
        path.write_bytes(b'original')
        send_document = AsyncMock(side_effect=BadRequest('unexpected server rejection'))
        self.bridge.app = NS(bot=NS(local_mode=True, base_url=LOCAL+'/botTEST',
                                    send_document=send_document))
        self.bridge.get_upload_transport = AsyncMock(return_value=None)

        async def send_relay(destination, uploaded, **kwargs):
            self.assertFalse(self.bridge.store.unknown_local_send('job'))
            self.assertEqual(self.bridge.pending['marker'], 'nonce')
            self.assertTrue(kwargs['force_document'])
            self.bridge.pending['future'].set_result(None)

        with patch.object(self.bridge.client, 'send_file', new=AsyncMock(side_effect=send_relay)) as relay:
            with patch('bridge.upload_file', new=AsyncMock(return_value='uploaded')) as upload:
                await self.bridge.deliver(NS(raw_text='', photo=None), (1, 2),
                                          marker='nonce', upload_path=path)
        send_document.assert_awaited_once()
        upload.assert_awaited_once()
        relay.assert_awaited_once()
        self.assertIsNone(self.bridge.pending)

    async def test_hybrid_does_not_fallback_on_unknown_or_chat_rejection(self):
        self.bridge.active = dict(id='job', expected=1)
        path = Path(self.tmp.name)/'original.mov'
        path.write_bytes(b'original')
        send_video = AsyncMock(side_effect=BadRequest('chat not found'))
        send_document = AsyncMock()
        self.bridge.app = NS(bot=NS(local_mode=True, base_url=LOCAL+'/botTEST',
                                    send_video=send_video, send_document=send_document))
        message = NS(raw_text='', media=NS(video=True), photo=None)
        attrs = [DocumentAttributeVideo(10, 128, 72)]
        with self.assertRaises(LocalBotUploadRejected):
            await self.bridge.deliver_local(message, (1, 2), 'nonce', path, attrs, False, None, False)
        send_video.side_effect = TimedOut()
        with self.assertRaises(DeliveryUnknownError):
            await self.bridge.deliver_local(message, (1, 2), 'nonce', path, attrs, False, None, False)
        send_document.assert_not_awaited()
        self.assertTrue(self.bridge.store.unknown_local_send('job'))

    async def test_unknown_local_delivery_is_not_replayed_after_restart(self):
        self.bridge.transfer_mode = 'download_upload'
        self.bridge.store.update('job', 'interrupted', 63)
        self.bridge.store.begin_local_send('job', (1, 2), 'nonce')
        self.bridge.store.recoverable_jobs = ['job']
        await self.bridge.restore_jobs()
        self.assertTrue(self.bridge.jobs.empty())
        self.assertEqual(self.bridge.store.get('job')['state'], 'needs_review')
