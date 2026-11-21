"""Bounded parallel ranges of one document using Telethon's public iterator."""
import asyncio
import time
import contextlib
from contextlib import asynccontextmanager
from functools import partial
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


class RequestPacer:
    """Space request starts after a rate limit, independently of in-flight size."""

    def __init__(self, interval=0.0):
        self.interval = interval
        self.next_request = 0.0
        self.resume_at = 0.0
        self.cooldown_seconds = 0.0

    def delay(self, now):
        return max(0.0, self.resume_at - now, self.next_request - now)

    def elapsed_cooldown(self, now):
        return self.cooldown_seconds - max(0.0, self.resume_at - now)

    def started(self, now):
        self.next_request = now + self.interval

    def pause(self, now, seconds):
        until = max(self.resume_at, now + seconds)
        self.cooldown_seconds += max(0.0, until - max(now, self.resume_at))
        self.resume_at = until

    def limited(self, now, began, seconds, *, pace):
        first = began >= self.resume_at
        if first and pace:
            self.interval = min(0.5, max(0.10, self.interval * 1.20))
        self.pause(now, seconds)
        return first

    def recover(self):
        self.interval = max(0.06, self.interval - 0.015) if self.interval else 0.0


class DownloadPacingHistory:
    """Reuse successful pacing briefly for consecutive files on the same route."""

    def __init__(self):
        self.profiles = {}

    def get(self, route, now):
        profile = self.profiles.get(route)
        if profile is None:
            return None
        saved_at, interval, workers = profile
        if now - saved_at >= 120:
            self.profiles.pop(route, None)
            return None
        return interval, workers

    def remember(self, route, now, interval, workers):
        if interval > 0:
            self.profiles[route] = (now, interval, workers)


async def wide_parts(client, document, offset, stride, count, chunk):
    location = types.InputDocumentFileLocation(document.id, document.access_hash,
                                               document.file_reference, '')
    sender = None
    invoke = client
    try:
        if client.session.dc_id != document.dc_id:
            # Reuse the exported sender managed by our pinned Telethon version.
            # The normal SDK iterator uses the same borrow/return API.
            sender = await client._borrow_exported_sender(document.dc_id)
            invoke = partial(client._call, sender)
        for index in range(count):
            result = await invoke(functions.upload.GetFileRequest(
                location, offset + index*stride, chunk, cdn_supported=False))
            if not isinstance(result, types.upload.File):
                raise WideDownloadUnsupported('The response requires the SDK download path')
            yield result.bytes
    finally:
        if sender is not None:
            await client._return_exported_sender(sender)


async def download_file(client, message, stream, progress=None, workers=8, stats=None,
                        worker_limit=None, *, wide=True, pacing_history=None, checkpoint=None):
    if not 1 <= workers <= 32:
        raise ValueError('DOWNLOAD_WORKERS must be between 1 and 32')
    stats = stats if stats is not None else {}
    size = getattr(getattr(message, 'file', None), 'size', None)

    async def standard(mode='sdk'):
        if checkpoint is not None:
            checkpoint.reset()
            stream.seek(0)
            stream.truncate(0)
            stats.pop('resumed_bytes', None)
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

    document = getattr(message, 'document', None)
    home_dc = getattr(getattr(client, 'session', None), 'dc_id', None)
    wide = bool(wide and home_dc and isinstance(document, types.Document)
                and document.file_reference and document.dc_id
                and (home_dc == document.dc_id or all(callable(getattr(client, name, None))
                     for name in ('_borrow_exported_sender', '_return_exported_sender', '_call'))))
    short = bool(size and size <= 10 * 1024**2)
    resumable = checkpoint is not None and document is not None and bool(size and size >= 512 * 1024)
    if (not document or not size or (workers == 1 and not resumable)
            or (short and (not wide or size < 2 * 1024**2) and not resumable)):
        await standard()
        return
    chunk = (1024 if wide else 512) * 1024
    if checkpoint is not None:
        checkpoint.begin(chunk)
    saved = frozenset(checkpoint.parts) if checkpoint is not None else frozenset()
    resumed = sum(min(chunk, size-offset) for offset in saved)
    stats['resumed_bytes'] = resumed
    pool = min(4 if short else 32, 32 if worker_limit else workers, (size+chunk-1)//chunk)
    stride = pool * chunk
    configured_limit = worker_limit or (lambda: workers)
    route = (home_dc, document.dc_id, short) if wide else None
    now = time.monotonic()
    learned = pacing_history.get(route, now) if wide and pacing_history is not None else None
    interval, temporary_limit = learned if learned is not None else (0.0, pool)
    limiter = LiveLimiter(lambda: min(configured_limit(), temporary_limit), pool)
    completed = resumed
    pacer = RequestPacer(interval)
    next_increase = now + 8 if learned is not None else 0.0
    clean_parts = 0
    stats.update(mode='parallel', workers=limiter.limit(), parts=0, wait_seconds=0.0,
                 rate_waits=0, cooldown_seconds=0.0, pace_ms=round(interval*1000),
                 pacing_reused=learned is not None)
    transport = ('mtproto_1m_short' if short else 'mtproto_1m') if wide else 'sdk_ranges'
    meter = TransferMeter(stats, size, transport=transport)
    if resumed and progress is not None:
        progress(resumed, size)

    async def download_range(index):
        nonlocal completed, temporary_limit, next_increase, clean_parts
        offset = index * chunk
        count = (size - offset + stride - 1) // stride
        def iterator_at(position, remaining):
            if wide:
                return wide_parts(client, document, position, stride, remaining, chunk)
            return client.iter_download(message, offset=position, stride=stride,
                limit=remaining, chunk_size=chunk, request_size=chunk, file_size=size)

        async def close_iterator(iterator):
            await (iterator.aclose() if wide else iterator.close())
        iterator = None
        try:
            for part in range(count):
                if offset in saved:
                    if iterator is not None:
                        await close_iterator(iterator)
                        iterator = None
                    offset += stride
                    continue
                retries = 0
                while True:
                    delay = pacer.delay(time.monotonic())
                    if delay > 0:
                        await asyncio.sleep(delay)
                    try:
                        async with limiter.slot():
                            began = time.monotonic()
                            # A queued range may have acquired its slot after
                            # another request received a rate limit.
                            if pacer.delay(began):
                                continue
                            pacer.started(began)
                            if iterator is None:
                                iterator = iterator_at(offset, count-part)
                            try:
                                data = await iterator.__anext__()
                            except Exception as error:
                                wait = rate_wait(error, retries)
                                if wait is not None:
                                    if pacer.limited(time.monotonic(), began, wait, pace=wide):
                                        # A paced pair can keep a request in flight
                                        # while avoiding two simultaneous starts.
                                        floor = 2 if wide else 1
                                        temporary_limit = max(floor, limiter.limit() // 2)
                                    stats['rate_waits'] += 1
                                    next_increase = pacer.resume_at+8
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
                        if rate_wait(error, retries-1) is None:
                            pacer.pause(time.monotonic(), wait)
                        if iterator is not None:
                            with contextlib.suppress(Exception):
                                await close_iterator(iterator)
                        iterator = None
                stats['workers'] = limiter.limit()
                stats['parts'] += 1
                if began >= pacer.resume_at:
                    clean_parts += 1
                    if clean_parts * chunk >= 16*1024**2 and time.monotonic() >= next_increase:
                        temporary_limit = min(pool, temporary_limit + 1)
                        pacer.recover()
                        clean_parts = 0
                        next_increase = time.monotonic()+8
                stats['pace_ms'] = round(1000 * pacer.interval)
                stats['cooldown_seconds'] = round(pacer.elapsed_cooldown(time.monotonic()), 2)
                stats['wait_seconds'] += time.monotonic() - began
                expected = min(chunk, size-offset)
                if len(data) != expected:
                    raise DownloadCoverageError('Download chunk size does not match')
                # No await between seek/write: each range writes exactly its own
                # offsets, through the same size and free-space guarded stream.
                stream.seek(offset)
                stream.write(data)
                if checkpoint is not None:
                    checkpoint.record(offset, data)
                completed += len(data)
                meter.update(completed-resumed, time.monotonic()-began)
                if progress is not None:
                    progress(completed, size)
                offset += stride
        finally:
            if iterator is not None:
                await close_iterator(iterator)

    tasks = [asyncio.create_task(download_range(index)) for index in range(pool)]
    try:
        await asyncio.gather(*tasks)
        if completed != size:
            raise DownloadCoverageError('Download coverage does not match file size')
        stream.flush()
        if wide and pacing_history is not None:
            pacing_history.remember(route, time.monotonic(), pacer.interval, limiter.limit())
    except BaseException as exc:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if wide and type(exc).__name__ in {'WideDownloadUnsupported', 'FileMigrateError', 'DcIdInvalidError',
                'LimitInvalidError', 'OffsetInvalidError', 'FileReferenceExpiredError',
                'FileReferenceInvalidError', 'FilerefUpgradeNeededError', 'DownloadCoverageError'}:
            stream.seek(0)
            stream.truncate(0)
            if checkpoint is not None:
                checkpoint.reset()
            return await download_file(client, message, stream, progress, workers,
                                       stats, worker_limit, wide=False, checkpoint=checkpoint)
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
            stats['cooldown_seconds'] = round(pacer.elapsed_cooldown(time.monotonic()), 2)
            meter.finish()
