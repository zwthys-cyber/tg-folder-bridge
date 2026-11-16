import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from telethon import TelegramClient, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from upload_transport import UploadTransport, UploadTimeoutError


class Sender:
    def __init__(self, auth_key, **kwargs):
        self.auth_key = auth_key
        self.connected = False
        self.requests = []
        self.endpoint = None

    async def connect(self, connection):
        self.connected = True
        self.endpoint = connection

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    def send(self, request, **kwargs):
        self.requests.append(request)
        result = asyncio.get_running_loop().create_future()
        result.set_result(True)
        return result


class Transport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        session = MemorySession()
        session.set_dc(2, '127.0.0.1', 443)
        session.auth_key = AuthKey(b'a'*256)
        self.client = TelegramClient(session, 1234, 'a'*32, flood_sleep_threshold=0)
        self.control = types.DcOption(id=2, ip_address='127.0.0.1', port=443)
        self.media = types.DcOption(id=2, ip_address='127.0.0.2', port=443, media_only=True)
        self.client._get_dc = AsyncMock(return_value=self.control)
        self.client._connection = Mock(side_effect=lambda ip, port, dc, **kw: (ip, port, dc))
        self.config_patch = patch.object(TelegramClient, '_config', NS(dc_options=[self.control, self.media]))
        self.config_patch.start()
        self.sender_patch = patch('upload_transport.MTProtoSender', side_effect=Sender)
        self.factory = self.sender_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.sender_patch.stop()

    async def test_real_sdk_calls_use_distinct_sessions_same_key_and_media_endpoint(self):
        pool = UploadTransport(self.client, 2)
        await pool.prepare()
        before = list(pool.senders)
        await pool.prepare()
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(len({id(s) for s in pool.senders}), 2)
        for sender in pool.senders:
            self.assertIs(sender.auth_key, self.client._sender.auth_key)
            self.assertEqual(sender.endpoint, ('127.0.0.2', 443, 2))
        requests = [functions.upload.SaveBigFilePartRequest(55, n, 10, b'part') for n in range(10)]
        self.assertEqual(await asyncio.gather(*(pool.send(r) for r in requests)), [True]*10)
        for sender in pool.senders:
            self.assertEqual(len(sender.requests), 5)
            self.assertIsInstance(sender.requests[0], functions.InvokeWithLayerRequest)
            for request in sender.requests:
                while hasattr(request, 'query'):
                    request = request.query
                self.assertIsInstance(request, functions.upload.SaveBigFilePartRequest)
        await pool.close()
        self.assertTrue(all(not s.connected for s in before))
        self.assertEqual(pool.senders, [])

    async def test_partial_setup_and_cancellation_close_every_opened_sender(self):
        pool = UploadTransport(self.client, 2)
        senders = []
        def factory(*args, **kwargs):
            sender = Sender(*args, **kwargs)
            if senders:
                sender.connect = AsyncMock(side_effect=ConnectionError())
            senders.append(sender)
            return sender
        self.factory.side_effect = factory
        with self.assertRaises(ConnectionError):
            await pool.prepare()
        self.assertTrue(all(not s.connected for s in senders))
        self.assertEqual(pool.senders, [])

    async def test_control_methods_cannot_leak_to_media_connections(self):
        pool = UploadTransport(self.client, 2)
        with self.assertRaises(TypeError):
            await pool.send(functions.help.GetConfigRequest())

    async def test_rate_limit_is_propagated_without_reconnecting_or_fallback(self):
        from telethon.errors import FloodWaitError
        pool = UploadTransport(self.client, 2)
        await pool.prepare()
        self.client._call = AsyncMock(side_effect=FloodWaitError(request=None, capture=120))
        with self.assertRaises(FloodWaitError):
            await pool.send(functions.upload.SaveBigFilePartRequest(55, 0, 2, b'part'))
        self.assertEqual(self.factory.call_count, 2)
        await pool.close()

    async def test_dc_change_never_sends_with_wrong_dc_authorization(self):
        pool = UploadTransport(self.client, 2)
        await pool.prepare()
        self.client.session.set_dc(3, '127.0.0.3', 443)
        with self.assertRaises(ConnectionError):
            await pool.send(functions.upload.SaveBigFilePartRequest(55, 0, 2, b'part'))
        await pool.close()

    async def test_balancer_avoids_lane_with_more_pending_work(self):
        pool = UploadTransport(self.client, 2, 'balanced')
        await pool.prepare()
        pool.inflight[:] = [5, 0]
        await pool.send(functions.upload.SaveBigFilePartRequest(55, 0, 2, b'part'))
        self.assertEqual(len(pool.senders[0].requests), 0)
        self.assertEqual(len(pool.senders[1].requests), 1)
        self.assertEqual(pool.inflight, [5, 0])
        await pool.close()

    async def test_balancer_releases_lane_reservations_on_rpc_failure(self):
        pool = UploadTransport(self.client, 2)
        await pool.prepare()
        self.client._call = AsyncMock(side_effect=ConnectionError())
        with self.assertRaises(ConnectionError):
            await pool.send(functions.upload.SaveBigFilePartRequest(55, 0, 2, b'part'))
        self.assertEqual(pool.inflight, [0, 0])
        await pool.close()
