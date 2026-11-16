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


def connections(path):
    path = Path(path)
    value = json.loads(path.read_text()).get('upload_connections', 1) if path.exists() else 1
    if type(value) is not int or not 1 <= value <= 4:
        raise ValueError('Upload connections must be 1–4')
    return value


def scheduler(path):
    path = Path(path)
    value = json.loads(path.read_text()).get('upload_scheduler', 'round_robin') if path.exists() else 'round_robin'
    if value not in {'round_robin', 'balanced'}:
        raise ValueError('Unknown upload scheduler')
    return value


def save(path, download, upload, automatic=False, upload_connections=None, upload_scheduler=None):
    validate(download, upload)
    path = Path(path)
    if upload_connections is None:
        upload_connections = connections(path)
    if type(upload_connections) is not int or not 1 <= upload_connections <= 4:
        raise ValueError('Upload connections must be 1–4')
    if upload_scheduler is None:
        upload_scheduler = scheduler(path)
    if upload_scheduler not in {'round_robin', 'balanced'}:
        raise ValueError('Unknown upload scheduler')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(dict(download_workers=download, upload_workers=upload,
                           automatic=automatic, upload_connections=upload_connections,
                           upload_scheduler=upload_scheduler), output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
