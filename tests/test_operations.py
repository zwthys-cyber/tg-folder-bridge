import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telegram.ext import ApplicationHandlerStop
import automation_control as control
from backup_metadata import backup
from browse_navigation import BrowseNavigation
from catalog_ui import send_file_page, send_folder_card
from core import Store
from operations import Operations


class Runtime(Operations, BrowseNavigation):
    pass


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Runtime()
        self.app.settings_file = self.root / 'performance.json'
        self.app.store = Store(self.root / 'index.sqlite3')
        self.app.owners = {7}
        self.app.active = None
        self.app.jobs = asyncio.Queue(20)
        self.app.blocked = False
        self.app.transfer_mode = 'download_upload'
        self.app.cooldown = NS(remaining=lambda:0)
        self.app.download_workers,self.app.upload_workers=12,8
        self.app.search_sessions={}
        self.app.pending_jumps={}
        self.app.store.create('job',7,'link')
        self.app.store.update('job','partial',3,'ConnectionError')
        self.update=NS(effective_chat=NS(id=7,type='private'),effective_user=NS(id=7),
                       message=NS(reply_text=AsyncMock(),reply_to_message=None))

    async def asyncTearDown(self):
        self.app.store.db.close()
        self.tmp.cleanup()

    async def test_non_owner_cannot_use_admin_controls(self):
        self.update.effective_user.id=8
        self.update.callback_query=NS(data='bridge_admin:transfers',answer=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop):
            await self.app.admin_callback(self.update,NS())
        self.assertFalse(control.paused(self.root,'transfers'))
        self.update.callback_query.answer.assert_awaited_once_with('仅管理员可用',show_alert=True)

    async def test_manual_retry_is_owner_checked_and_idempotent(self):
        self.assertIn('没有',self.app.queue_retry(8,'job'))
        self.assertIn('已排队',self.app.queue_retry(7,'job'))
        self.assertIn('无需重复',self.app.queue_retry(7,'job'))
        self.assertEqual(self.app.jobs.qsize(),1)

    async def test_unknown_delivery_is_never_automatically_retried(self):
        self.app.store.begin_local_send('job',(1,2),'token')
        self.app.schedule_retry('job',ConnectionError())
        self.assertEqual(self.app.store.db.execute('select count(*) from retry_schedule').fetchone()[0],0)
        self.assertIn('结果未知',self.app.queue_retry(7,'job'))
        self.assertTrue(self.app.jobs.empty())

    async def test_network_retry_schedule_is_persistent_and_bounded(self):
        for delay in (60,120,240):
            self.assertEqual(self.app.schedule_retry('job',ConnectionError()),delay)
        self.assertIsNone(self.app.schedule_retry('job',ConnectionError()))
        row=self.app.store.db.execute('select attempts,next_at from retry_schedule').fetchone()
        self.assertEqual(tuple(row),(4,0))
        self.app.schedule_retry('job',ValueError())
        self.assertEqual(self.app.store.db.execute('select attempts from retry_schedule').fetchone()[0],4)

    async def test_controls_survive_restart_and_panel_reports_them(self):
        control.toggle(self.root,'collector')
        control.toggle(self.root,'transfers')
        text,keys=self.app.panel(7)
        self.assertIn('采集：已暂停',text)
        self.assertIn('新转存任务：已暂停',text)
        self.assertTrue(control.paused(self.root,'collector'))
        self.assertEqual((self.root/'automation.json').stat().st_mode & 0o777,0o600)

    async def test_failure_page_keeps_permanent_errors_visible(self):
        text,keys=self.app.failures_panel(7,1)
        self.assertIn('ConnectionError',text)
        self.assertEqual(keys.inline_keyboard[0][0].callback_data,'bridge_admin:retry:job')
        self.app.store.begin_local_send('job',(1,2),'token')
        text,keys=self.app.failures_panel(7,1)
        self.assertIn('/resolve job retry',text)
        self.assertFalse(any('retry:' in b.callback_data for row in keys.inline_keyboard for b in row))

    async def test_page_jump_does_not_capture_regular_numeric_search(self):
        self.app.pending_jumps[(7,7)]=('bridge_catalog',42,9999999999)
        self.assertFalse(await self.app.handle_jump_reply(self.update,NS(),'1051'))
        self.update.message.reply_to_message=NS(message_id=42)
        self.app.jump_to=AsyncMock(return_value=True)
        self.assertTrue(await self.app.handle_jump_reply(self.update,NS(),'2'))
        self.app.jump_to.assert_awaited_once_with(self.update,unittest.mock.ANY,'bridge_catalog',2)
        self.assertNotIn((7,7),self.app.pending_jumps)

    async def test_search_jump_is_bound_to_search_owner(self):
        self.app.search_sessions['token']=(8,'query')
        with self.assertRaises(ValueError):self.app.jump_rows('bridge_search:token',7)

    async def test_out_of_range_jump_is_explained_not_silently_clamped(self):
        bot=NS(send_message=AsyncMock())
        self.assertFalse(await self.app.jump_to(self.update,NS(bot=bot),'bridge_catalog',99))
        bot.send_message.assert_not_awaited()
        self.assertIn('1–1',self.update.message.reply_text.call_args.args[0])

    async def test_bookmark_saved_after_success_and_private(self):
        items=[dict(kind='document',file_id=str(i),source_chat=1,source_message=i) for i in range(12)]
        progress=self.app.store.start_view(7,'job')
        bot=NS(send_message=AsyncMock(),send_media_group=AsyncMock())
        await send_file_page(bot,7,dict(id='job'),items,1,AsyncMock(),progress=progress)
        saved=self.app.store.bookmark(7,'job')
        self.assertEqual(saved['page'],1)
        self.assertIsNone(self.app.store.bookmark(8,'job'))
        await send_folder_card(bot,7,dict(id='job'),items,bookmark=saved)
        button=bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[1][0]
        self.assertEqual(button.callback_data,f'bridge_page:{progress.id}:2')
        bot.send_media_group.side_effect=RuntimeError('send failed')
        with self.assertRaises(RuntimeError):
            await send_file_page(bot,7,dict(id='job'),items,2,AsyncMock(),progress=progress)
        self.assertEqual(self.app.store.bookmark(7,'job')['page'],1)


class BackupTests(unittest.TestCase):
    def test_live_database_snapshot_excludes_media_and_retains_only_own_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);project=root/'project';project.mkdir();data=project/'data';data.mkdir()
            live=Store(data/'index.sqlite3');live.create('job',7,'link')
            (project/'.env').write_text('DATA_DIR=data\nBOT_TOKEN=test-secret\n')
            (data/'spool').mkdir();(data/'spool'/'media.bin').write_bytes(b'not for backup')
            dest=root/'backups'
            first=backup(project,dest,keep=1)
            self.assertEqual((first/'bridge.env').stat().st_mode&0o777,0o600)
            with sqlite3.connect(first/'index.sqlite3') as db:
                self.assertEqual(db.execute('select id from jobs').fetchone()[0],'job')
            self.assertFalse((first/'spool').exists())
            manual=dest/'manual';manual.mkdir()
            second=backup(project,dest,keep=1)
            self.assertFalse(first.exists());self.assertTrue(second.exists());self.assertTrue(manual.exists())
            self.assertEqual(live.get('job')['state'],'queued')
            live.db.close()
