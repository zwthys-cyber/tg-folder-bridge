"""Bounded concurrent parts of one large file; small files use Telethon."""
import asyncio
import secrets
from pathlib import Path
from telethon import functions, types


async def upload_file(client, path, progress, workers=3):
    path = Path(path)
    size = path.stat().st_size
    if not 1 <= workers <= 4:
        raise ValueError('UPLOAD_WORKERS must be between 1 and 4')
    if size <= 10 * 1024**2 or workers == 1:
        return await client.upload_file(str(path), part_size_kb=512, progress_callback=progress)
    chunk_size = 512 * 1024
    parts = (size + chunk_size - 1) // chunk_size
    file_id = secrets.randbits(63)
    completed = 0
    pending = set()

    async def send(part, data):
        nonlocal completed
        result = await client(functions.upload.SaveBigFilePartRequest(
            file_id=file_id, file_part=part, file_total_parts=parts, bytes=data))
        if not result:
            raise RuntimeError('Telegram did not accept an upload part')
        completed += len(data)
        progress(completed, size)

    try:
        with path.open('rb') as stream:
            for part in range(parts):
                if len(pending) >= workers:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    await asyncio.gather(*done)
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
