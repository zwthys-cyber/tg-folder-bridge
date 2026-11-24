"""Read-only serving export. No media, relay authentication or job recovery writes."""
import argparse
import hashlib
import json
import os
import sqlite3
import urllib.request
import uuid
from pathlib import Path
from core import Store


def snapshot(path):
    store = Store.__new__(Store)
    store.db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    store.db.row_factory = sqlite3.Row
    try:
        # One consistent SQLite read transaction; never call Store.__init__ here.
        store.db.execute('BEGIN')
        folders = []
        for job in store.catalog():
            items = []
            cover = None
            for item in store.files(job['id']):
                if cover is None and (item.get('source_kind') == 'photo' or
                        Path(item.get('source_name') or '').suffix.lower() in {'.jpg', '.jpeg', '.heic', '.heif', '.png', '.webp'}):
                    cover = item.get('preview_file_id') or (item['file_id'] if item['kind'] == 'photo' else None)
                items.append(dict(kind=item['kind'], file_id=item['file_id'],
                    source_kind=item.get('source_kind') or item['kind'],
                    file_size=item.get('file_size'), name=item.get('source_name') or '',
                    caption=(item.get('caption') or '')[:1024]))
            folder = {key: job.get(key) or '' for key in
                ('id', 'title', 'partial_notice', 'source_created_at', 'source_updated_at')}
            folder['title'] = (folder['title'] or '文件夹 ' + job['id'][:8])[:160]
            folder.update(items=items, cover=cover)
            if len(json.dumps(folder, ensure_ascii=False).encode()) > 900000:
                raise ValueError('Folder exceeds D1 row budget: ' + job['id'])
            folders.append(folder)
        return dict(folders=folders,
                    favorites=[dict(x) for x in store.db.execute('SELECT owner,job FROM favorites')],
                    subscribers=[dict(x) for x in store.db.execute('SELECT owner,enabled FROM subscribers')])
    finally:
        store.db.close()


class EdgeClient:
    def __init__(self, base=None, token=None):
        self.base = (base or os.environ['EDGE_URL']).rstrip('/')
        self.token = token or os.environ['EDGE_SYNC_TOKEN']
        if not self.base.startswith('https://') or not self.token:
            raise ValueError('Edge requires HTTPS and a private API token')

    def call(self, path, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(self.base + '/api/' + path, data=data,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)

    def publish(self, data, bot_id, seed=False):
        generation = uuid.uuid4().hex
        self.call('sync/begin', dict(id=generation, bot_id=bot_id, count=len(data['folders'])))
        for i, folder in enumerate(data['folders']):
            self.call('sync/folder', dict(generation=generation, ordinal=i, folder=folder))
        if seed:
            for kind in ('favorites', 'subscribers'):
                for start in range(0, len(data[kind]), 50):
                    self.call('sync/preferences', {'generation': generation, kind: data[kind][start:start+50]})
        return self.call('sync/publish', dict(generation=generation))


def fingerprint(data):
    # Edge owns preferences after migration; local copies must not force resync.
    return hashlib.sha256(json.dumps(data['folders'], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    parser.add_argument('--bot-id', type=int, required=True)
    parser.add_argument('--seed-preferences', action='store_true')
    args = parser.parse_args()
    data = snapshot(args.database)
    result = EdgeClient().publish(data, args.bot_id, args.seed_preferences)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
