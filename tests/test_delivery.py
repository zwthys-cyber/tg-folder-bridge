import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.ext import ApplicationHandlerStop
from telegram.error import RetryAfter, Forbidden
from core import Store
from delivery import WholeDelivery, batches, display_order


class Runtime(WholeDelivery):
    def can_view(self,job,uid):
        return bool(job and (job['state']=='complete' or job['owner']==uid))


class WholeFolder(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'index.sqlite3'
        self.app=Runtime();self.app.store=Store(self.path)
        self.bot=NS(send_message=AsyncMock(),send_media_group=AsyncMock(),send_document=AsyncMock(),
                    send_video=AsyncMock(),send_photo=AsyncMock())
        self.app.app=NS(bot=self.bot)
        self.app.store.create('job',7,'link');self.app.store.update('job','complete',21)
        pattern=['document']*8+['video']*2+['document']+['video']*5+['document']+['video']*4
        for i,kind in enumerate(pattern):
            self.app.store.add_file('job',(1,i),NS(chat_id=7,message_id=i),str(i),kind,caption='')
        self.sleep=patch('delivery.asyncio.sleep',new_callable=AsyncMock);self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop();self.app.store.db.close();self.tmp.cleanup()

    async def enqueue(self,uid=7,origin=42):
        await self.app.queue_whole_folder(self.bot,uid,self.app.store.get('job'),origin)
        row=self.app.store.db.execute('SELECT * FROM deliveries ORDER BY rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    async def callback(self,row,action,uid=7):
        query=NS(data=f"bridge_delivery:{row['id']}:{action}",answer=AsyncMock())
        update=NS(callback_query=query,effective_user=NS(id=uid),effective_chat=NS(type='private',id=uid))
        with self.assertRaises(ApplicationHandlerStop):
            await self.app.delivery_callback(update,NS(bot=self.bot))
        return query

    async def test_one_click_sends_all_21_files_and_one_details_message(self):
        row=await self.enqueue();await self.app.execute_delivery(row)
        self.assertEqual(self.app.delivery(row['id'])['state'],'complete')
        progress=self.app.store.view(row['id'],7)
        self.assertEqual(progress.received_count(json.loads(row['plan'])),21)
        self.assertEqual(progress.message_count,22)
        texts=[c.args[1] for c in self.bot.send_message.await_args_list]
        self.assertEqual(sum('开始发送内容' in text for text in texts),1)
        self.assertIn('✅ 文件夹内容发送完成\n\n共发送 22 条消息',texts)
        self.assertFalse(any('下一组' in text for text in texts))
        self.assertEqual(self.bot.send_media_group.await_count,2)
        self.assertEqual(self.bot.send_document.await_count,0)
        self.assertEqual(self.bot.send_video.await_count,1)
        self.assertIsNone(self.app.store.bookmark(7,'job'))

    async def test_repeated_clicks_do_not_create_another_delivery(self):
        row=await self.enqueue();await self.enqueue()
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0],1)
        await self.app.execute_delivery(row);await self.enqueue()
        self.assertEqual(self.app.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0],1)

    async def test_stop_finishes_current_batch_then_continue_skips_confirmed_files(self):
        row=await self.enqueue()
        async def stop_during_group(**kwargs):
            self.app.delivery_state(row['id'],'stopping')
        self.bot.send_media_group.side_effect=stop_during_group
        await self.app.execute_delivery(row)
        self.assertEqual(self.app.delivery(row['id'])['state'],'paused')
        self.assertEqual(self.app.store.view(row['id'],7).received_count(json.loads(row['plan'])),10)
        self.bot.send_media_group.side_effect=None
        await self.callback(row,'resume')
        await self.app.execute_delivery(self.app.delivery(row['id']))
        self.assertEqual(self.app.store.view(row['id'],7).message_count,22)
        self.assertEqual(self.app.delivery(row['id'])['state'],'complete')

    async def test_unknown_batch_is_not_automatically_repeated_after_restart(self):
        row=await self.enqueue();self.bot.send_media_group.side_effect=TimeoutError()
        await self.app.execute_delivery(row)
        self.assertEqual(self.app.delivery(row['id'])['state'],'uncertain')
        self.assertTrue(self.app.delivery(row['id'])['pending'])
        self.app.recover_deliveries()
        await self.enqueue()
        self.assertEqual(self.app.delivery(row['id'])['state'],'uncertain')
        self.assertEqual(self.bot.send_media_group.await_count,1)

    async def test_received_confirmation_is_idempotent_and_remaining_files_continue(self):
        row=await self.enqueue();self.bot.send_media_group.side_effect=TimeoutError()
        await self.app.execute_delivery(row)
        await self.callback(row,'received');await self.callback(row,'received')
        self.assertEqual(self.app.store.view(row['id'],7).message_count,11)
        self.bot.send_media_group.side_effect=None
        await self.app.execute_delivery(self.app.delivery(row['id']))
        self.assertEqual(self.app.store.view(row['id'],7).message_count,22)

    async def test_another_user_cannot_stop_or_confirm_this_delivery(self):
        row=await self.enqueue();query=await self.callback(row,'stop',uid=8)
        query.answer.assert_awaited_once_with('此发送任务不可用',show_alert=True)
        self.assertEqual(self.app.delivery(row['id'])['state'],'queued')

    async def test_restart_with_unconfirmed_intent_requires_review(self):
        row=await self.enqueue()
        self.app.delivery_state(row['id'],'running',pending={'kind':'detail'})
        self.app.recover_deliveries()
        self.assertEqual(self.app.delivery(row['id'])['state'],'uncertain')
        self.app.delivery_state(row['id'],'running',pending={})
        self.app.recover_deliveries()
        self.assertEqual(self.app.delivery(row['id'])['state'],'queued')

    async def test_explicit_rate_limit_retries_without_double_counting(self):
        row=await self.enqueue();self.bot.send_media_group.side_effect=[RetryAfter(1),[],[]]
        await self.app.execute_delivery(row)
        self.assertEqual(self.app.delivery(row['id'])['state'],'complete')
        self.assertEqual(self.app.store.view(row['id'],7).message_count,22)

    async def test_blocked_user_is_paused_without_a_retry_loop(self):
        row=await self.enqueue();self.bot.send_media_group.side_effect=Forbidden('Blocked')
        await self.app.execute_delivery(row)
        self.assertEqual(self.app.delivery(row['id'])['state'],'paused')
        self.assertEqual(self.bot.send_media_group.await_count,1)

    async def test_documents_photos_and_videos_use_separate_albums(self):
        items=[{'kind':kind,'file_id':str(i)} for i,kind in enumerate(['photo','video','document','video'])]
        result=batches(items)
        self.assertEqual([len(x) for x in result],[1,1,1,1])
        self.assertEqual([x for group in result for x in group],items)

    async def test_new_plan_puts_original_files_first_without_changing_source_order(self):
        original=self.app.store.files('job')
        row=await self.enqueue();plan=json.loads(row['plan'])
        self.assertEqual([x['kind'] for x in plan],['document']*10+['video']*11)
        for kind in ('document','video'):
            self.assertEqual([x['file_id'] for x in plan if x['kind']==kind],
                             [x['file_id'] for x in original if x['kind']==kind])
        self.assertEqual(self.app.store.files('job'),original)
        ordered=display_order(original)
        self.assertIs(ordered[0],original[0])

    async def test_normal_delivery_buttons_have_no_stop_or_browsing_progress(self):
        row=await self.enqueue()
        labels=[b.text for line in self.app.delivery_keys(row).inline_keyboard for b in line]
        self.assertFalse(any('停止' in text or '浏览进度' in text or '继续浏览' in text for text in labels))
        await self.app.execute_delivery(row)
        for call in self.bot.send_message.await_args_list:
            keys=call.kwargs.get('reply_markup')
            if keys:
                self.assertFalse(any('停止' in b.text for line in keys.inline_keyboard for b in line))
