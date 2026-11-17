import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop
from core import Store
from menu_actions import menu_guard,retire_menu
from operations import Operations
import automation_control as control


class Runtime:
    @menu_guard
    async def click(self,update,context):
        await self.effect()
        await retire_menu(self,context.bot,update.effective_chat.id,update.callback_query.message)
        raise ApplicationHandlerStop


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.runtime=Runtime();self.runtime.store=Store(self.root/'index.sqlite3')
        self.runtime.effect=AsyncMock()
        self.query=NS(message=NS(message_id=42),answer=AsyncMock())
        self.update=NS(callback_query=self.query,effective_chat=NS(id=7,type='private'),effective_user=NS(id=7))
        self.bot=NS(edit_message_reply_markup=AsyncMock(),delete_message=AsyncMock())
        self.context=NS(bot=self.bot)

    async def asyncTearDown(self):
        self.runtime.store.db.close();self.tmp.cleanup()

    async def invoke(self):
        with self.assertRaises(ApplicationHandlerStop):await self.runtime.click(self.update,self.context)

    async def test_consumed_menu_survives_restart_and_keeps_message_content(self):
        await self.invoke()
        self.bot.edit_message_reply_markup.assert_awaited_once_with(chat_id=7,message_id=42,reply_markup=None)
        self.bot.delete_message.assert_not_awaited()
        self.runtime.store.db.close();self.runtime.store=Store(self.root/'index.sqlite3')
        await self.invoke()
        self.runtime.effect.assert_awaited_once()
        self.assertIn('已使用',self.query.answer.call_args.args[0])
        self.update.effective_chat.id=8
        await self.invoke()
        self.assertEqual(self.runtime.effect.await_count,2)

    async def test_cleanup_failure_cannot_repeat_the_operation(self):
        self.bot.edit_message_reply_markup.side_effect=BadRequest('cannot edit old message')
        await self.invoke();await self.invoke()
        self.runtime.effect.assert_awaited_once()

    async def test_failed_operation_keeps_menu_available(self):
        self.runtime.effect.side_effect=RuntimeError('send failed')
        with self.assertRaises(RuntimeError):await self.runtime.click(self.update,self.context)
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.runtime.effect.side_effect=None
        await self.invoke()
        self.assertEqual(self.runtime.effect.await_count,2)

    async def test_concurrent_clicks_do_not_duplicate_side_effects(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def effect():entered.set();await release.wait()
        self.runtime.effect.side_effect=effect
        first=asyncio.create_task(self.invoke())
        await entered.wait()
        try:
            await self.invoke()
            self.runtime.effect.assert_awaited_once()
        finally:
            release.set();await first

    async def test_retiring_menu_also_removes_its_pending_jump_prompt(self):
        self.runtime.pending_jumps={(7,7):('bridge_catalog',80,9999999999,self.query.message)}
        await self.invoke()
        self.assertEqual(self.runtime.pending_jumps,{})
        self.bot.delete_message.assert_awaited_once_with(chat_id=7,message_id=80)

    async def test_stale_admin_pause_does_not_toggle_back_to_running(self):
        runtime=Operations();runtime.store=self.runtime.store;runtime.settings_file=self.root/'performance.json'
        runtime.owners={7};runtime.panel=lambda uid:('panel',None)
        self.query.edit_message_text=AsyncMock();self.query.data='bridge_admin:collector:pause'
        for _ in range(2):
            with self.assertRaises(ApplicationHandlerStop):await runtime.admin_callback(self.update,self.context)
        self.assertTrue(control.paused(self.root,'collector'))
        self.query.data='bridge_admin:collector:resume'
        with self.assertRaises(ApplicationHandlerStop):await runtime.admin_callback(self.update,self.context)
        self.assertFalse(control.paused(self.root,'collector'))
