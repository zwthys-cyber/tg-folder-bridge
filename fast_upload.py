"""Bounded concurrent parts of one large file; small files use Telethon."""
import asyncio
import secrets
import time
from pathlib import Path
from telethon import functions, types


async def upload_file(client, path, progress, workers=8, worker_limit=None, stats=None):
    path = Path(path)
    size = path.stat().st_size
    if not 1 <= workers <= 32:
        raise ValueError('UPLOAD_WORKERS must be between 1 and 32')
    stats = stats if stats is not None else {}
    stats.update(workers=1, parts=0, wait_seconds=0.0)
    if size <= 10 * 1024**2 or workers == 1:
        return await client.upload_file(str(path), part_size_kb=512, progress_callback=progress)
    chunk_size = 512 * 1024
    parts = (size + chunk_size - 1) // chunk_size
    file_id = secrets.randbits(63)
    completed = 0
    pending = set()

    async def send(part, data):
        nonlocal completed
        began = time.monotonic()
        result = await client(functions.upload.SaveBigFilePartRequest(
            file_id=file_id, file_part=part, file_total_parts=parts, bytes=data))
        if not result:
            raise RuntimeError('Telegram did not accept an upload part')
        stats['parts'] += 1
        stats['wait_seconds'] += time.monotonic() - began
        completed += len(data)
        progress(completed, size)

    try:
        with path.open('rb') as stream:
            for part in range(parts):
                limit = max(1, min(32, int(worker_limit() if worker_limit else workers)))
                stats['workers'] = limit
                while len(pending) >= limit:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    await asyncio.gather(*done)
                    limit = max(1, min(32, int(worker_limit() if worker_limit else workers)))
                    stats['workers'] = limit
                data = stream.read(chunk_size)
                if len(data) != min(chunk_size, size - part * chunk_size):
                    raise RuntimeError('Upload source truncated')
                pending.add(asyncio.create_task(send(part, data)))
            if pending:
                await asyncio.gather(*pending)
    except BaseException:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        raise
    return types.InputFileBig(id=file_id, parts=parts, name=path.name)
