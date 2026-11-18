import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from download_pipeline import DownloadPipeline


class Pipeline(unittest.IsolatedAsyncioTestCase):
    async def test_only_next_file_is_prefetched_and_all_paths_are_released(self):
        messages = [NS(chat_id=1, id=i) for i in range(4)]
        opened, closed, maximum = set(), [], 0
        second_started = asyncio.Event()
        @asynccontextmanager
        async def stage(message, stats, progress):
            nonlocal maximum
            opened.add(message.id)
            maximum = max(maximum, len(opened))
            stats['bytes'] = message.id+1
            try:
                if message.id == 1:
                    second_started.set()
                yield message.id
            finally:
                opened.remove(message.id)
                closed.append(message.id)
        pipeline = DownloadPipeline(messages, {(1, i) for i in range(4)}, stage)
        statistics = []
        try:
            async with pipeline.stage(messages[0], statistics.append, None) as path:
                await second_started.wait()
                self.assertEqual(path, 0)
                self.assertEqual(opened, {0, 1})
            async with pipeline.stage(messages[1], statistics.append, None) as path:
                await asyncio.sleep(0)
                self.assertEqual(path, 1)
                self.assertNotIn(3, opened)
        finally:
            await pipeline.close()
        self.assertLessEqual(maximum, 2)
        self.assertEqual(opened, set())
        self.assertEqual(sorted(closed), [0, 1, 2])
        self.assertEqual([s['bytes'] for s in statistics], [1, 2])

    async def test_cancellation_cleans_partial_next_file(self):
        messages = [NS(chat_id=1, id=i) for i in range(2)]
        started = asyncio.Event()
        closed = []
        @asynccontextmanager
        async def stage(message, stats, progress):
            try:
                if message.id == 1:
                    started.set()
                    await asyncio.Future()
                yield message.id
            finally:
                closed.append(message.id)
        pipeline = DownloadPipeline(messages, {(1, 0), (1, 1)}, stage)
        async with pipeline.stage(messages[0], lambda stats: None, None):
            await started.wait()
        await pipeline.close()
        self.assertEqual(sorted(closed), [0, 1])

    async def test_prefetch_failure_surfaces_only_when_that_file_is_consumed(self):
        messages = [NS(chat_id=1, id=i) for i in range(2)]
        @asynccontextmanager
        async def stage(message, stats, progress):
            if message.id == 1:
                raise ConnectionError('offline')
            yield message.id
        pipeline = DownloadPipeline(messages, {(1, 0), (1, 1)}, stage)
        async with pipeline.stage(messages[0], lambda stats: None, None):
            await asyncio.sleep(0)
        with self.assertRaises(ConnectionError):
            async with pipeline.stage(messages[1], lambda stats: None, None):
                self.fail('failed download must never upload')
        await pipeline.close()
