import unittest
from datetime import datetime,timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from telethon import types,functions,errors
from source_probe import probe_original_tail


class Probe(unittest.IsolatedAsyncioTestCase):
    def fixture(self,dc=5,size=5*1024**2+123):
        doc=types.Document(123,456,b'ref',datetime.now(timezone.utc),
            'video/mp4',size,dc,[])
        response=types.upload.File(types.storage.FileUnknown(),0,b'x'*123)
        client=NS(session=NS(dc_id=5),_sender='home',_call=AsyncMock(return_value=response),
            _borrow_exported_sender=AsyncMock(return_value='file-dc'),
            _return_exported_sender=AsyncMock())
        return client,NS(document=doc),{}

    async def test_reads_only_aligned_last_four_kib_and_keeps_original_identity(self):
        client,message,stats=self.fixture()
        await probe_original_tail(client,message,stats)
        call=client._call.await_args;request=call.args[1]
        self.assertIsInstance(request,functions.upload.GetFileRequest)
        self.assertEqual(request.offset,5*1024**2)
        self.assertEqual(request.limit,4096)
        self.assertTrue(request.precise)
        self.assertFalse(request.cdn_supported)
        self.assertEqual(request.location.id,123)
        self.assertEqual(call.kwargs['flood_sleep_threshold'],0)
        self.assertEqual(stats['bytes'],123)

    async def test_exported_file_dc_is_returned_when_tail_is_unavailable(self):
        client,message,stats=self.fixture(dc=4)
        client._call.side_effect=errors.FloodWaitError(None,capture=7)
        with self.assertRaises(errors.FloodWaitError):
            await probe_original_tail(client,message,stats)
        client._call.assert_awaited_once()
        self.assertEqual(client._call.await_args.args[0],'file-dc')
        client._return_exported_sender.assert_awaited_once_with('file-dc')
        self.assertEqual(stats['bytes'],0)

    async def test_small_original_stays_in_first_block_and_photo_is_not_probed(self):
        client,message,stats=self.fixture(size=123)
        await probe_original_tail(client,message,stats)
        self.assertEqual(client._call.await_args.args[1].offset,0)
        client._call.reset_mock()
        await probe_original_tail(client,NS(document=None),{})
        client._call.assert_not_awaited()
