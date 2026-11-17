"""Persistent automation switches shared by collector and bridge."""
import json
import os
import tempfile
from pathlib import Path


def read(root):
    path = Path(root) / 'automation.json'
    return json.loads(path.read_text()) if path.exists() else {}


def write(root, values, name='automation.json'):
    path = Path(root) / name
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(values, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def paused(root, name):
    return bool(read(root).get(name + '_paused', False))


def toggle(root, name):
    values = read(root)
    values[name + '_paused'] = not bool(values.get(name + '_paused', False))
    write(root, values)
    return values[name + '_paused']


def set_paused(root,name,value):
    values=read(root)
    values[name+'_paused']=bool(value)
    write(root,values)
