import asyncio
import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from telethon import TelegramClient, types
from telethon.sessions import MemorySession
from fast_download import DownloadCoverageError, download_file


class Iterator:
    def __init__(self, data, offset, stride, limit, chunk_size, fail=False):
        self.data, self.offset, self.stride = data, offset, stride
        self.left, self.chunk_size = limit, chunk_size
        self.fail = fail
        self.closed = False

    async def __anext__(self):
        if self.fail:
            await asyncio.sleep(0.05)
            raise StopAsyncIteration
        if not self.left:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        data = self.data[self.offset:self.offset+self.chunk_size]
        self.offset += self.stride
        self.left -= 1
        return data

    async def close(self):
        self.closed = True


class Downloads(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_retries_failed_offset_without_discarding_saved_ranges(self):
        from telethon.errors import FloodWaitError
        data = bytes(range(256)) * 45000
        offsets, iterators, failed = [], [], False
        def iterate(message, **kwargs):
            kwargs.pop('request_size'); kwargs.pop('file_size')
            offsets.append(kwargs['offset'])
            iterator = Iterator(data, **kwargs)
            original = iterator.__anext__
            async def read():
                nonlocal failed
                if iterator.offset == 4*1024**2 and not failed:
                    failed = True
                    raise FloodWaitError(request=None, capture=1)
                return await original()
            iterator.__anext__ = read
            iterators.append(iterator)
            return iterator
        stats, output = {}, io.BytesIO()
        await download_file(NS(iter_download=iterate), NS(document=True,
            file=NS(size=len(data))), output, workers=4, stats=stats)
        self.assertEqual(output.getvalue(), data)
        self.assertEqual(stats['retries'], 1)
        self.assertEqual(stats['workers'], 2)
        self.assertIn(4*1024**2, offsets)
        self.assertEqual(offsets.count(0), 1)
        self.assertTrue(all(it.closed for it in iterators))

    async def test_real_sdk_iterators_cover_all_ranges_with_a_short_final_chunk(self):
        data = bytes(range(256)) * 45000 + b'last bytes'
        document = types.Document(id=123, access_hash=456, file_reference=b'ref',
            date=datetime.now(timezone.utc), mime_type='application/octet-stream',
            size=len(data), dc_id=2, attributes=[types.DocumentAttributeFilename('sample.bin')])
        message = types.Message(id=5, peer_id=types.PeerUser(7),
                                media=types.MessageMediaDocument(document=document))
        session = MemorySession()
        session.set_dc(2, '149.154.167.51', 443)
        client = TelegramClient(session, 1234, 'a'*32)
        active, peak = 0, 0

        async def call(sender, request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.001)
                return NS(bytes=data[request.offset:request.offset+request.limit])
            finally:
                active -= 1

        # Actual Telethon iterator and file-location handling; only network RPC
        # is mocked, so this test never connects or uses a Telegram account.
        client._call = AsyncMock(side_effect=call)
        output, stats, progress = io.BytesIO(), {}, Mock()
        try:
            await download_file(client, message, output, progress, workers=4, stats=stats)
            self.assertEqual(output.getvalue(), data)
            self.assertGreater(peak, 1)
            self.assertLessEqual(peak, 4)
            self.assertEqual(stats['workers'], 4)
            self.assertEqual(stats['mode'], 'parallel')
            progress.assert_called_with(len(data), len(data))
        finally:
            await client.disconnect()

    async def test_sparse_file_size_cannot_hide_a_missing_range(self):
        data = b'x' * (11 * 1024**2 + 123)
        iterators = []

        def iterate(message, **kwargs):
            kwargs.pop('request_size')
            kwargs.pop('file_size')
            iterator = Iterator(data, **kwargs, fail=kwargs['offset'] == 512*1024)
            iterators.append(iterator)
            return iterator

        client = NS(iter_download=iterate, download_media=AsyncMock())
        message = NS(document=True, file=NS(size=len(data)))
        output = io.BytesIO()
        with self.assertRaises(DownloadCoverageError):
            await download_file(client, message, output)
        # The tail range has already extended the file to its expected length,
        # but the absent middle range must still cause failure.
        self.assertEqual(len(output.getvalue()), len(data))
        self.assertTrue(all(x.closed for x in iterators))
        client.download_media.assert_not_awaited()

    async def test_cdn_redirect_cancels_ranges_then_delegates_to_sdk(self):
        class _CdnRedirect(Exception):
            pass

        data = b'x' * (11 * 1024**2 + 123)
        iterators = []

        def iterate(message, **kwargs):
            kwargs.pop('request_size')
            kwargs.pop('file_size')
            iterator = Iterator(data, **kwargs)
            if kwargs['offset'] == 0:
                async def redirect():
                    await asyncio.sleep(0)
                    raise _CdnRedirect()
                iterator.__anext__ = redirect
            iterators.append(iterator)
            return iterator

        async def standard(message, file, **kwargs):
            self.assertEqual(file.tell(), 0)
            self.assertEqual(len(file.getvalue()), 0)
            self.assertTrue(all(x.closed for x in iterators))
            file.write(data)

        client = NS(iter_download=iterate, download_media=AsyncMock(side_effect=standard))
        stats, output = {}, io.BytesIO()
        await download_file(client, NS(document=True, file=NS(size=len(data))), output, stats=stats)
        self.assertEqual(output.getvalue(), data)
        client.download_media.assert_awaited_once()
        self.assertEqual(stats['mode'], 'sdk_fallback')
        self.assertEqual(stats['workers'], 1)

    async def test_one_worker_preserves_sdk_path(self):
        message = NS(document=True, file=NS(size=20 * 1024**2))
        client = NS(download_media=AsyncMock(), iter_download=Mock())
        await download_file(client, message, io.BytesIO(), workers=1)
        client.iter_download.assert_not_called()
        client.download_media.assert_awaited_once()

    async def test_cancel_closes_all_iterators(self):
        started = asyncio.Event()
        iterators = []

        def iterate(message, **kwargs):
            class Waiting:
                closed = False

                async def __anext__(self):
                    started.set()
                    await asyncio.Future()

                async def close(self):
                    self.closed = True
            iterator = Waiting()
            iterators.append(iterator)
            return iterator

        client = NS(iter_download=iterate)
        task = asyncio.create_task(download_file(client, NS(document=True,
            file=NS(size=20*1024**2)), io.BytesIO()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(iterators)
        self.assertTrue(all(x.closed for x in iterators))

    async def test_live_limit_changes_in_existing_download_without_losing_ranges(self):
        data = b'a'*(17*1024**2+123)
        limit = [2]
        gate, started = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        class LiveIterator(Iterator):
            async def __anext__(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
                try:
                    await gate.wait()
                    await asyncio.sleep(0.002)
                    return await super().__anext__()
                finally:
                    active -= 1

        def iterate(message, **kwargs):
            kwargs.pop('request_size')
            kwargs.pop('file_size')
            return LiveIterator(data, **kwargs)

        output, stats = io.BytesIO(), {}
        task = asyncio.create_task(download_file(NS(iter_download=iterate),
            NS(document=True, file=NS(size=len(data))), output, workers=2,
            stats=stats, worker_limit=lambda: limit[0]))
        await started.wait()
        limit[0] = 32
        gate.set()
        await task
        self.assertEqual(output.getvalue(), data)
        self.assertGreater(peak, 8)
        self.assertLessEqual(peak, 32)
        self.assertEqual(stats['workers'], 32)
