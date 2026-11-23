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
        self.environment = patch.dict('os.environ', {'BACKUP_DIR': str(self.root/'backups')})
        self.environment.start()
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
        self.environment.stop()
        self.tmp.cleanup()

    async def test_temporary_repair_pause_releases_only_after_pass_settles(self):
        control.write(self.root,dict(collector_paused=True,repair_resume_collector=True,
                                    repair_pass_jobs=['job']))
        self.app.store.db.execute("INSERT INTO retry_schedule VALUES('job',1,1)")
        self.app.store.db.commit()
        self.assertFalse(self.app.finish_repair_pass())
        self.app.store.db.execute("UPDATE retry_schedule SET next_at=0")
        self.app.store.db.commit()
        self.app.store.update('job','queued',3)
        self.assertFalse(self.app.finish_repair_pass())
        self.app.store.update('job','partial',3)
        self.assertTrue(self.app.finish_repair_pass())
        self.assertFalse(control.paused(self.root,'collector'))
        self.assertEqual(self.app.store.get('job')['state'],'partial')
        self.assertFalse(self.app.finish_repair_pass())

    async def test_manual_collection_pause_cancels_automatic_repair_resume(self):
        control.write(self.root,dict(collector_paused=True,repair_resume_collector=True,
                                    repair_pass_jobs=['job']))
        control.set_paused(self.root,'collector',True,manual=True)
        self.assertFalse(self.app.finish_repair_pass())
        self.assertTrue(control.paused(self.root,'collector'))

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

    async def test_completed_share_hides_historical_failure_and_blocks_retry(self):
        self.app.store.db.execute("UPDATE jobs SET link='https://t.me/EPAN?start=Token' WHERE id='job'")
        self.app.store.db.commit()
        self.app.store.create('complete',7,'https://t.me/epan?start=Token')
        self.app.store.update('complete','complete',3)
        self.assertEqual(self.app.failed_rows(7),[])
        self.assertIn('另一任务完成',self.app.queue_retry(7,'job'))
        self.assertTrue(self.app.jobs.empty())
        self.assertEqual(self.app.store.get('job')['state'],'partial')
        self.app.store.begin_local_send('job',(1,2),'token')
        self.assertEqual(len(self.app.failed_rows(7)),1)
        self.assertIn('结果未知',self.app.queue_retry(7,'job'))

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

    async def test_short_delivery_uses_persistent_shared_retry_budget(self):
        job=dict(id='job',expected=3)
        self.app.store.update('job','needs_review',3)
        self.assertEqual(self.app.schedule_incomplete(job),60)
        self.app.store.db.close()
        self.app.store=Store(self.root/'index.sqlite3')
        self.assertEqual(self.app.schedule_incomplete(job),120)
        self.assertEqual(self.app.schedule_retry('job',ConnectionError()),240)
        for delay in (900,3600,21600,86400,86400):
            self.assertEqual(self.app.schedule_incomplete(job),delay)

    async def test_exhausted_transport_flood_recovery_gets_bounded_task_retry(self):
        Flood=type('FloodWaitError',(Exception,),{'seconds':10})
        self.assertEqual(self.app.schedule_retry('job',Flood()),60)
        self.assertEqual(self.app.schedule_retry('job',type('FloodWaitError',(Exception,),{'seconds':1800})()),1800)
        self.app.store.begin_local_send('job',(1,2),'unconfirmed')
        self.assertIsNone(self.app.schedule_retry('job',Flood()))

    async def test_brief_server_wait_after_many_attempts_still_retries_within_an_hour(self):
        self.app.store.db.execute("insert into retry_schedule values('job',6,0)")
        flood = type('FloodWaitError',(Exception,),{'seconds':6})()
        self.assertEqual(self.app.schedule_retry('job', flood), 3600)
        self.app.store.db.execute("update jobs set partial_notice='缺一项' where id='job'")
        self.assertEqual(self.app.schedule_retry('job', flood), 3600)
        long_wait = type('FloodWaitError',(Exception,),{'seconds':90000})()
        self.assertGreaterEqual(self.app.schedule_retry('job', long_wait), 90000)

    async def test_download_flood_is_not_requeued_after_file_recovery(self):
        self.app.active={'phase':'下载'}
        flood=type('FloodWaitError',(Exception,),{'seconds':5})()
        self.assertIsNone(self.app.schedule_retry('job',flood))
        self.app.store.update('job','needs_review',3)
        self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3,download_limited=1)))
        self.assertEqual(self.app.store.db.execute('select count(*) from retry_schedule').fetchone()[0],0)

    async def test_short_delivery_refuses_unsafe_replays(self):
        self.app.store.update('job','needs_review',3)
        for extra in ({'expected':None},{'expected':0},{'source_stalled':True},{'dedup_audit':True},
                      {'audit_unmatched':True},{'overflow':True}):
            self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3)|extra))
        self.app.blocked=True
        self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3)))
        self.app.blocked=False
        self.app.transfer_mode='forward'
        self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3)))
        self.app.transfer_mode='download_upload'
        self.app.store.begin_local_send('job',(1,2),'token')
        self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3)))
        self.assertEqual(self.app.store.db.execute('select count(*) from retry_schedule').fetchone()[0],0)

    async def test_short_delivery_does_not_retry_complete_or_excess_count(self):
        self.app.store.update('job','complete',3)
        self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3)))
        self.app.store.update('job','needs_review',3)
        for count in (3,4):
            with patch.object(self.app.store,'processed_count',return_value=count):
                self.assertIsNone(self.app.schedule_incomplete(dict(id='job',expected=3)))

    async def test_due_short_delivery_queues_once_without_resetting_budget(self):
        self.app.store.update('job','needs_review',3)
        self.app.schedule_incomplete(dict(id='job',expected=3))
        with self.app.store.db:
            self.app.store.db.execute('update retry_schedule set next_at=1')
        self.app.active = {'id':'other'}
        with patch('operations.asyncio.sleep',new=AsyncMock(side_effect=[None,None,asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):await self.app.retry_loop()
        self.assertEqual(self.app.jobs.qsize(),1)
        self.assertEqual(self.app.store.get('job')['state'],'queued')
        self.assertEqual(tuple(self.app.store.db.execute('select attempts,next_at from retry_schedule').fetchone()),(1,0))

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
        self.assertTrue(await self.app.handle_jump_reply(self.update,NS(bot=NS(delete_message=AsyncMock())),'2'))
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

    async def test_browsing_progress_is_not_recorded_or_exposed(self):
        items=[dict(kind='document',file_id=str(i),source_chat=1,source_message=i) for i in range(12)]
        progress=self.app.store.start_view(7,'job')
        bot=NS(send_message=AsyncMock(),send_media_group=AsyncMock())
        await send_file_page(bot,7,dict(id='job'),items,1,AsyncMock(),progress=progress)
        self.assertIsNone(self.app.store.bookmark(7,'job'))
        self.assertIsNone(self.app.store.bookmark(8,'job'))
        await send_folder_card(bot,7,dict(id='job'),items)
        keys=bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertFalse(any('继续浏览' in b.text for row in keys for b in row))
        bot.send_media_group.side_effect=RuntimeError('send failed')
        with self.assertRaises(RuntimeError):
            await send_file_page(bot,7,dict(id='job'),items,2,AsyncMock(),progress=progress)
        self.assertIsNone(self.app.store.bookmark(7,'job'))


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
