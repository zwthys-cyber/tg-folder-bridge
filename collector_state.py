"""Collector ledger and read-only reconciliation with the bridge database."""
import sqlite3
import re
import time
import contextlib
from pathlib import Path
from urllib.parse import urlencode
from core import parse_link

ACTIVE = {'running', 'queued', 'cooldown'}


def canonical(url, allowed):
    name, token = parse_link(url, allowed)
    return f'https://t.me/{name}?' + urlencode({'start': token})


def existing_jobs(path, owner, allowed):
    # Never instantiate core.Store here: its startup recovery mutates jobs.
    with contextlib.closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
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
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(items)')}
        if 'priority' not in columns:
            self.db.execute('ALTER TABLE items ADD COLUMN priority INTEGER DEFAULT 0')
        for column in ('observed_count', 'refresh_count'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE items ADD COLUMN {column} INTEGER')
        self.db.commit()

    def add(self, source, key, title, link, observed_count=None):
        with self.db:
            suffix = re.search(r'(\d+)\s*$', title)
            priority = int(suffix[1]) if suffix else 0
            added = self.db.execute('INSERT OR IGNORE INTO items(source,item_key,title,link,priority) VALUES(?,?,?,?,?)',
                            (source, key, title, link, priority)).rowcount > 0
            self.db.execute('INSERT OR IGNORE INTO discoveries VALUES(?,?)', (source, key))
            if observed_count is not None:
                self.db.execute('UPDATE items SET observed_count=? WHERE link=?', (observed_count, link))
            return added

    def seen(self, source, key):
        return self.db.execute('SELECT 1 FROM discoveries WHERE source=? AND item_key=?', (source, key)).fetchone() is not None

    def page(self, source):
        row = self.db.execute('SELECT page,total,finished FROM cursor WHERE source=?', (source,)).fetchone()
        return tuple(row) if row else (1, 0, 0)

    def advance(self, source, page, total):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cursor VALUES(?,?,?,?)',
                            (source, min(page + 1, total), total, int(page == total)))

    def set_state(self, row, state, job=None, attempt=False, refresh_count=None):
        with self.db:
            self.db.execute('UPDATE items SET state=?, job=COALESCE(?,job), attempts=attempts+?, '
                            'refresh_count=COALESCE(?,refresh_count) '
                            'WHERE source=? AND item_key=?',
                            (state, job, int(attempt), refresh_count, row['source'], row['item_key']))

    def reconcile(self, jobs):
        for row in self.db.execute('SELECT * FROM items').fetchall():
            job = jobs.get(row['link'])
            if not job:
                # dispatching/submitted with no bridge record stays uncertain. Never resend blindly.
                continue
            if job['state']=='cancelled':
                self.set_state(row,'ignored',job['id'])
                continue
            if row['state'] == 'dispatching' and row['refresh_count'] is not None:
                if job['state'] == 'complete' and (job.get('expected') or 0) < row['refresh_count']:
                    continue
                if job.get('refresh_expected') == row['refresh_count'] and job['state'] not in ACTIVE:
                    self.set_state(row, 'review', job['id'])
                    continue
            state = 'complete' if job['state'] == 'complete' else (
                'submitted' if job['state'] in ACTIVE else (
                    'ready' if row['attempts'] == 0 else 'review'))
            if row['state'] == 'dispatching' and job['state'] not in ACTIVE | {'complete'}:
                continue
            self.set_state(row, state, job['id'])

    def next(self):
        row = self.db.execute("SELECT * FROM items WHERE state='ready' ORDER BY priority DESC,rowid LIMIT 1").fetchone()
        return dict(row) if row else None

    def pending(self):
        return self.db.execute("SELECT * FROM items WHERE state='ready' OR "
                               "(state='complete' AND observed_count>coalesce(refresh_count,0)) "
                               "ORDER BY priority DESC,rowid").fetchall()

    @staticmethod
    def refresh_target(item, job):
        if not job or job['state'] != 'complete' or not job.get('expected'):
            return None
        count = item['observed_count']
        if count is not None and max(job['expected'], item['refresh_count'] or 0) < count <= 5000:
            return count

    def begin_head(self, source):
        with self.db:
            key = 'head-base:' + source
            if not self.db.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone():
                baseline = self.db.execute('SELECT coalesce(max(rowid),0) FROM items').fetchone()[0]
                self.db.execute('INSERT INTO settings VALUES(?,?)', (key, baseline))
                self.head_checkpoint(source, 1, 0)

    def known_before_head(self, source, link):
        base = self.db.execute('SELECT value FROM settings WHERE key=?', ('head-base:' + source,)).fetchone()
        if base is None:
            raise RuntimeError('Head scan has not been initialized')
        row = self.db.execute('SELECT rowid FROM items WHERE link=?', (link,)).fetchone()
        return bool(row and row[0] <= base[0])

    def head_position(self, source):
        page = self.db.execute('SELECT value FROM settings WHERE key=?', ('head-page:' + source,)).fetchone()
        clean = self.db.execute('SELECT value FROM settings WHERE key=?', ('head-clean:' + source,)).fetchone()
        return (int(page[0]) if page else 1, int(clean[0]) if clean else 0)

    def head_checkpoint(self, source, page, clean):
        with self.db:
            self.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                [('head-page:' + source, page), ('head-clean:' + source, clean)])

    def head_due(self, source, interval=600):
        if self.db.execute('SELECT 1 FROM settings WHERE key=?', ('head-base:' + source,)).fetchone():
            return True
        row = self.db.execute('SELECT value FROM settings WHERE key=?', ('head:' + source,)).fetchone()
        return not row or time.time() - row[0] >= interval

    def checked_head(self, source):
        with self.db:
            self.db.executemany('DELETE FROM settings WHERE key=?',
                [('head-base:' + source,), ('head-page:' + source,), ('head-clean:' + source,)])
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('head:' + source, time.time()))

    def revisit_page(self, source, total):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', ('revisit:' + source,)).fetchone()
        return int(row[0]) if row and 3 <= row[0] <= total else 3

    def checked_revisit(self, source, page, total):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                            ('revisit:' + source, page + 1 if page < total else 3))

    def summary(self):
        return dict(self.db.execute('SELECT state,count(*) FROM items GROUP BY state'))
