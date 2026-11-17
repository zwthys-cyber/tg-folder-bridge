"""Collector ledger and read-only reconciliation with the bridge database."""
import sqlite3
from pathlib import Path
from urllib.parse import urlencode
from core import parse_link

ACTIVE = {'running', 'queued', 'cooldown'}


def canonical(url, allowed):
    name, token = parse_link(url, allowed)
    return f'https://t.me/{name}?' + urlencode({'start': token})


def existing_jobs(path, owner, allowed):
    # Never instantiate core.Store here: its startup recovery mutates jobs.
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY rowid', (owner,))]
    result = {}
    for row in rows:
        try:
            key = canonical(row['link'], allowed)
        except ValueError:
            continue
        old = result.get(key)
        # A complete job takes precedence over older or newer failed attempts.
        if not old or (old['state'] != 'complete' and
                       (row['state'] == 'complete' or old['state'] not in ACTIVE)):
            result[key] = row
    return rows, result


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS items (
                source TEXT, item_key TEXT, title TEXT, link TEXT,
                state TEXT DEFAULT 'ready', job TEXT DEFAULT '', attempts INTEGER DEFAULT 0,
                PRIMARY KEY(source,item_key), UNIQUE(link)
            );
            CREATE TABLE IF NOT EXISTS cursor (
                source TEXT PRIMARY KEY, page INTEGER NOT NULL DEFAULT 1,
                total INTEGER DEFAULT 0, finished INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS discoveries (
                source TEXT, item_key TEXT, PRIMARY KEY(source,item_key)
            );
        ''')

    def add(self, source, key, title, link):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO items(source,item_key,title,link) VALUES(?,?,?,?)',
                            (source, key, title, link))
            self.db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?)', (source, key))

    def seen(self, source, key):
        return self.db.execute('SELECT 1 FROM discoveries WHERE source=? AND item_key=?', (source, key)).fetchone() is not None

    def page(self, source):
        row = self.db.execute('SELECT page,total,finished FROM cursor WHERE source=?', (source,)).fetchone()
        return tuple(row) if row else (1, 0, 0)

    def advance(self, source, page, total):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cursor VALUES(?,?,?,?)',
                            (source, min(page + 1, total), total, int(page == total)))

    def set_state(self, row, state, job=None, attempt=False):
        with self.db:
            self.db.execute('UPDATE items SET state=?, job=COALESCE(?,job), attempts=attempts+? '
                            'WHERE source=? AND item_key=?',
                            (state, job, int(attempt), row['source'], row['item_key']))

    def reconcile(self, jobs):
        for row in self.db.execute('SELECT * FROM items').fetchall():
            job = jobs.get(row['link'])
            if not job:
                # dispatching/submitted with no bridge record stays uncertain. Never resend blindly.
                continue
            state = 'complete' if job['state'] == 'complete' else (
                'submitted' if job['state'] in ACTIVE else (
                    'ready' if row['attempts'] == 0 else 'review'))
            if row['state'] == 'dispatching' and job['state'] not in ACTIVE | {'complete'}:
                continue
            self.set_state(row, state, job['id'])

    def next(self):
        row = self.db.execute("SELECT * FROM items WHERE state='ready' ORDER BY rowid LIMIT 1").fetchone()
        return dict(row) if row else None

    def summary(self):
        return dict(self.db.execute('SELECT state,count(*) FROM items GROUP BY state'))
