import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from telegram.ext import ApplicationHandlerStop
from telethon import types
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

from bridge import Bridge
from core import Store
from local_upload import DeliveryUnknownError
from reference_repair import repair_reference


class Repair(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = Bridge.__new__(Bridge)
        self.bridge.store = Store(Path(self.tmp.name)/'index.sqlite3')
        self.bridge.store.create('job', 7, 'link')
        self.job = dict(id='job', expected=1)
        self.bridge.active = self.job
        self.bridge.pending = None
        self.bridge.blocked = False
        self.bridge.destination = 'own_bot'
        self.bridge.await_cooldown = AsyncMock()
        self.bridge.cooldown = NS(defer=lambda *args: None)
        self.bridge.client = NS(send_file=AsyncMock())
        doc = types.Document(id=123, access_hash=456, file_reference=b'fresh',
            date=datetime.now(timezone.utc), mime_type='video/mp4', size=123456,
            dc_id=2, attributes=[types.DocumentAttributeFilename('IMG_1234.MP4')])
        self.message = NS(chat_id=101, id=9, raw_text='original caption',
                          document=doc, noforwards=False, chat=None)

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    async def receipt(self, *, size=123456, marker=None):
        received = NS(chat_id=7, message_id=88,
            caption=marker or self.bridge.pending['marker'],
            document=NS(file_id='bot-owned-file', file_size=size), forward_origin=None)
        update = NS(effective_chat=NS(type='private'), effective_message=received)
        with self.assertRaises(ApplicationHandlerStop):
            await self.bridge.receive_file(update, None)

    async def test_confirmed_receipt_saves_original_and_closes_intent_atomically(self):
        async def send(*args, **kwargs):
            self.assertIsInstance(args[1], types.InputDocument)
            self.assertEqual(args[1].id, self.message.document.id)
            self.assertEqual(args[1].file_reference, b'fresh')
            self.assertEqual(self.bridge.store.files('job'), [])
            await self.receipt()
        self.bridge.client.send_file.side_effect = send
        self.assertTrue(await repair_reference(self.bridge, self.message, (101,9), self.job))
        saved = self.bridge.store.files('job')
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['media_key'], 'document:123')
        self.assertEqual(saved[0]['caption'], 'original caption')
        self.assertFalse(self.bridge.store.unknown_local_send('job'))
        self.assertIsNone(self.bridge.pending)

    async def test_protected_source_never_creates_intent_or_sends(self):
        self.message.noforwards = True
        self.assertFalse(await repair_reference(self.bridge, self.message, (101,9), self.job))
        self.bridge.client.send_file.assert_not_awaited()
        self.bridge.await_cooldown.assert_not_awaited()
        self.assertFalse(self.bridge.store.unknown_local_send('job'))

    async def test_explicit_rejection_is_not_a_success_and_does_not_block_other_jobs(self):
        self.bridge.client.send_file.side_effect = ChatForwardsRestrictedError(None)
        self.assertFalse(await repair_reference(self.bridge, self.message, (101,9), self.job))
        self.assertEqual(self.bridge.store.files('job'), [])
        self.assertFalse(self.bridge.store.unknown_local_send('job'))
        self.assertFalse(self.bridge.blocked)

    async def test_reference_flood_retains_server_wait_and_does_not_retry_send(self):
        waits = []
        self.bridge.cooldown.defer = lambda seconds, reason: waits.append(seconds)
        self.bridge.client.send_file.side_effect = FloodWaitError(None, capture=42)
        self.assertFalse(await repair_reference(self.bridge, self.message, (101,9), self.job))
        self.assertEqual(waits, [42])
        self.assertEqual(self.bridge.client.send_file.await_count, 1)
        self.assertFalse(self.bridge.store.unknown_local_send('job'))

    async def test_wrong_size_receipt_does_not_enter_index_or_authorize_resend(self):
        async def send(*args, **kwargs):
            await self.receipt(size=3)
        self.bridge.client.send_file.side_effect = send
        with self.assertRaises(DeliveryUnknownError):
            await repair_reference(self.bridge, self.message, (101,9), self.job)
        self.assertEqual(self.bridge.store.files('job'), [])
        self.assertTrue(self.bridge.store.unknown_local_send('job'))
        self.assertTrue(self.bridge.blocked)

    async def test_lost_response_keeps_pending_intent_and_cancellation_propagates(self):
        self.bridge.client.send_file.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await repair_reference(self.bridge, self.message, (101,9), self.job)
        self.assertTrue(self.bridge.store.unknown_local_send('job'))
        self.assertTrue(self.bridge.blocked)

