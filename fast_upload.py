"""Bounded concurrent parts of one large file; small files use Telethon."""
import asyncio
import secrets
import time
from pathlib import Path
from telethon import functions, types
from transfer_metrics import TransferMeter
from transport_recovery import rate_wait
from upload_transport import UploadTimeoutError


async def upload_file(client, path, progress, workers=8, worker_limit=None, stats=None,
                      transport=None):
    path = Path(path)
    size = path.stat().st_size
    if not 1 <= workers <= 32:
        raise ValueError('UPLOAD_WORKERS must be between 1 and 32')
    stats = stats if stats is not None else {}
    stats.update(workers=1, parts=0, wait_seconds=0.0)
    if size <= 10 * 1024**2 or workers == 1:
        meter = TransferMeter(stats, size, transport='sdk')
        def callback(current, total):
            meter.update(current)
            progress(current, total)
        try:
            return await client.upload_file(str(path), part_size_kb=512, progress_callback=callback)
        finally:
            meter.finish()
    meter = TransferMeter(stats, size, transport.count if transport else 1,
                          'dedicated' if transport else 'control')
    stats.update(retries=0, scheduler=getattr(transport, 'scheduler', 'control'))
    chunk_size = 512 * 1024
    parts = (size + chunk_size - 1) // chunk_size
    file_id = secrets.randbits(63)
    completed = 0
    cooldown_until = 0.0
    pending = set()
    acknowledged = set()
    identity = (path.stat().st_size, path.stat().st_mtime_ns)

    async def send(part, data):
        nonlocal completed, cooldown_until
        began = time.monotonic()
        request = functions.upload.SaveBigFilePartRequest(
            file_id=file_id, file_part=part, file_total_parts=parts, bytes=data)
        for attempt in range(4):
            while (delay := cooldown_until - time.monotonic()) > 0:
                await asyncio.sleep(delay)
            try:
                result = await asyncio.wait_for((transport.send if transport else client)(request), 65)
                break
            except asyncio.TimeoutError:
                raise UploadTimeoutError('Upload part deadline exceeded') from None
            except Exception as error:
                wait = rate_wait(error, attempt)
                # A transport-level 429 may have already closed the sender.
                # Only short RPC waits are retried in place with the same file ID.
                if (attempt == 3 or wait is None or wait > 60
                        or type(error).__name__ not in {'FloodWaitError', 'FloodPremiumWaitError'}):
                    raise
                now = time.monotonic()
                until = max(cooldown_until, now + wait)
                stats['cooldown_seconds'] = stats.get('cooldown_seconds', 0.0) + max(
                    0.0, until - max(cooldown_until, now))
                cooldown_until = until
                stats['rate_waits'] = stats.get('rate_waits', 0) + 1
                stats['retries'] += 1
        if not result:
            raise RuntimeError('Telegram did not accept an upload part')
        stats['parts'] += 1
        stats['wait_seconds'] += time.monotonic() - began
        completed += len(data)
        acknowledged.add(part)
        meter.update(completed, time.monotonic()-began)
        progress(completed, size)

    async def drain():
        for task in pending:
            task.cancel()
        # Inspect all results: a simultaneous 429 must never be hidden behind
        # a retryable disconnect from a different connection.
        results = await asyncio.gather(*pending, return_exceptions=True)
        pending.clear()
        for result in results:
            if isinstance(result, BaseException) and rate_wait(result, 0) is not None:
                return result

    try:
        for attempt in range(3):
            try:
                if transport:
                    await transport.prepare()
                with path.open('rb') as stream:
                    if (path.stat().st_size, path.stat().st_mtime_ns) != identity:
                        raise RuntimeError('Upload source changed during transfer')
                    for part in range(parts):
                        if part in acknowledged:
                            continue
                        limit = max(1, min(32, int(worker_limit() if worker_limit else workers)))
                        stats['workers'] = limit
                        while len(pending) >= limit:
                            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                            # Retain done tasks until all results are inspected.
                            # Otherwise a first error could discard another 429.
                            results = await asyncio.gather(*done, return_exceptions=True)
                            pending.difference_update(done)
                            failures = [r for r in results if isinstance(r, BaseException)]
                            if failures:
                                raise next((e for e in failures if rate_wait(e, 0) is not None), failures[0])
                            limit = max(1, min(32, int(worker_limit() if worker_limit else workers)))
                            stats['workers'] = limit
                        stream.seek(part*chunk_size)
                        data = stream.read(chunk_size)
                        if len(data) != min(chunk_size, size-part*chunk_size):
                            raise RuntimeError('Upload source truncated')
                        pending.add(asyncio.create_task(send(part, data)))
                    if pending:
                        while pending:
                            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                            results = await asyncio.gather(*done, return_exceptions=True)
                            pending.difference_update(done)
                            failures = [r for r in results if isinstance(r, BaseException)]
                            if failures:
                                raise next((e for e in failures if rate_wait(e, 0) is not None), failures[0])
                break
            except BaseException as exc:
                rate_error = await drain()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if rate_error is not None:
                    raise rate_error
                if (not transport or not isinstance(exc, ConnectionError)
                        or rate_wait(exc, 0) is not None or attempt == 2):
                    raise
                await transport.close()
                stats['retries'] += 1
                progress(completed, size)
                await asyncio.sleep(2**attempt)
    except BaseException:
        await drain()
        if transport:
            await transport.close()
        raise
    finally:
        meter.finish()
    if len(acknowledged) != parts or completed != size:
        raise RuntimeError('Upload part coverage is incomplete')
    return types.InputFileBig(id=file_id, parts=parts, name=path.name)
