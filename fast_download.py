"""Bounded parallel ranges of one document using Telethon's public iterator."""
import asyncio
import time
from contextlib import asynccontextmanager


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


async def download_file(client, message, stream, progress=None, workers=32, stats=None, worker_limit=None):
    if not 1 <= workers <= 32:
        raise ValueError('DOWNLOAD_WORKERS must be between 1 and 32')
    stats = stats if stats is not None else {}
    size = getattr(getattr(message, 'file', None), 'size', None)
    parallel = workers > 1 and bool(getattr(message, 'document', None)) and size and size > 10 * 1024**2

    async def standard(mode='sdk'):
        stats.update(mode=mode, workers=1, parts=0, wait_seconds=0.0)
        kwargs = dict(file=stream)
        if progress is not None:
            kwargs['progress_callback'] = progress
        await client.download_media(message, **kwargs)

    if not parallel:
        await standard()
        return
    chunk = 512 * 1024
    pool = min(32 if worker_limit else workers, (size+chunk-1)//chunk)
    stride = pool * chunk
    limiter = LiveLimiter(worker_limit or (lambda: workers), pool)
    completed = 0
    stats.update(mode='parallel', workers=limiter.limit(), parts=0, wait_seconds=0.0)

    async def download_range(index):
        nonlocal completed
        offset = index * chunk
        count = (size - offset + stride - 1) // stride
        iterator = client.iter_download(message, offset=offset, stride=stride,
                                        limit=count, chunk_size=chunk,
                                        request_size=chunk, file_size=size)
        started = False
        try:
            for _ in range(count):
                async with limiter.slot():
                    began = time.monotonic()
                    try:
                        started = True
                        data = await iterator.__anext__()
                    except StopAsyncIteration:
                        raise DownloadCoverageError('Download range ended early') from None
                    stats['workers'] = limiter.limit()
                    stats['parts'] += 1
                    stats['wait_seconds'] += time.monotonic() - began
                expected = min(chunk, size-offset)
                if len(data) != expected:
                    raise DownloadCoverageError('Download chunk size does not match')
                # No await between seek/write: each range writes exactly its own
                # offsets, through the same size and free-space guarded stream.
                stream.seek(offset)
                stream.write(data)
                completed += len(data)
                if progress is not None:
                    progress(completed, size)
                offset += stride
        finally:
            if started:
                await iterator.close()

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
        if type(exc).__name__ not in {'_CdnRedirect', 'FileReferenceExpiredError', 'FilerefUpgradeNeededError'}:
            raise
        # Public iter_download lacks download_media's CDN/ref-refresh wrapper.
        # Cancel and close every iterator first, then discard partial ranges and
        # delegate to the original SDK path. Never bypass CDN handling or waits.
        stream.seek(0)
        stream.truncate(0)
        await standard(mode='sdk_fallback')
