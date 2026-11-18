"""A portable map of written parts, independent of the number of download workers."""
import hashlib
import json


class PartialDownload:
    def __init__(self):
        self.chunk = 0
        self.parts = {}

    def reset(self):
        self.chunk = 0
        self.parts.clear()

    def begin(self, chunk):
        if self.chunk != chunk:
            self.reset()
            self.chunk = chunk

    def record(self, offset, data):
        self.parts[offset] = hashlib.sha256(data).hexdigest()

    def load(self, directory, name, size):
        """Recheck disk bytes before treating a recorded offset as complete."""
        try:
            manifest = directory / 'parts.json'
            data = directory / name
            if manifest.is_symlink() or data.is_symlink() or manifest.stat().st_size > 2 * 1024**2:
                return
            document = json.loads(manifest.read_text())
            chunk, parts = document['chunk'], document['parts']
            if chunk not in (512 * 1024, 1024 * 1024) or not isinstance(parts, dict):
                return
            with data.open('rb') as stream:
                if data.stat().st_size > size:
                    return
                self.chunk = chunk
                for raw_offset, digest in parts.items():
                    offset = int(raw_offset)
                    if offset < 0 or offset >= size or offset % chunk:
                        continue
                    length = min(chunk, size - offset)
                    stream.seek(offset)
                    saved = stream.read(length)
                    if len(saved) == length and hashlib.sha256(saved).hexdigest() == digest:
                        self.parts[offset] = digest
        except (OSError, ValueError, TypeError, KeyError):
            self.reset()

    def save(self, directory):
        (directory / 'parts.json').write_text(json.dumps(dict(chunk=self.chunk, parts=self.parts)))
