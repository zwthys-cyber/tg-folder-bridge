"""A single-file disk spool. Only this module's transfer-* directories are cleaned."""
import fcntl
import io
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


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
    def __init__(self, root, max_bytes, reserve_bytes):
        if max_bytes <= 0 or reserve_bytes <= 0:
            raise ValueError('Spool limits must be positive')
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError('Spool directory must not be a symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes
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
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.lock.closed:
            self.lock.close()

    @asynccontextmanager
    async def stage(self, client, message):
        size = getattr(getattr(message, 'file', None), 'size', None)
        if size is not None and size > self.max_bytes:
            raise MediaTooLargeError('Media exceeds configured limit')
        required = size if size is not None else self.max_bytes
        if shutil.disk_usage(self.root).free < required + self.reserve_bytes:
            raise InsufficientDiskError('Not enough free disk space')
        suffix = getattr(getattr(message, 'file', None), 'ext', '') or ''
        if not re.fullmatch(r'\.[A-Za-z0-9]{1,12}', suffix):
            suffix = '.jpg' if message.photo else '.bin'
        with tempfile.TemporaryDirectory(prefix='transfer-', dir=self.root) as directory:
            path = Path(directory) / ('media' + suffix)
            path.touch(mode=0o600, exist_ok=False)
            raw = path.open('wb', buffering=0)
            with LimitedWriter(raw, self.root, self.max_bytes, self.reserve_bytes) as stream:
                await client.download_media(message, file=stream)
            actual = path.stat().st_size
            if actual == 0 or (size is not None and size > 0 and actual != size):
                raise IncompleteMediaError('Downloaded media size does not match')
            # The caller uploads and awaits receipt while the context stays alive.
            # Success, exception, timeout, or cancellation all unlink the file.
            yield path
