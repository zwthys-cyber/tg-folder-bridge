import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from telethon import types
from fast_upload import upload_file


class Upload(unittest.IsolatedAsyncioTestCase):
    async def test_route_trial_compares_real_parts_and_keeps_the_faster_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.bin'
            data = bytes(range(256)) * 45000
            path.write_bytes(data)
            parts = {}

            def accept(request, route):
                self.assertNotIn(request.file_part, parts)
                parts[request.file_part] = (request.file_id, request.bytes, route)
                return True

            async def control(request):
                await asyncio.sleep(0.012)
                return accept(request, 'control')

            async def dedicated(request):
                return accept(request, 'dedicated')

            transport = NS(count=2, prepare=AsyncMock(), close=AsyncMock(), send=dedicated)
            stats = {}
            with patch('fast_upload.ROUTE_MIN_BYTES', 0), patch('fast_upload.ROUTE_SAMPLE_PARTS', 4):
                result = await upload_file(control, path, Mock(), workers=4, stats=stats,
                                           transport=transport, route_probe=True)
            self.assertEqual({part[0] for part in parts.values()}, {result.id})
            self.assertEqual(b''.join(parts[n][1] for n in range(result.parts)), data)
            self.assertEqual([parts[n][2] for n in range(4)], ['control'] * 4)
            self.assertTrue(all(parts[n][2] == 'dedicated' for n in range(4, result.parts)))
            self.assertEqual(stats['route_choice'], 'dedicated')

    async def test_failed_route_trial_preserves_confirmed_parts_on_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.bin'
            data = b'abcd' * 3 * 1024**2
            path.write_bytes(data)
            parts = {}

            async def control(request):
                self.assertNotIn(request.file_part, parts)
                parts[request.file_part] = (request.file_id, request.bytes)
                return True

            async def dedicated(request):
                if request.file_part == 5:
                    raise ConnectionError('dedicated uplink unavailable')
                return await control(request)

            transport = NS(count=2, prepare=AsyncMock(), close=AsyncMock(), send=dedicated)
            stats = {}
            with patch('fast_upload.ROUTE_MIN_BYTES', 0), patch('fast_upload.ROUTE_SAMPLE_PARTS', 4):
                result = await upload_file(control, path, Mock(), workers=4, stats=stats,
                                           transport=transport, route_probe=True)
            self.assertEqual({part[0] for part in parts.values()}, {result.id})
            self.assertEqual(b''.join(parts[n][1] for n in range(result.parts)), data)
            self.assertEqual(stats['route_choice'], 'control_connection_fallback')
            transport.close.assert_awaited_once()

    async def test_small_files_use_larger_telethon_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'small.jpg'
            path.write_bytes(b'small')
            client = NS(upload_file=AsyncMock(return_value='handle'))
            callback = Mock()
            self.assertEqual(await upload_file(client, path, callback), 'handle')
            client.upload_file.assert_awaited_once()
            self.assertEqual(client.upload_file.call_args.args, (str(path),))
            self.assertEqual(client.upload_file.call_args.kwargs['part_size_kb'], 512)
            client.upload_file.call_args.kwargs['progress_callback'](5, 5)
            callback.assert_called_once_with(5, 5)

    async def test_large_upload_is_bounded_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.mp4'
            data = b'a' * (10 * 1024**2 + 123)
            path.write_bytes(data)
            active, peak = 0, 0
            parts = {}

            async def send(request):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0)
                    parts[request.file_part] = request.bytes
                    return True
                finally:
                    active -= 1

            progress = Mock()
            result = await upload_file(send, path, progress, workers=3)
            self.assertIsInstance(result, types.InputFileBig)
            self.assertGreater(peak, 1)
            self.assertLessEqual(peak, 3)
            self.assertEqual(b''.join(parts[x] for x in sorted(parts)), data)
            self.assertEqual(result.parts, len(parts))
            progress.assert_called_with(len(data), len(data))

    async def test_error_cancels_remaining_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.bin'
            path.write_bytes(b'a' * (11 * 1024**2))
            active = 0

            async def send(request):
                nonlocal active
                active += 1
                try:
                    if request.file_part == 0:
                        await asyncio.sleep(0)
                        raise RuntimeError('rejected')
                    await asyncio.Future()
                finally:
                    active -= 1

            with self.assertRaises(RuntimeError):
                await upload_file(send, path, Mock(), workers=3)
            self.assertEqual(active, 0)

    async def test_dedicated_transport_preserves_every_part_and_records_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'large.bin'
            data = b'a'*(11*1024**2+123)
            path.write_bytes(data)
            parts = {}
            async def send(request):
                await asyncio.sleep(0)
                parts[request.file_part] = request.bytes
                return True
            transport = NS(count=2, prepare=AsyncMock(), send=send)
            client = AsyncMock(side_effect=AssertionError('control connection used'))
            stats = {}
            result = await upload_file(client, path, Mock(), workers=8,
                                       stats=stats, transport=transport)
            self.assertEqual(b''.join(parts[n] for n in sorted(parts)), data)
            self.assertEqual(result.parts, len(parts))
            self.assertEqual(stats['bytes'], len(data))
            self.assertEqual(stats['transport'], 'dedicated')
            self.assertEqual(stats['connections'], 2)
            self.assertGreater(stats['average_mib'], 0)
            client.assert_not_awaited()

    async def test_live_increase_changes_existing_upload_without_restarting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.bin'
            path.write_bytes(b'a'*(17*1024**2+123))
            limit = [2]
            gate, started = asyncio.Event(), asyncio.Event()
            active, peak = 0, 0

            async def send(request):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
                try:
                    await gate.wait()
                    await asyncio.sleep(0.002)
                    return True
                finally:
                    active -= 1

            stats = {}
            task = asyncio.create_task(upload_file(send, path, Mock(), workers=2,
                                                    worker_limit=lambda: limit[0], stats=stats))
            await started.wait()
            limit[0] = 32
            gate.set()
            await task
            self.assertGreater(peak, 8)
            self.assertLessEqual(peak, 32)
            self.assertEqual(stats['workers'], 32)

    async def test_reconnect_keeps_file_id_and_confirmed_parts_without_rereading_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'large.bin'
            data = b'a'*(11*1024**2+123)
            path.write_bytes(data)
            accepted, calls, ids = {}, {}, set()
            async def send(request):
                n = request.file_part
                calls[n] = calls.get(n, 0)+1
                ids.add(request.file_id)
                if n == 3 and calls[n] == 1:
                    raise ConnectionError('temporary failure')
                self.assertNotIn(n, accepted)
                accepted[n] = request.bytes
                return True
            transport = NS(count=2, prepare=AsyncMock(), close=AsyncMock(), send=send)
            stats, progress = {}, Mock()
            with patch('fast_upload.asyncio.sleep', new=AsyncMock()):
                result = await upload_file(None, path, progress, workers=8,
                                           transport=transport, stats=stats)
            self.assertEqual(len(ids), 1)
            self.assertEqual(result.id, next(iter(ids)))
            self.assertEqual(b''.join(accepted[n] for n in sorted(accepted)), data)
            self.assertEqual(calls[3], 2)
            self.assertTrue(all(n == 3 or count == 1 for n, count in calls.items()))
            self.assertEqual(stats['bytes'], len(data))
            self.assertEqual(stats['retries'], 1)
            self.assertEqual(transport.prepare.await_count, 2)
            values = [call.args[0] for call in progress.call_args_list]
            self.assertEqual(values, sorted(values))

    async def test_short_flood_wait_keeps_same_upload_and_confirmed_parts(self):
        from telethon.errors import FloodWaitError
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.bin'
            data = bytes(range(256)) * 45000
            path.write_bytes(data)
            parts, ids = {}, set()
            failed = False

            async def send(request):
                nonlocal failed
                ids.add(request.file_id)
                if request.file_part == 4 and not failed:
                    failed = True
                    raise FloodWaitError(request=None, capture=1)
                self.assertNotIn(request.file_part, parts)
                parts[request.file_part] = request.bytes
                return True

            stats = {}
            result = await upload_file(send, path, Mock(), workers=3, stats=stats)
            self.assertEqual(ids, {result.id})
            self.assertEqual(b''.join(parts[n] for n in sorted(parts)), data)
            self.assertEqual(stats['rate_waits'], 1)
            self.assertEqual(stats['retries'], 1)

    async def test_no_local_retry_on_429_or_long_flood_wait(self):
        from telethon.errors import InvalidBufferError, FloodWaitError
        for error in (InvalidBufferError((-429).to_bytes(4, 'little', signed=True)),
                      FloodWaitError(request=None, capture=120)):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'large.bin'; path.write_bytes(b'a'*(11*1024**2))
                transport = NS(count=2, prepare=AsyncMock(), close=AsyncMock(),
                               send=AsyncMock(side_effect=error))
                with self.assertRaises(type(error)):
                    await upload_file(None, path, Mock(), transport=transport)
                self.assertEqual(transport.prepare.await_count, 1)
                transport.close.assert_awaited()

    async def test_simultaneous_429_takes_priority_over_retryable_disconnect(self):
        from telethon.errors import InvalidBufferError
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'large.bin'; path.write_bytes(b'a'*(11*1024**2))
            async def send(request):
                if request.file_part == 0:
                    raise ConnectionError()
                if request.file_part == 1:
                    raise InvalidBufferError((-429).to_bytes(4, 'little', signed=True))
                return True
            transport = NS(count=2, prepare=AsyncMock(), close=AsyncMock(), send=send)
            with self.assertRaises(InvalidBufferError):
                await upload_file(None, path, Mock(), transport=transport)
            self.assertEqual(transport.prepare.await_count, 1)

    async def test_outage_retries_are_bounded_and_source_changes_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'large.bin'; path.write_bytes(b'a'*(11*1024**2))
            transport = NS(count=2, prepare=AsyncMock(), close=AsyncMock(),
                           send=AsyncMock(side_effect=ConnectionError()))
            with patch('fast_upload.asyncio.sleep', new=AsyncMock()):
                with self.assertRaises(ConnectionError):
                    await upload_file(None, path, Mock(), transport=transport)
            self.assertEqual(transport.prepare.await_count, 3)
            async def change(request):
                import os
                st = path.stat()
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns+1000000))
                raise ConnectionError()
            transport.prepare.reset_mock()
            transport.send = change
            with patch('fast_upload.asyncio.sleep', new=AsyncMock()):
                with self.assertRaisesRegex(RuntimeError, 'source changed'):
                    await upload_file(None, path, Mock(), transport=transport)
            self.assertEqual(transport.prepare.await_count, 2)
