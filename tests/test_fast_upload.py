import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from telethon import types
from fast_upload import upload_file


class Upload(unittest.IsolatedAsyncioTestCase):
    async def test_small_files_use_larger_telethon_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'small.jpg'
            path.write_bytes(b'small')
            client = NS(upload_file=AsyncMock(return_value='handle'))
            callback = Mock()
            self.assertEqual(await upload_file(client, path, callback), 'handle')
            client.upload_file.assert_awaited_once_with(str(path), part_size_kb=512,
                                                       progress_callback=callback)

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
