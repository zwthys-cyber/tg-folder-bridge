import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telegram.error import RetryAfter,Forbidden
from telegram.ext import ApplicationHandlerStop
from core import Store
from notifications import Notifications


class Notices(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'index.sqlite3'
        self.app=Notifications();self.app.store=Store(self.path)
        self.bot=NS(send_message=AsyncMock());self.app.app=NS(bot=self.bot)

    async def asyncTearDown(self):
        self.app.store.db.close();self.tmp.cleanup()

    def complete(self,job='new'):
        self.app.store.create(job,7,'https://t.me/epan?start='+job)
        self.app.store.update(job,'complete',0)
        self.app.publish_ready()

    def queued(self):
        row=self.app.store.db.execute("SELECT * FROM notice_queue WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        return dict(row) if row else None

    async def test_only_opted_in_users_receive_one_notice_per_folder(self):
        self.app.set_subscription(7,True)
        self.complete();self.app.publish_ready()
        row=self.queued();self.assertEqual(row['owner'],7)
        await self.app.send_notice(row)
        self.assertIsNone(self.queued());self.bot.send_message.assert_awaited_once()
        keys=self.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual(keys[0][0].callback_data,'bridge_open:new:1')
        self.assertEqual(keys[1][0].callback_data,'bridge_notice:off')
        self.app.store.update('new','running',0);self.app.store.update('new','complete',0)
        self.app.publish_ready()
        self.assertEqual(self.app.store.db.execute('select count(*) from notice_queue').fetchone()[0],1)

    async def test_no_subscribers_and_late_subscribers_do_not_get_history(self):
        self.complete('old')
        self.assertIsNone(self.queued())
        self.app.store.create('just_complete',7,'link');self.app.store.update('just_complete','complete',0)
        self.app.set_subscription(7,True)
        self.assertIsNone(self.queued())
        self.complete();self.assertEqual(self.queued()['job'],'new')

    async def test_unsubscribe_cancels_queued_notices_and_keeps_preference_after_restart(self):
        self.app.set_subscription(7,True);self.complete()
        row=self.queued();self.app.set_subscription(7,False)
        self.assertIsNone(self.queued())
        await self.app.send_notice(row);self.bot.send_message.assert_not_awaited()
        self.app.store.db.close();self.app.store=Store(self.path)
        self.assertFalse(self.app.subscribed(7))
        self.complete('another');self.assertIsNone(self.queued())

    async def test_network_timeout_and_restart_never_blindly_resend(self):
        self.app.set_subscription(7,True);self.complete()
        self.bot.send_message.side_effect=TimeoutError()
        await self.app.send_notice(self.queued())
        self.app.recover_notices();self.assertIsNone(self.queued())
        self.assertEqual(self.app.store.db.execute('select state from notice_queue').fetchone()[0],'unknown')
        self.bot.send_message.assert_awaited_once()
        with self.app.store.db:self.app.store.db.execute("update notice_queue set state='sending'")
        self.app.recover_notices();self.assertIsNone(self.queued())

    async def test_explicit_flood_rejection_is_scheduled_and_bounded(self):
        self.app.set_subscription(7,True);self.complete()
        self.bot.send_message.side_effect=RetryAfter(5)
        for _ in range(4):await self.app.send_notice(self.queued())
        self.assertIsNone(self.queued())
        self.assertEqual(self.app.store.db.execute('select state from notice_queue').fetchone()[0],'failed')

    async def test_blocked_bot_disables_subscription_and_cancels_other_notices(self):
        self.app.set_subscription(7,True);self.complete();self.complete('another')
        self.bot.send_message.side_effect=Forbidden('blocked')
        await self.app.send_notice(self.queued())
        self.assertFalse(self.app.subscribed(7));self.assertIsNone(self.queued())

    async def test_migration_does_not_broadcast_existing_catalog(self):
        self.complete('old')
        with self.app.store.db:
            self.app.store.db.execute('drop table notice_events')
        self.app.store.db.close();self.app.store=Store(self.path)
        self.app.set_subscription(7,True);self.app.publish_ready()
        self.assertIsNone(self.queued())

    async def test_cancelled_send_retains_uncertainty(self):
        self.app.set_subscription(7,True);self.complete()
        self.bot.send_message.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await self.app.send_notice(self.queued())
        self.assertIsNone(self.queued())
        self.assertEqual(self.app.store.db.execute('select state from notice_queue').fetchone()[0],'unknown')

    async def test_private_subscription_commands_and_consumed_buttons(self):
        self.bot.edit_message_reply_markup=AsyncMock()
        query=NS(data='bridge_notice:on',message=NS(message_id=88),answer=AsyncMock())
        update=NS(effective_chat=NS(id=7,type='private'),effective_user=NS(id=7),
                  callback_query=query,message=NS(text='/notifications'))
        with self.assertRaises(ApplicationHandlerStop):await self.app.notifications_command(update,NS(bot=self.bot))
        self.assertFalse(self.app.subscribed(7))
        with self.assertRaises(ApplicationHandlerStop):await self.app.notice_callback(update,NS(bot=self.bot))
        self.assertTrue(self.app.subscribed(7))
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=7,message_id=88,reply_markup=None)
        update.message.text='/unsubscribe'
        with self.assertRaises(ApplicationHandlerStop):await self.app.notifications_command(update,NS(bot=self.bot))
        self.assertFalse(self.app.subscribed(7))
