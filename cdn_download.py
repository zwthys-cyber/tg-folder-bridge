"""Single-lane CDN-capable recovery with trusted SHA-256 checks before writes."""
import asyncio
import contextlib
import hashlib
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from telethon import TelegramClient, functions, types
from telethon.crypto import rsa
from telethon.sessions import MemorySession
from transfer_metrics import TransferMeter


class CdnIntegrityError(RuntimeError):
    pass


async def open_cdn(client, redirect):
    # _get_dc loads CDN RSA keys from help.getCdnConfig on the trusted main
    # connection. Use a fresh unauthenticated key, never the user's auth key.
    dc = await client._get_dc(redirect.dc_id, cdn=True)
    keys = [item.public_key for item in client._cdn_config.public_keys
            if item.dc_id == redirect.dc_id]
    if not keys:
        raise CdnIntegrityError('No trusted RSA key for the CDN data center')
    for key in keys:
        rsa.add_key(key, old=False)
    session = MemorySession()
    session.set_dc(dc.id, dc.ip_address, dc.port)
    cdn = TelegramClient(session, client.api_id, client.api_hash,
        connection=client._connection, proxy=client._proxy, receive_updates=False,
        flood_sleep_threshold=0, request_retries=0, connection_retries=0,
        device_model='media-transfer', system_version='1', app_version='1')
    try:
        await asyncio.wait_for(cdn._sender.connect(cdn._connection(
            dc.ip_address, dc.port, dc.id, loggers=cdn._log, proxy=cdn._proxy)), 30)
        return cdn
    except BaseException:
        await cdn.disconnect()
        raise


def decrypt(key, iv, offset, data):
    if len(key) != 32 or len(iv) != 16 or offset % 16:
        raise CdnIntegrityError('Invalid CDN encryption parameters')
    counter = iv[:12] + (offset//16).to_bytes(4, 'big')
    decoder = Cipher(algorithms.AES(key), modes.CTR(counter)).decryptor()
    return decoder.update(data) + decoder.finalize()


async def download_cdn(client, message, stream, *, stats, progress=None, checkpoint=None,
                       cdn_factory=open_cdn):
    document = getattr(message, 'document', None)
    if not isinstance(document, types.Document) or not document.size:
        raise ValueError('CDN recovery requires a known original document')
    size = document.size
    if checkpoint is not None:
        checkpoint.reset()
    stream.seek(0)
    stream.truncate(0)
    stats.update(workers=1, parts=0, wait_seconds=0.0, resumed_bytes=0)
    meter = TransferMeter(stats, size, transport='mtproto_cdn_capable')
    location = types.InputDocumentFileLocation(document.id, document.access_hash,
                                               document.file_reference, '')
    sender = None
    cdn = None
    async def origin(request):
        if sender is None:
            return await asyncio.wait_for(client(request), 30)
        return await asyncio.wait_for(client._call(sender, request, flood_sleep_threshold=0), 30)
    def write(offset, data, began):
        stream.seek(offset)
        stream.write(data)
        stats['parts'] += 1
        stats['wait_seconds'] += time.monotonic()-began
        meter.update(offset+len(data), time.monotonic()-began)
        if progress:
            progress(offset+len(data), size)
    try:
        if document.dc_id != client.session.dc_id:
            sender = await asyncio.wait_for(client._borrow_exported_sender(document.dc_id), 30)
        offset = 0
        redirect = None
        while offset < size:
            began = time.monotonic()
            result = await origin(functions.upload.GetFileRequest(location,
                offset, 1024**2, cdn_supported=True))
            if isinstance(result, types.upload.FileCdnRedirect):
                redirect = result
                break
            if not isinstance(result, types.upload.File) or len(result.bytes) != min(1024**2, size-offset):
                raise CdnIntegrityError('Origin download did not cover the expected range')
            write(offset, result.bytes, began)
            offset += len(result.bytes)
        if redirect is not None:
            cdn = await cdn_factory(client, redirect)
            stats['transport'] = 'telegram_cdn_verified'
            hashes = {h.offset: h for h in redirect.file_hashes}
            while offset < size:
                began = time.monotonic()
                reuploads = 0
                while True:
                    result = await asyncio.wait_for(cdn._call(cdn._sender,
                        functions.upload.GetCdnFileRequest(redirect.file_token, offset, 1024**2),
                        flood_sleep_threshold=0), 30)
                    if not isinstance(result, types.upload.CdnFileReuploadNeeded):
                        break
                    reuploads += 1
                    if reuploads > 2:
                        raise CdnIntegrityError('CDN repeatedly requested reupload')
                    for trusted in await origin(functions.upload.ReuploadCdnFileRequest(
                            redirect.file_token, result.request_token)):
                        hashes[trusted.offset] = trusted
                required = min(1024**2, size-offset)
                if not isinstance(result, types.upload.CdnFile) or len(result.bytes) != required:
                    raise CdnIntegrityError('CDN download did not cover the expected range')
                block = decrypt(redirect.encryption_key, redirect.encryption_iv, offset, result.bytes)
                checked = 0
                while checked < len(block):
                    position = offset + checked
                    if position not in hashes:
                        for trusted in await origin(functions.upload.GetCdnFileHashesRequest(
                                redirect.file_token, position)):
                            hashes[trusted.offset] = trusted
                    item = hashes.get(position)
                    if (item is None or item.offset != position or not 0 < item.limit <= 1024**2
                            or len(item.hash) != 32):
                        raise CdnIntegrityError('Missing or invalid trusted CDN hash')
                    length = min(item.limit, size-position)
                    if length > len(block)-checked:
                        raise CdnIntegrityError('CDN hash crosses an unsupported block boundary')
                    if hashlib.sha256(block[checked:checked+length]).digest() != item.hash:
                        raise CdnIntegrityError('CDN hash verification failed')
                    checked += length
                write(offset, block, began)
                offset += len(block)
        stream.flush()
    finally:
        meter.finish()
        if cdn is not None:
            with contextlib.suppress(Exception):
                await cdn.disconnect()
        if sender is not None:
            await client._return_exported_sender(sender)
