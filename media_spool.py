"""Bounded transfer spool and short-lived checkpoints of complete downloads."""
import asyncio
import fcntl
import hashlib
import io
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fast_download import DownloadPacingHistory, download_file
from download_checkpoint import PartialDownload
from local_upload import DeliveryUnknownError
from transport_recovery import rate_wait


class InsufficientDiskError(RuntimeError):
    pass


class MediaTooLargeError(RuntimeError):
    pass


class IncompleteMediaError(RuntimeError):
    pass


class SpoolBusyError(RuntimeError):
    pass


class LimitedWriter(io.BufferedWriter):
    def __init__(self, raw, root, max_bytes, reserve_bytes):
        super().__init__(raw)
        self.root = root
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes

    def write(self, data):
        if self.tell() + len(data) > self.max_bytes:
            raise MediaTooLargeError('Media exceeds configured limit')
        if shutil.disk_usage(self.root).free - len(data) < self.reserve_bytes:
            raise InsufficientDiskError('Disk reserve would be exceeded')
        return super().write(data)


class MediaSpool:
    checkpoint_minimum = 512 * 1024
    checkpoint_lifetime = 24 * 60 * 60
    checkpoint_limit = 2

    def __init__(self, root, max_bytes, reserve_bytes):
        if max_bytes <= 0 or reserve_bytes <= 0:
            raise ValueError('Spool limits must be positive')
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError('Spool directory must not be a symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes
        self.download_pacing = DownloadPacingHistory()
        self.in_use = set()
        self.lock = (self.root / '.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise SpoolBusyError('Another transfer process owns the spool') from None
        try:
            # Recover disk space after kill -9/power loss, without touching indexes
            # or sessions. The exclusive lock prevents deleting active transfers.
            for child in self.root.glob('transfer-*'):
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
            self.prune_checkpoints()
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.lock.closed:
            self.lock.close()

    def prune_checkpoints(self, for_new=False, evict_unused=False):
        candidates = []
        now = time.time()
        for child in self.root.iterdir():
            if child in self.in_use or not re.fullmatch(r'(?:ready|partial)-[0-9a-f]{64}', child.name):
                continue
            if child.is_symlink() or not child.is_dir():
                child.unlink()
                continue
            age = now - child.stat().st_mtime
            if age >= self.checkpoint_lifetime:
                shutil.rmtree(child)
            else:
                candidates.append((age, child))
        slots = 0 if evict_unused else max(0, self.checkpoint_limit - len(self.in_use) - int(for_new))
        for _, child in sorted(candidates)[slots:]:
            shutil.rmtree(child)

    def checkpoint_directory(self, scope, message, size):
        identifier = getattr(getattr(message, 'document', None), 'id', None)
        if not scope or not identifier or size is None or size < self.checkpoint_minimum:
            return None
        key = (scope, message.chat_id, message.id, identifier, size)
        digest = hashlib.sha256(repr(key).encode()).hexdigest()
        path = self.root / ('ready-' + digest)
        return path if path not in self.in_use else None

    @staticmethod
    def sync_file(path):
        with path.open('rb') as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def recoverable(error):
        return (isinstance(error, (ConnectionError, asyncio.TimeoutError, asyncio.CancelledError))
                or rate_wait(error, 0) is not None)

    @asynccontextmanager
    async def held_checkpoint(self, directory, name):
        self.in_use.add(directory)
        preserve = False
        try:
            yield directory / name
        except BaseException as error:
            preserve = self.recoverable(error) or isinstance(error, DeliveryUnknownError)
            raise
        finally:
            self.in_use.remove(directory)
            if not preserve:
                shutil.rmtree(directory)

    @asynccontextmanager
    async def stage(self, client, message, progress=None, workers=1, stats=None,
                    worker_limit=None, checkpoint_scope=None):
        size = getattr(getattr(message, 'file', None), 'size', None)
        if size is not None and size > self.max_bytes:
            raise MediaTooLargeError('Media exceeds configured limit')
        suffix = getattr(getattr(message, 'file', None), 'ext', '') or ''
        if not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', suffix):
            suffix = '.jpg' if message.photo else '.bin'
        name = 'media' + suffix
        checkpoint = self.checkpoint_directory(checkpoint_scope, message, size)
        self.prune_checkpoints()
        if checkpoint and checkpoint.is_dir():
            saved = checkpoint / name
            if saved.is_file() and not saved.is_symlink() and saved.stat().st_size == size:
                # Rendered previews may have been left by the interrupted
                # attempt. Rebuild them from the original for this delivery.
                for auxiliary in ('preview.jpg', 'thumbnail.jpg'):
                    (checkpoint / auxiliary).unlink(missing_ok=True)
                if stats is not None:
                    stats.update(transport='disk_checkpoint', bytes=size, total=size)
                async with self.held_checkpoint(checkpoint, name) as path:
                    yield path
                return
            shutil.rmtree(checkpoint)
        partial = checkpoint.with_name(checkpoint.name.replace('ready-', 'partial-', 1)) if checkpoint else None
        state = PartialDownload() if partial else None
        if state and partial.is_dir():
            self.in_use.add(partial)
            try:
                await asyncio.to_thread(state.load, partial, name, size)
            finally:
                self.in_use.remove(partial)
            if not state.parts:
                shutil.rmtree(partial)
        required = size if size is not None else self.max_bytes
        if state and state.parts:
            required -= sum(min(state.chunk, size-offset) for offset in state.parts)
        if shutil.disk_usage(self.root).free < required + self.reserve_bytes:
            if checkpoint:
                self.in_use.add(partial)
                try:
                    self.prune_checkpoints(evict_unused=True)
                finally:
                    self.in_use.remove(partial)
            if not checkpoint or shutil.disk_usage(self.root).free < required + self.reserve_bytes:
                raise InsufficientDiskError('Not enough free disk space')
        with tempfile.TemporaryDirectory(prefix='transfer-', dir=self.root) as directory:
            path = Path(directory) / name
            if state and state.parts:
                (partial / name).rename(path)
                shutil.rmtree(partial)
            else:
                path.touch(mode=0o600, exist_ok=False)
            raw = path.open('r+b', buffering=0)
            try:
                with LimitedWriter(raw, self.root, self.max_bytes, self.reserve_bytes) as stream:
                    await download_file(client, message, stream, progress=progress, workers=workers,
                                        stats=stats, worker_limit=worker_limit,
                                        pacing_history=self.download_pacing, checkpoint=state)
            except BaseException as error:
                if state and state.parts and self.recoverable(error):
                    with suppress(OSError):
                        await asyncio.to_thread(self.sync_file, path)
                        if not partial.exists() and len(self.in_use) < self.checkpoint_limit:
                            state.save(Path(directory))
                            self.prune_checkpoints(for_new=True)
                            os.utime(directory, None)
                            Path(directory).rename(partial)
                raise
            actual = path.stat().st_size
            if actual == 0 or (size is not None and size > 0 and actual != size):
                raise IncompleteMediaError('Downloaded media size does not match')
            if partial and partial.is_dir() and partial not in self.in_use:
                shutil.rmtree(partial)
            if checkpoint and len(self.in_use) < self.checkpoint_limit:
                await asyncio.to_thread(self.sync_file, path)
            if (checkpoint and not checkpoint.exists()
                    and len(self.in_use) < self.checkpoint_limit):
                self.prune_checkpoints(for_new=True)
                os.utime(directory, None)
                Path(directory).rename(checkpoint)
                async with self.held_checkpoint(checkpoint, name) as saved:
                    yield saved
            else:
                yield path
