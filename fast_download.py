"""Bounded parallel ranges of one document using Telethon's public iterator."""
import asyncio
import time
import contextlib
from contextlib import asynccontextmanager
from telethon import functions, types
from transfer_metrics import TransferMeter
from transport_recovery import rate_wait


class LiveLimiter:
    def __init__(self, getter, maximum):
        self.getter, self.maximum = getter, maximum
        self.active = 0
        self.changed = asyncio.Condition()

    def limit(self):
        return max(1, min(self.maximum, int(self.getter())))

    @asynccontextmanager
    async def slot(self):
        async with self.changed:
            await self.changed.wait_for(lambda: self.active < self.limit())
            self.active += 1
        try:
            yield
        finally:
            async with self.changed:
                self.active -= 1
                self.changed.notify_all()


class DownloadCoverageError(RuntimeError):
    pass


class WideDownloadUnsupported(RuntimeError):
    pass


async def wide_parts(client, document, offset, stride, count, chunk):
    location = types.InputDocumentFileLocation(document.id, document.access_hash,
                                               document.file_reference, '')
    for index in range(count):
        result = await client(functions.upload.GetFileRequest(
            location, offset + index*stride, chunk, cdn_supported=False))
        if not isinstance(result, types.upload.File):
            raise WideDownloadUnsupported('The response requires the SDK download path')
        yield result.bytes


async def download_file(client, message, stream, progress=None, workers=8, stats=None,
                        worker_limit=None, *, wide=True):
    if not 1 <= workers <= 32:
        raise ValueError('DOWNLOAD_WORKERS must be between 1 and 32')
    stats = stats if stats is not None else {}
    size = getattr(getattr(message, 'file', None), 'size', None)
    parallel = workers > 1 and bool(getattr(message, 'document', None)) and size and size > 10 * 1024**2

    async def standard(mode='sdk'):
        stats.update(mode=mode, workers=1, parts=0, wait_seconds=0.0)
        meter = TransferMeter(stats, size, transport=mode)
        kwargs = dict(file=stream)
        def callback(current, total):
            meter.update(current)
            if progress is not None:
                progress(current, total)
        if progress is not None:
            kwargs['progress_callback'] = callback
        try:
            await client.download_media(message, **kwargs)
        finally:
            meter.finish()

    if not parallel:
        await standard()
        return
    document = getattr(message, 'document', None)
    wide = bool(wide and isinstance(document, types.Document)
                and getattr(getattr(client, 'session', None), 'dc_id', None) == document.dc_id
                and document.file_reference)
    chunk = (1024 if wide else 512) * 1024
    pool = min(32 if worker_limit else workers, (size+chunk-1)//chunk)
    stride = pool * chunk
    configured_limit = worker_limit or (lambda: workers)
    temporary_limit = pool
    limiter = LiveLimiter(lambda: min(configured_limit(), temporary_limit), pool)
    completed = 0
    pause_until = 0.0
    next_increase = 0.0
    clean_parts = 0
    stats.update(mode='parallel', workers=limiter.limit(), parts=0, wait_seconds=0.0)
    transport = 'mtproto_1m' if wide else 'sdk_ranges'
    meter = TransferMeter(stats, size, transport=transport)

    async def download_range(index):
        nonlocal completed, pause_until, temporary_limit, next_increase, clean_parts
        offset = index * chunk
        count = (size - offset + stride - 1) // stride
        def iterator_at(position, remaining):
            if wide:
                return wide_parts(client, document, position, stride, remaining, chunk)
            return client.iter_download(message, offset=position, stride=stride,
                limit=remaining, chunk_size=chunk, request_size=chunk, file_size=size)

        async def close_iterator(iterator):
            await (iterator.aclose() if wide else iterator.close())
        iterator = iterator_at(offset, count)
        started = False
        try:
            for part in range(count):
                retries = 0
                while True:
                    delay = pause_until-time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        async with limiter.slot():
                            began = time.monotonic()
                            # A queued range may have acquired its slot after
                            # another request received a rate limit.
                            if began < pause_until:
                                continue
                            started = True
                            try:
                                data = await iterator.__anext__()
                            except Exception as error:
                                wait = rate_wait(error, retries)
                                if wait is not None:
                                    if began >= pause_until:
                                        temporary_limit = max(1, limiter.limit() // 2)
                                    pause_until = max(pause_until, time.monotonic()+wait)
                                    next_increase = pause_until+8
                                    clean_parts = 0
                                raise
                        break
                    except StopAsyncIteration:
                        raise DownloadCoverageError('Download range ended early') from None
                    except Exception as error:
                        wait = rate_wait(error, retries)
                        if wait is None and isinstance(error, (ConnectionError, asyncio.TimeoutError)):
                            wait = min(2**retries, 15)
                        if wait is None or retries >= 3:
                            raise
                        retries += 1
                        stats['retries'] = stats.get('retries', 0)+1
                        # Every range observes the same cooldown. Already written
                        # ranges stay intact; retry exactly the failed offset.
                        pause_until = max(pause_until, time.monotonic()+wait)
                        with contextlib.suppress(Exception):
                            await close_iterator(iterator)
                        iterator = iterator_at(offset, count-part)
                        started = False
                stats['workers'] = limiter.limit()
                stats['parts'] += 1
                if began >= pause_until and temporary_limit < pool:
                    clean_parts += 1
                    if clean_parts * chunk >= 16*1024**2 and time.monotonic() >= next_increase:
                        temporary_limit += 1
                        clean_parts = 0
                        next_increase = time.monotonic()+8
                stats['wait_seconds'] += time.monotonic() - began
                expected = min(chunk, size-offset)
                if len(data) != expected:
                    raise DownloadCoverageError('Download chunk size does not match')
                # No await between seek/write: each range writes exactly its own
                # offsets, through the same size and free-space guarded stream.
                stream.seek(offset)
                stream.write(data)
                completed += len(data)
                meter.update(completed, time.monotonic()-began)
                if progress is not None:
                    progress(completed, size)
                offset += stride
        finally:
            if started:
                await close_iterator(iterator)

    tasks = [asyncio.create_task(download_range(index)) for index in range(pool)]
    try:
        await asyncio.gather(*tasks)
        if completed != size:
            raise DownloadCoverageError('Download coverage does not match file size')
        stream.flush()
    except BaseException as exc:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if wide and type(exc).__name__ in {'WideDownloadUnsupported', 'FileMigrateError',
                'LimitInvalidError', 'OffsetInvalidError', 'FileReferenceExpiredError',
                'FileReferenceInvalidError', 'FilerefUpgradeNeededError'}:
            stream.seek(0)
            stream.truncate(0)
            return await download_file(client, message, stream, progress, workers,
                                       stats, worker_limit, wide=False)
        if type(exc).__name__ not in {'_CdnRedirect', 'FileReferenceExpiredError', 'FilerefUpgradeNeededError'}:
            raise
        # Public iter_download lacks download_media's CDN/ref-refresh wrapper.
        # Cancel and close every iterator first, then discard partial ranges and
        # delegate to the original SDK path. Never bypass CDN handling or waits.
        stream.seek(0)
        stream.truncate(0)
        await standard(mode='sdk_fallback')
    finally:
        if stats.get('transport') == transport:
            meter.finish()
