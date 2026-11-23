import hashlib
import io
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from telethon import functions, types
from cdn_download import CdnIntegrityError, decrypt, download_cdn, open_cdn


class Cdn(unittest.IsolatedAsyncioTestCase):
    def test_ctr_matches_nist_aes256_vector_at_nonzero_offset(self):
        key = bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4')
        iv = bytes.fromhex('f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff')
        cipher = bytes.fromhex('601ec313775789a5b7a7f504bbf3d228')
        plain = bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')
        self.assertEqual(decrypt(key, iv, 0xfcfdfeff*16, cipher), plain)

    async def fixture(self, *, corrupt=False, missing_hash=False, reupload=False, normal=False):
        data = bytes(range(256))*512+b'short final block!'
        chunk = 128*1024
        key, iv = bytes(range(32)), bytes(range(16))
        hashes = [types.FileHash(offset, min(chunk, len(data)-offset),
            hashlib.sha256(data[offset:offset+chunk]).digest()) for offset in range(0,len(data),chunk)]
        redirect = types.upload.FileCdnRedirect(9,b'token',key,iv,[] if missing_hash else hashes)
        document = types.Document(id=123,access_hash=456,file_reference=b'ref',
            date=datetime.now(timezone.utc),mime_type='video/mp4',size=len(data),dc_id=3,attributes=[])
        origin_calls = []
        async def call(sender, request, **kwargs):
            self.assertEqual(sender, 'file-dc')
            origin_calls.append(request)
            if isinstance(request, functions.upload.GetFileRequest):
                self.assertTrue(request.cdn_supported)
                if normal:
                    return types.upload.File(types.storage.FileUnknown(),0,data)
                return redirect
            self.assertIsInstance(request, (functions.upload.GetCdnFileHashesRequest,
                                           functions.upload.ReuploadCdnFileRequest))
            return hashes
        source = NS(session=NS(dc_id=1),_call=call,
            _borrow_exported_sender=AsyncMock(return_value='file-dc'),
            _return_exported_sender=AsyncMock())
        requested_reupload = False
        async def read(sender, request, **kwargs):
            nonlocal requested_reupload
            self.assertEqual(sender, 'cdn-dc')
            self.assertIsInstance(request, functions.upload.GetCdnFileRequest)
            if reupload and not requested_reupload:
                requested_reupload = True
                return types.upload.CdnFileReuploadNeeded(b'reupload')
            counter = iv[:12]+(request.offset//16).to_bytes(4,'big')
            encoder = Cipher(algorithms.AES(key),modes.CTR(counter)).encryptor()
            encrypted = encoder.update(data[request.offset:request.offset+request.limit])+encoder.finalize()
            if corrupt:
                encrypted = bytes([encrypted[0]^1])+encrypted[1:]
            return types.upload.CdnFile(encrypted)
        cdn = NS(_sender='cdn-dc',_call=read,disconnect=AsyncMock())
        factory = AsyncMock(return_value=cdn)
        return data,NS(document=document),source,cdn,factory,origin_calls

    async def test_verified_download_checks_missing_hashes_on_file_dc_and_preserves_tail(self):
        data,message,source,cdn,factory,calls = await self.fixture(missing_hash=True)
        output,stats = io.BytesIO(),{}
        await download_cdn(source,message,output,stats=stats,cdn_factory=factory)
        self.assertEqual(output.getvalue(),data)
        self.assertEqual(stats['transport'],'telegram_cdn_verified')
        self.assertEqual(stats['bytes'],len(data))
        self.assertTrue(any(isinstance(r,functions.upload.GetCdnFileHashesRequest) for r in calls))
        source._return_exported_sender.assert_awaited_once_with('file-dc')
        cdn.disconnect.assert_awaited_once()

    async def test_corrupt_cdn_block_is_never_written(self):
        _,message,source,cdn,factory,_ = await self.fixture(corrupt=True)
        output = io.BytesIO()
        with self.assertRaises(CdnIntegrityError):
            await download_cdn(source,message,output,stats={},cdn_factory=factory)
        self.assertEqual(output.getvalue(),b'')
        cdn.disconnect.assert_awaited_once()

    async def test_reupload_goes_to_original_file_dc_and_then_verifies_bytes(self):
        data,message,source,cdn,factory,calls = await self.fixture(reupload=True)
        output = io.BytesIO()
        await download_cdn(source,message,output,stats={},cdn_factory=factory)
        self.assertEqual(output.getvalue(),data)
        self.assertEqual(sum(isinstance(r,functions.upload.ReuploadCdnFileRequest) for r in calls),1)

    async def test_capable_request_can_still_return_normal_file_without_cdn_session(self):
        data,message,source,cdn,factory,_ = await self.fixture(normal=True)
        output = io.BytesIO()
        await download_cdn(source,message,output,stats={},cdn_factory=factory)
        self.assertEqual(output.getvalue(),data)
        factory.assert_not_awaited()

    async def test_cdn_connection_uses_fresh_key_and_generic_device_details(self):
        dc = NS(id=9,ip_address='127.0.0.1',port=443)
        origin = NS(_get_dc=AsyncMock(return_value=dc),api_id=123,api_hash='a'*32,
            _cdn_config=NS(public_keys=[NS(dc_id=9,public_key='trusted-key')]),
            _connection=object(),_proxy=None,session=NS(auth_key='must-not-copy'))
        cdn = NS(_sender=NS(connect=AsyncMock()),_connection=lambda *args,**kwargs: object(),
                 _log={},_proxy=None,disconnect=AsyncMock())
        with patch('cdn_download.TelegramClient',return_value=cdn) as create, patch('cdn_download.rsa.add_key') as trust:
            self.assertIs(await open_cdn(origin,NS(dc_id=9)),cdn)
        self.assertIsNone(create.call_args.args[0].auth_key)
        self.assertFalse(create.call_args.kwargs['receive_updates'])
        self.assertEqual(create.call_args.kwargs['device_model'],'media-transfer')
        trust.assert_called_once_with('trusted-key',old=False)

