"""Private, atomic persistence for live transfer concurrency."""
import json
import os
import tempfile
from pathlib import Path

MAX_WORKERS = 32


def validate(download, upload):
    if any(type(x) is not int or not 1 <= x <= MAX_WORKERS for x in (download, upload)):
        raise ValueError('下载、上传并发必须是 1–32 的整数')
    return download, upload


def load(path, download, upload):
    path = Path(path)
    if path.exists():
        values = json.loads(path.read_text())
        download, upload = values['download_workers'], values['upload_workers']
    return validate(download, upload)


def automatic(path):
    path = Path(path)
    return path.exists() and json.loads(path.read_text()).get('automatic', False) is True


def save(path, download, upload, automatic=False):
    validate(download, upload)
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(dict(download_workers=download, upload_workers=upload,
                           automatic=automatic), output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
