"""Bounded concurrent parts of one large file; small files use Telethon."""
import asyncio
import secrets
import time
from pathlib import Path
from telethon import functions, types
from transfer_metrics import TransferMeter
from transport_recovery import rate_wait
from upload_transport import UploadTimeoutError


ROUTE_SAMPLE_PARTS = 128
ROUTE_MIN_BYTES = 256 * 1024**2


async def upload_file(client, path, progress, workers=8, worker_limit=None, stats=None,
                      transport=None, route_probe=False):
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
    comparing = bool(route_probe and transport and size >= ROUTE_MIN_BYTES)
    active_transport = None if route_probe else transport
    meter = TransferMeter(stats, size, active_transport.count if active_transport else 1,
                          'dedicated' if active_transport else 'control')
    stats.update(retries=0, scheduler=getattr(active_transport, 'scheduler', 'control'))
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
                result = await asyncio.wait_for((active_transport.send if active_transport else client)(request), 65)
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

    def choose(selected):
        nonlocal active_transport
        active_transport = selected
        stats.update(transport='dedicated' if selected else 'control',
                     connections=selected.count if selected else 1,
                     scheduler=getattr(selected, 'scheduler', 'control'))

    def limit(fixed=None):
        ceiling = max(1, min(32, int(worker_limit() if worker_limit else workers)))
        return min(ceiling, fixed) if fixed else ceiling

    async def inspect_finished():
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        results = await asyncio.gather(*done, return_exceptions=True)
        pending.difference_update(done)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise next((error for error in failures if rate_wait(error, 0) is not None), failures[0])

    async def transmit(first, stop, fixed=None):
        with path.open('rb') as stream:
            if (path.stat().st_size, path.stat().st_mtime_ns) != identity:
                raise RuntimeError('Upload source changed during transfer')
            for part in range(first, stop):
                if part in acknowledged:
                    continue
                while len(pending) >= limit(fixed):
                    await inspect_finished()
                stats['workers'] = limit(fixed)
                stream.seek(part * chunk_size)
                data = stream.read(chunk_size)
                if len(data) != min(chunk_size, size - part * chunk_size):
                    raise RuntimeError('Upload source truncated')
                pending.add(asyncio.create_task(send(part, data)))
            while pending:
                await inspect_finished()

    async def compare_routes():
        sample = min(ROUTE_SAMPLE_PARTS, parts // 3)
        fixed = min(4, limit())
        stats['route_probing'] = True
        try:
            began = time.monotonic()
            await transmit(0, sample, fixed)
            control_rate = completed / max(time.monotonic() - began, .001)
            stats['control_mib'] = control_rate / 1024**2
            if stats.get('rate_waits'):
                stats['route_choice'] = 'control_rate_wait'
                return
            try:
                await transport.prepare()
                choose(transport)
                began, previous = time.monotonic(), completed
                await transmit(sample, 2 * sample, fixed)
            except ConnectionError as error:
                rate_error = await drain()
                if rate_error is not None or rate_wait(error, 0) is not None:
                    raise rate_error or error
                await transport.close()
                choose(None)
                stats['route_choice'] = 'control_connection_fallback'
                return
            dedicated_rate = (completed - previous) / max(time.monotonic() - began, .001)
            stats['dedicated_mib'] = dedicated_rate / 1024**2
            if dedicated_rate >= control_rate * 1.15:
                stats['route_choice'] = 'dedicated'
            else:
                await transport.close()
                choose(None)
                stats['route_choice'] = 'control'
        finally:
            stats['route_probing'] = False

    try:
        if comparing:
            await compare_routes()
        for attempt in range(4 if comparing else 3):
            try:
                if active_transport:
                    await active_transport.prepare()
                await transmit(0, parts)
                break
            except BaseException as exc:
                rate_error = await drain()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if rate_error is not None:
                    raise rate_error
                if not active_transport or not isinstance(exc, ConnectionError) or rate_wait(exc, 0) is not None:
                    raise
                if attempt >= 2:
                    if not comparing or attempt == 3:
                        raise
                    await active_transport.close()
                    choose(None)
                    stats['route_choice'] = 'control_connection_fallback'
                else:
                    await active_transport.close()
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
