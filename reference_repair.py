"""Bounded repair of an accessible original, using its existing Telegram media.

Only called after a failed download and its server cooldown. No file content is
rewritten, no protected source is copied, and ambiguous sends are never replayed.
"""
import asyncio
import contextlib
import time
import uuid

from telethon import types
from core import media_key
from local_upload import DeliveryUnknownError
from transport_recovery import error_detail, rate_wait


REJECTED = {'ChatForwardsRestrictedError', 'FileReferenceExpiredError',
            'FileReferenceInvalidError', 'FilerefUpgradeNeededError',
            'MediaEmptyError', 'MediaInvalidError', 'DocumentInvalidError',
            'FileIdInvalidError', 'ChatWriteForbiddenError', 'UserIsBlockedError'}


async def repair_reference(bridge, message, key, job):
    document = getattr(message, 'document', None)
    if (not isinstance(document, types.Document) or not document.file_reference
            or (message.chat_id, message.id) != key
            or getattr(message, 'noforwards', False)
            or getattr(getattr(message, 'chat', None), 'noforwards', False)
            or bridge.pending or bridge.blocked):
        return False
    await bridge.await_cooldown(job)
    marker = f"reference:{job['id']}:{uuid.uuid4().hex}"
    intent = bridge.store.begin_local_send(job['id'], key, marker)
    future = asyncio.get_running_loop().create_future()
    bridge.pending = dict(source=key, future=future, marker=marker,
        caption=message.raw_text or '', media_key=media_key(message),
        intent=intent, expected_size=document.size)
    started = time.monotonic()
    failure = ''
    success = False
    job['phase'] = '云端补收'
    bridge.store.update(job['id'], 'running', job['expected'],
        '下载受限，正在尝试复用来源原文件；机器人确认收件后才计入文件夹。')
    try:
        await asyncio.wait_for(bridge.client.send_file(bridge.destination,
            types.InputDocument(document.id, document.access_hash, document.file_reference),
            caption=marker, parse_mode=None), timeout=30)
        await asyncio.wait_for(future, timeout=60)
        success = True
        return True
    except BaseException as error:
        failure = error_detail(error, '云端补收')
        # An authenticated receipt may have committed just before cancellation
        # or a lost send response. It remains authoritative and is not replayed.
        if future.done() and not future.cancelled() and future.exception() is None:
            if isinstance(error, asyncio.CancelledError):
                raise
            success = True
            failure = ''
            return True
        wait = rate_wait(error, 0)
        if type(error).__name__ in REJECTED or wait is not None:
            bridge.store.reject_local_send(intent)
            if wait is not None:
                bridge.cooldown.defer(wait, failure)
            return False
        bridge.blocked = True
        if isinstance(error, asyncio.CancelledError):
            raise
        raise DeliveryUnknownError('云端补收结果未确认，保留发送记录，禁止自动重发。') from None
    finally:
        if not future.done():
            future.cancel()
        else:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                future.exception()
        bridge.pending = None
        job['phase'] = '下载'
        bridge.store.record_transfer(job['id'], '云端补收',
            dict(bytes=document.size if success else 0, total=document.size,
                 seconds=time.monotonic()-started, transport='telegram_reference',
                 connections=1, workers=1), failure)
