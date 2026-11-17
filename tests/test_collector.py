import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from collector_state import Ledger, canonical, existing_jobs
from core import Store
from source_catalog import Catalog, share_link, list_page
from source_lock import source_lease


class StateTests(unittest.TestCase):
    def test_canonical_case_and_invalid_target(self):
        self.assertEqual(canonical('https://t.me/EPAN?start=s_AbC&extra=1', {'epan'}),
                         'https://t.me/epan?start=s_AbC')
        with self.assertRaises(ValueError):
            canonical('https://t.me/evil?start=s_AbC', {'epan'})

    def test_completed_wins_over_partial_and_owner_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'index.sqlite3'
            store = Store(path)
            for ident, owner, state in [('old', 7, 'complete'), ('new', 7, 'partial'), ('other', 8, 'running')]:
                store.create(ident, owner, 'https://t.me/EPAN?start=Token')
                store.update(ident, state)
            rows, jobs = existing_jobs(path, 7, {'epan'})
            self.assertEqual(len(rows), 2)
            self.assertEqual(jobs['https://t.me/epan?start=Token']['id'], 'old')
            self.assertEqual(store.find_link(7, 'https://t.me/epan?start=Token', {'epan'})['id'], 'old')
            self.assertEqual(store.get('other')['state'], 'running')
            store.db.close()

    def test_ledger_persists_mid_page_and_deduplicates_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'collector.sqlite3'
            ledger = Ledger(path)
            ledger.add('source', '1', 'first', 'link')
            ledger.add('source', '2', 'alias', 'link')
            self.assertTrue(ledger.seen('source', '2'))
            self.assertEqual(ledger.summary(), {'ready': 1})
            self.assertEqual(ledger.page('source'), (1, 0, 0))
            ledger.advance('source', 1, 106)
            ledger.db.close()
            ledger = Ledger(path)
            self.assertTrue(ledger.seen('source', '1'))
            self.assertEqual(ledger.page('source'), (2, 106, 0))
            ledger.db.close()

    def test_unknown_dispatch_is_never_retried_on_old_failed_job(self):
        ledger = Ledger(':memory:')
        ledger.add('source', '1', 'folder', 'link')
        item = ledger.next()
        ledger.set_state(item, 'dispatching', attempt=True)
        ledger.reconcile({})
        self.assertEqual(ledger.summary(), {'dispatching': 1})
        ledger.reconcile({'link': {'id': 'old', 'state': 'partial'}})
        self.assertEqual(ledger.summary(), {'dispatching': 1})
        ledger.reconcile({'link': {'id': 'old', 'state': 'running'}})
        self.assertEqual(ledger.summary(), {'submitted': 1})
        ledger.reconcile({'link': {'id': 'old', 'state': 'complete'}})
        self.assertEqual(ledger.summary(), {'complete': 1})
        ledger.db.close()

    def test_complete_scan_does_not_restart_106_pages(self):
        ledger = Ledger(':memory:')
        ledger.advance('source', 106, 106)
        self.assertEqual(ledger.page('source'), (106, 106, 1))
        ledger.db.close()


class MenuTests(unittest.IsolatedAsyncioTestCase):
    def test_details_parse_and_refuse_unrelated_link(self):
        message = NS(raw_text='📁 文件夹1051\n分享链接：\nhttps://t.me/epan?start=s_AbC', entities=[], buttons=[])
        self.assertEqual(share_link(message, {'epan'}), ('文件夹1051', 'https://t.me/epan?start=s_AbC'))
        message.raw_text = '其他链接 https://t.me/epan?start=s_AbC'
        self.assertIsNone(share_link(message, {'epan'}))

    def test_list_is_not_content_page(self):
        numbers = [[NS(text=str(i), data=f'folder/{i}'.encode()) for i in range(1, 11)]]
        message = NS(raw_text='文件夹列表\n共 1052 个文件夹（第 1/106 页）', buttons=numbers)
        self.assertEqual(list_page(message)[:2], (1, 106))
        message.raw_text = '共 63 个文件（第 1/7 组）'
        self.assertIsNone(list_page(message))

    async def test_shared_lease_excludes_and_cancellation_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'source.lock'
            entered = asyncio.Event()
            async def contender():
                async with source_lease(lock):
                    entered.set()
            async with source_lease(lock):
                task = asyncio.create_task(contender())
                await asyncio.sleep(0.02)
                self.assertFalse(entered.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            async with source_lease(lock):
                pass

    async def test_scan_only_clicks_number_and_back(self):
        ledger = Ledger(':memory:')
        number = NS(text='1', data=b'folder/123')
        listing = NS(id=1, raw_text='文件夹列表\n共 1 个文件夹（第 1/1 页）', buttons=[[number]])
        back = NS(text='文件夹列表', data=b'back_to_folders_list_user/1')
        detail = NS(id=1, raw_text='📁 folder\n分享链接：https://t.me/epan?start=Token', entities=[],
                    buttons=[[NS(text='查看内容', data=b'content/123')], [back]])
        catalog = Catalog(None, 'source', {'epan'})
        catalog.page = AsyncMock(return_value=listing)
        catalog.click = AsyncMock(side_effect=[detail, listing])
        self.assertTrue(await catalog.scan_page(ledger))
        self.assertEqual([c.args[1] for c in catalog.click.await_args_list],
                         [b'folder/123', b'back_to_folders_list_user/1'])
        self.assertEqual(ledger.summary(), {'ready': 1})
        self.assertFalse(await catalog.scan_page(ledger))
        ledger.db.close()

    async def test_source_lease_excludes_and_cancelled_waiter_does_not_unlock_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lease'
            entered = asyncio.Event()
            async def waiter():
                async with source_lease(path):
                    entered.set()
            async with source_lease(path):
                task = asyncio.create_task(waiter())
                await asyncio.sleep(0.02)
                self.assertFalse(entered.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                task = asyncio.create_task(waiter())
                await asyncio.sleep(0.02)
                self.assertFalse(entered.is_set())
            await asyncio.wait_for(task, 1)
            self.assertTrue(entered.is_set())

    async def test_new_head_folder_does_not_reset_main_cursor_and_has_priority(self):
        ledger = Ledger(':memory:')
        ledger.add('source', 'old', '文件夹1039', 'old-link')
        ledger.advance('source', 40, 106)
        self.assertTrue(ledger.head_due('source'))
        ledger.add('source', 'new', '文件夹1052', 'new-link')
        ledger.checked_head('source')
        self.assertFalse(ledger.head_due('source'))
        self.assertEqual(ledger.page('source'), (41, 106, 0))
        self.assertEqual(ledger.next()['link'], 'new-link')
        ledger.db.close()

    async def test_page_navigation_uses_real_page_protocol_not_detail_back(self):
        m = NS(raw_text='共1052个文件夹（第1/106页）',
               buttons=[[NS(text='1', data=b'folder_preview_list/1/1/list'),
                         NS(text='下一页', data=b'folders_list_page/2')]])
        catalog = Catalog(NS(), 'source', {'epan'})
        catalog.find_panel = AsyncMock(return_value=m)
        catalog.click = AsyncMock(return_value=NS())
        await catalog.page(2)
        self.assertEqual(catalog.click.await_args.args[1], b'folders_list_page/2')
        await catalog.page(1)
        self.assertEqual(catalog.click.await_count, 1)

    async def test_resume_from_detail_returns_before_paging(self):
        detail = NS(raw_text='详情', buttons=[[NS(text='文件夹列表', data=b'back_to_folders_list_user/2')]])
        listing = NS(raw_text='共1052个文件夹（第2/106页）',
                     buttons=[[NS(text='1', data=b'folder_preview_list/1/2/list'),
                               NS(text='上一页', data=b'folders_list_page/1')]])
        catalog = Catalog(NS(), 'source', {'epan'})
        catalog.find_panel = AsyncMock(return_value=detail)
        catalog.click = AsyncMock(side_effect=[listing, NS()])
        await catalog.page(1)
        self.assertEqual([c.args[1] for c in catalog.click.await_args_list],
                         [b'back_to_folders_list_user/2', b'folders_list_page/1'])
