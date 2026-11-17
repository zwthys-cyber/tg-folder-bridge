"""Cross-process lease for one relay account's source bot menus."""
import asyncio
import contextlib
import fcntl


@contextlib.asynccontextmanager
async def source_lease(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.25)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
