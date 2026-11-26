"""Catalog publication timing and retry behavior; run only in CI."""
import asyncio
import unittest
from unittest.mock import Mock, patch
from edge_importer import sync


class CatalogSyncTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, versions, times, failures=None):
        position = [0]
        client = Mock()
        if failures:
            client.publish.side_effect = failures

        async def inline(function, *args):
            return function(*args)

        async def sleep(seconds):
            self.assertEqual(seconds, 60)
            position[0] += 1
            if position[0] >= len(versions):
                raise asyncio.CancelledError()

        with patch('edge_importer.snapshot', side_effect=lambda _: {'folders': versions[position[0]]}), \
             patch('edge_importer.fingerprint', side_effect=lambda data: str(data['folders'])), \
             patch('edge_importer.asyncio.to_thread', side_effect=inline), \
             patch('edge_importer.asyncio.sleep', side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await sync(client, 'unused', 1, clock=lambda: times[position[0]])
        return client

    async def test_changed_catalog_is_published_without_hour_wait(self):
        client = await self.exercise([['old'], ['new']], [0, 300])
        self.assertEqual(client.publish.call_count, 2)
        self.assertEqual(client.publish.call_args.args[0]['folders'], ['new'])

    async def test_unchanged_catalog_does_not_write_again(self):
        client = await self.exercise([['same'], ['same'], ['same']], [0, 300, 600])
        self.assertEqual(client.publish.call_count, 1)

    async def test_rapid_changes_are_batched_and_latest_snapshot_wins(self):
        client = await self.exercise([['a'], ['b'], ['c']], [0, 60, 300])
        self.assertEqual(client.publish.call_count, 2)
        self.assertEqual(client.publish.call_args.args[0]['folders'], ['c'])

    async def test_failed_publish_retries_without_marking_snapshot_published(self):
        client = await self.exercise([['a'], ['a']], [0, 60], [RuntimeError('temporary'), None])
        self.assertEqual(client.publish.call_count, 2)
