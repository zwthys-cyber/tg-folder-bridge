"""Pure parsing and persistent metadata. No media bytes are written here."""
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlparse


class ViewProgress:
    def __init__(self, store, row):
        self.store,self.id,self.job,self.owner=store,row['id'],row['job'],row['owner']

    @property
    def details_sent(self):
        return bool(self.store.db.execute('SELECT detail_sent FROM view_sessions WHERE id=?',(self.id,)).fetchone()[0])

    @property
    def message_count(self):
        return self.store.db.execute('SELECT sent_messages FROM view_sessions WHERE id=?',(self.id,)).fetchone()[0]

    def record_details(self):
        self.store.db.execute('UPDATE view_sessions SET detail_sent=1,sent_messages=sent_messages+1 '
                              'WHERE id=? AND detail_sent=0',(self.id,))
        self.store.db.commit()

    def record(self, items):
        with self.store.db:
            self.store.db.executemany('INSERT OR IGNORE INTO view_receipts VALUES(?,?,?)',
                [(self.id,x['source_chat'],x['source_message']) for x in items])
            self.store.db.execute('UPDATE view_sessions SET sent_messages=sent_messages+? WHERE id=?',
                                  (len(items),self.id))

    def received_count(self, items):
        received=set(tuple(r) for r in self.store.db.execute(
            'SELECT source_chat,source_message FROM view_receipts WHERE session=?',(self.id,)))
        return sum((x['source_chat'],x['source_message']) in received for x in items)

    def bookmark(self, page):
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO bookmarks VALUES(?,?,?,?)',
                                  (self.owner, self.job, page, self.id))


def parse_link(url, allowed):
    p = urlparse(url.strip())
    if p.scheme != 'https' or p.netloc.lower() != 't.me':
        raise ValueError('仅支持 https://t.me/机器人?start=参数')
    name = p.path.strip('/')
    args = parse_qs(p.query).get('start', [])
    if not re.fullmatch(r'[A-Za-z0-9_]{1,32}', name):
        raise ValueError('机器人用户名无效')
    if name.lower() not in {x.lower() for x in allowed}:
        raise ValueError('跳转目标不在 ALLOWED_SOURCE_BOTS 中')
    if len(args) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args[0]):
        raise ValueError('分享参数无效')
    return name.lower(), args[0]


def media_count(text):
    counts = re.findall(r'(?:图片数|视频数|文档数)\s*[：:]\s*([\d,]+)', text)
    return sum(int(x.replace(',', '')) for x in counts) if counts else None


def media_key(message):
    for kind in ('document', 'photo'):
        media = getattr(message, kind, None)
        identifier = getattr(media, 'id', None)
        if identifier is not None:
            return f'{kind}:{identifier}'
    return ''


def pagination_marker(text, labels):
    """Ignore changing clocks when the actual batch counters stay the same."""
    combined = text + '\n' + '\n'.join(labels)
    counters = re.findall(r'(\d+(?:\s*-\s*\d+)?)\s*/\s*(\d+)', combined)
    if counters:
        return tuple((re.sub(r'\s+', '', a), b) for a, b in counters)
    return text, tuple(labels)


def folder_title(text):
    match = re.search(r'^\s*[📁📂]\ufe0f?\s*([^\n]+)', text, re.MULTILINE)
    return match[1].strip()[:100] if match else ''


def folder_metadata(text):
    result = {}
    for label, field in (('创建时间', 'source_created_at'), ('最后更新', 'source_updated_at')):
        match = re.search(label + r'\s*[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', text)
        if match:
            result[field] = match[1]
    return result


def source_entry(message, position):
    file = getattr(message, 'file', None)
    document = getattr(message, 'document', None)
    mime = getattr(file, 'mime_type', '') or getattr(document, 'mime_type', '') or ''
    kind = 'photo' if getattr(message, 'photo', None) else ('video' if mime.startswith('video/') else 'document')
    return dict(source_chat=message.chat_id, source_message=message.id, position=position,
                name=getattr(file, 'name', None) or '', size=getattr(file, 'size', None), kind=kind)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS catalog_panels (
                chat INTEGER, list_message INTEGER, stats_message INTEGER,
                PRIMARY KEY(chat,list_message)
            );
            CREATE TABLE IF NOT EXISTS favorites (
                owner INTEGER, job TEXT, PRIMARY KEY(owner,job)
            );
            CREATE TABLE IF NOT EXISTS view_sessions (
                id TEXT PRIMARY KEY, owner INTEGER, job TEXT,
                detail_sent INTEGER DEFAULT 0, sent_messages INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS bookmarks (
                owner INTEGER, job TEXT, page INTEGER, session TEXT, PRIMARY KEY(owner,job)
            );
            CREATE TABLE IF NOT EXISTS retry_schedule (
                job TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS view_receipts (
                session TEXT, source_chat INTEGER, source_message INTEGER,
                PRIMARY KEY(session,source_chat,source_message)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner INTEGER, link TEXT,
                state TEXT, expected INTEGER, detail TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS files (
                job TEXT, source_chat INTEGER, source_message INTEGER,
                chat INTEGER, message INTEGER, file_id TEXT, kind TEXT,
                PRIMARY KEY(job, source_chat, source_message)
            );
            CREATE TABLE IF NOT EXISTS transfer_metrics (
                id INTEGER PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP,
                job TEXT, phase TEXT, payload TEXT
            );
            CREATE TABLE IF NOT EXISTS local_send_intents (
                id INTEGER PRIMARY KEY, job TEXT, source_chat INTEGER,
                source_message INTEGER, marker TEXT, state TEXT
            );
            CREATE TABLE IF NOT EXISTS file_aliases (
                job TEXT, source_chat INTEGER, source_message INTEGER, media_key TEXT,
                canonical_chat INTEGER, canonical_message INTEGER,
                PRIMARY KEY(job,source_chat,source_message)
            );
            CREATE TABLE IF NOT EXISTS source_entries (
                job TEXT, source_chat INTEGER, source_message INTEGER,
                position INTEGER, name TEXT, size INTEGER, kind TEXT,
                PRIMARY KEY(job,source_chat,source_message)
            );
        ''')
        columns = {x['name'] for x in self.db.execute('PRAGMA table_info(files)')}
        if 'caption' not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN caption TEXT DEFAULT ''")
        if 'preview_file_id' not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN preview_file_id TEXT DEFAULT ''")
        if 'media_key' not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN media_key TEXT DEFAULT ''")
        if 'has_thumbnail' not in columns:
            self.db.execute('ALTER TABLE files ADD COLUMN has_thumbnail INTEGER DEFAULT 0')
        for column, default in (('content_sha256', "''"), ('stream_sha256', "''"), ('quality_json', "'{}'")):
            if column not in columns:
                self.db.execute(f'ALTER TABLE files ADD COLUMN {column} TEXT DEFAULT {default}')
        job_columns = {x['name'] for x in self.db.execute('PRAGMA table_info(jobs)')}
        if 'title' not in job_columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN title TEXT DEFAULT ''")
        if 'restart_pending' not in job_columns:
            self.db.execute('ALTER TABLE jobs ADD COLUMN restart_pending INTEGER DEFAULT 0')
        for column in ('audit_requested', 'audit_public'):
            if column not in job_columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {column} INTEGER DEFAULT 0')
        for column in ('source_created_at', 'source_updated_at', 'cover_file_id'):
            if column not in job_columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT DEFAULT ''")
        self.db.execute("UPDATE jobs SET restart_pending=1 WHERE state IN ('running','queued','cooldown')")
        self.recoverable_jobs = [row['id'] for row in self.db.execute(
            "SELECT id FROM jobs WHERE restart_pending=1 AND state IN "
            "('running','queued','cooldown','interrupted') ORDER BY rowid")]
        self.db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running','cooldown')")
        self.db.commit()

    def create(self, job, owner, link):
        self.db.execute('INSERT INTO jobs(id,owner,link,state,restart_pending) VALUES(?,?,?,?,1)',
                        (job, owner, link, 'queued'))
        self.db.commit()

    def begin_local_send(self, job, source, marker):
        if self.unknown_local_send(job):
            raise RuntimeError('An earlier local delivery needs review')
        cursor = self.db.execute('INSERT INTO local_send_intents(job,source_chat,source_message,marker,state) '
                                 "VALUES(?,?,?,?,'pending')", (job, *source, marker))
        self.db.commit()
        return cursor.lastrowid

    def unknown_local_send(self, job):
        return self.db.execute("SELECT 1 FROM local_send_intents WHERE job=? AND state='pending' LIMIT 1",
                               (job,)).fetchone() is not None

    def reject_local_send(self, intent):
        self.db.execute("UPDATE local_send_intents SET state='rejected' WHERE id=?", (intent,))
        self.db.commit()

    def authorize_local_retry(self, job):
        self.db.execute("UPDATE local_send_intents SET state='retry_authorized' WHERE job=? AND state='pending'", (job,))
        self.db.commit()

    def confirm_local_send(self, intent, save):
        try:
            self.db.execute("UPDATE local_send_intents SET state='confirmed' WHERE id=? AND state='pending'", (intent,))
            save()  # add_file / set_preview commits this transaction too.
        except BaseException:
            self.db.rollback()
            raise

    def record_transfer(self, job, phase, stats, error=''):
        import json
        keys = ('bytes', 'total', 'seconds', 'average_mib', 'recent_mib',
                'recent_seconds', 'connections', 'transport', 'workers', 'p50_ms', 'p95_ms',
                'retries', 'scheduler')
        payload = {key: stats[key] for key in keys if key in stats}
        payload['error'] = error
        self.db.execute('INSERT INTO transfer_metrics(job,phase,payload) VALUES(?,?,?)',
                        (job, phase, json.dumps(payload)))
        self.db.execute('DELETE FROM transfer_metrics WHERE id NOT IN '
                        '(SELECT id FROM transfer_metrics ORDER BY id DESC LIMIT 500)')
        self.db.commit()

    def transfer_history(self, limit=10):
        import json
        return [dict(row, payload=json.loads(row['payload'])) for row in self.db.execute(
            'SELECT * FROM transfer_metrics ORDER BY id DESC LIMIT ?', (limit,))]

    def update(self, job, state, expected=None, detail=''):
        self.db.execute('UPDATE jobs SET state=?,expected=?,detail=?,restart_pending=CASE '
                        "WHEN ? IN ('queued','running','cooldown') THEN 1 "
                        "WHEN ?='interrupted' THEN restart_pending ELSE 0 END WHERE id=?",
                        (state, expected, detail, state, state, job))
        self.db.commit()

    def get(self, job):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        return dict(row) if row else None

    def find_link(self, owner, link, allowed):
        target = parse_link(link, allowed)
        matches = []
        for row in self.db.execute('SELECT * FROM jobs WHERE owner=? ORDER BY rowid DESC', (owner,)):
            try:
                if parse_link(row['link'], allowed) == target:
                    matches.append(dict(row))
            except ValueError:
                continue
        return next((row for row in matches if row['state'] == 'complete'),
                    next((row for row in matches if row['state'] in {'queued','running','cooldown'}),
                         matches[0] if matches else None))

    def files(self, job):
        return [dict(x) for x in self.db.execute(
            '''SELECT f.*, e.name AS source_name, e.size AS file_size, e.kind AS source_kind
            FROM files f LEFT JOIN source_entries e ON e.job=f.job
              AND e.source_chat=f.source_chat AND e.source_message=f.source_message
            WHERE f.job=? ORDER BY COALESCE(
              (SELECT MIN(p.position) FROM source_entries p WHERE p.job=f.job AND
                ((p.source_chat=f.source_chat AND p.source_message=f.source_message) OR
                  EXISTS(SELECT 1 FROM file_aliases a WHERE a.job=f.job
                    AND a.source_chat=p.source_chat AND a.source_message=p.source_message
                    AND a.canonical_chat=f.source_chat AND a.canonical_message=f.source_message))),
              MIN(f.source_message, COALESCE((SELECT MIN(a.source_message) FROM file_aliases a
                WHERE a.job=f.job AND a.canonical_chat=f.source_chat
                  AND a.canonical_message=f.source_message), f.source_message))),
              f.source_chat, f.source_message''', (job,))]

    def set_source_entries(self, job, entries):
        self.db.executemany('INSERT OR REPLACE INTO source_entries VALUES(?,?,?,?,?,?,?)',
            [(job, e['source_chat'], e['source_message'], e['position'], e['name'], e['size'], e['kind'])
             for e in entries])
        self.db.commit()

    def set_folder_metadata(self, job, metadata):
        for field in ('source_created_at', 'source_updated_at'):
            if metadata.get(field):
                self.db.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (metadata[field], job))
        if metadata:
            self.db.commit()

    def set_cover(self, job, file_id):
        self.db.execute('UPDATE jobs SET cover_file_id=? WHERE id=?', (file_id, job))
        self.db.commit()

    def register_catalog_panel(self, chat, list_message, stats_message=None):
        if not isinstance(list_message,int) or (stats_message is not None and not isinstance(stats_message,int)):
            return
        self.db.execute('INSERT OR REPLACE INTO catalog_panels VALUES(?,?,?)',
                        (chat,list_message,stats_message))
        self.db.commit()

    def catalog_panel_stats(self, chat, list_message):
        row=self.db.execute('SELECT stats_message FROM catalog_panels WHERE chat=? AND list_message=?',
                            (chat,list_message)).fetchone()
        return row['stats_message'] if row else None

    def forget_catalog_panel(self, chat, list_message):
        self.db.execute('DELETE FROM catalog_panels WHERE chat=? AND list_message=?',(chat,list_message))
        self.db.commit()

    def is_favorite(self, owner, job):
        return bool(self.db.execute('SELECT 1 FROM favorites WHERE owner=? AND job=?', (owner,job)).fetchone())

    def favorite(self, owner, job, saved=True):
        if saved:
            self.db.execute('INSERT OR IGNORE INTO favorites VALUES(?,?)', (owner,job))
        else:
            self.db.execute('DELETE FROM favorites WHERE owner=? AND job=?', (owner,job))
        self.db.commit()

    def favorite_catalog(self, owner):
        return [dict(r) for r in self.db.execute('''SELECT j.*,count(f.file_id) AS file_count
            FROM favorites v JOIN jobs j ON j.id=v.job JOIN files f ON f.job=j.id
            WHERE v.owner=? AND (j.state='complete' OR j.audit_public=1 OR j.owner=?)
            GROUP BY j.id ORDER BY j.rowid DESC''', (owner,owner))]

    def start_view(self, owner, job, details_sent=False):
        sid=secrets.token_hex(6)
        self.db.execute('INSERT INTO view_sessions(id,owner,job,detail_sent) VALUES(?,?,?,?)',
                        (sid,owner,job,int(details_sent)))
        self.db.commit()
        return self.view(sid,owner)

    def view(self, sid, owner):
        row=self.db.execute('SELECT * FROM view_sessions WHERE id=? AND owner=?', (sid,owner)).fetchone()
        return ViewProgress(self,dict(row)) if row else None

    def bookmark(self, owner, job):
        row = self.db.execute('SELECT * FROM bookmarks WHERE owner=? AND job=?', (owner,job)).fetchone()
        return dict(row) if row else None

    def processed_count(self, job):
        return len(self.files(job)) + self.db.execute('SELECT count(*) FROM file_aliases WHERE job=?', (job,)).fetchone()[0]

    def resume_entries(self, job):
        rows = self.files(job)
        canonical = {(r['source_chat'], r['source_message']): r for r in rows}
        aliases = []
        for alias in self.db.execute('SELECT * FROM file_aliases WHERE job=?', (job,)):
            row = canonical.get((alias['canonical_chat'], alias['canonical_message']))
            if row:
                aliases.append(dict(row, source_chat=alias['source_chat'], source_message=alias['source_message'],
                                    media_key=alias['media_key'], is_alias=True))
        return rows+aliases

    def set_analysis(self, job, source, analysis):
        import json
        self.db.execute('UPDATE files SET content_sha256=?,stream_sha256=?,quality_json=? '
                        'WHERE job=? AND source_chat=? AND source_message=?',
            (analysis['content_sha256'], analysis['stream_sha256'], json.dumps(analysis['quality']), job, *source))
        self.db.commit()

    def duplicate(self, job, source, analysis):
        row = self.db.execute('SELECT * FROM files WHERE job=? AND NOT (source_chat=? AND source_message=?) '
            "AND ((content_sha256<>'' AND content_sha256=?) OR (stream_sha256<>'' AND stream_sha256=?)) LIMIT 1",
            (job, *source, analysis['content_sha256'], analysis['stream_sha256'])).fetchone()
        return dict(row) if row else None

    def merge_alias(self, job, source, canonical, source_media_key):
        if source == canonical:
            return
        if not self.db.execute('SELECT 1 FROM files WHERE job=? AND source_chat=? AND source_message=?',
                               (job, *canonical)).fetchone():
            raise ValueError('Canonical file must already be confirmed')
        with self.db:
            self.db.execute('UPDATE file_aliases SET canonical_chat=?,canonical_message=? '
                            'WHERE job=? AND canonical_chat=? AND canonical_message=?', (*canonical, job, *source))
            self.db.execute('INSERT OR REPLACE INTO file_aliases VALUES(?,?,?,?,?,?)',
                            (job, *source, source_media_key, *canonical))
            self.db.execute('DELETE FROM files WHERE job=? AND source_chat=? AND source_message=?', (job, *source))

    def request_audit(self, job):
        self.db.execute("UPDATE jobs SET audit_requested=1,audit_public=(state='complete') WHERE id=?", (job,))
        self.db.commit()

    def finish_audit(self, job):
        self.db.execute('UPDATE jobs SET audit_requested=0,audit_public=0 WHERE id=?', (job,))
        self.db.commit()

    def list_jobs(self, owner, limit=20):
        return [dict(x) for x in self.db.execute(
            'SELECT * FROM jobs WHERE owner=? ORDER BY rowid DESC LIMIT ?', (owner, limit))]

    def set_preview(self, job, source, file_id):
        self.db.execute('UPDATE files SET preview_file_id=? '
                        'WHERE job=? AND source_chat=? AND source_message=?',
                        (file_id, job, *source))
        self.db.commit()

    def set_media_key(self, job, source, key):
        self.db.execute('UPDATE files SET media_key=? WHERE job=? AND source_chat=? AND source_message=?',
                        (key, job, *source))
        self.db.commit()

    def set_title(self, job, title):
        self.db.execute('UPDATE jobs SET title=? WHERE id=?', (title, job))
        self.db.commit()

    def catalog(self):
        return [dict(x) for x in self.db.execute('''
            SELECT jobs.*, count(files.file_id) AS file_count FROM jobs
            JOIN files ON files.job=jobs.id WHERE (jobs.state='complete' OR jobs.audit_public=1)
            GROUP BY jobs.id ORDER BY jobs.rowid DESC
        ''')]

    def resource_stats(self):
        row=self.db.execute('''SELECT count(DISTINCT j.id) AS folders,count(*) AS media,
            coalesce(sum(coalesce(e.kind,f.kind)='photo'),0) AS photos,
            coalesce(sum(coalesce(e.kind,f.kind)='video'),0) AS videos
            FROM jobs j JOIN files f ON f.job=j.id LEFT JOIN source_entries e
              ON e.job=f.job AND e.source_chat=f.source_chat AND e.source_message=f.source_message
            WHERE j.state='complete' OR j.audit_public=1''').fetchone()
        # Match folder details: media entries plus one information message
        # per folder, excluding navigation and repeated deliveries.
        return dict(folders=row['folders'],messages=row['media']+row['folders'],
                    photos=row['photos'],videos=row['videos'])

    def search_catalog(self, query, uid, owned=False):
        return [dict(x) for x in self.db.execute('''
            SELECT jobs.*, count(files.file_id) AS file_count FROM jobs
            JOIN files ON files.job=jobs.id
            WHERE instr(lower(jobs.title), lower(?)) > 0
              AND (jobs.state='complete' OR jobs.audit_public=1 OR (? AND jobs.owner=?))
            GROUP BY jobs.id ORDER BY jobs.rowid DESC
        ''', (query, int(owned), uid))]

    def add_file(self, job, source, message, file_id, kind, caption=None, source_media_key='', has_thumbnail=False, replace=False):
        if replace:
            self.db.execute('''UPDATE files SET chat=?,message=?,file_id=?,kind=?,caption=?,
                media_key=?,has_thumbnail=? WHERE job=? AND source_chat=? AND source_message=?''',
                (message.chat_id, message.message_id, file_id, kind,
                 (getattr(message, 'caption', None) or '') if caption is None else caption,
                 source_media_key, int(has_thumbnail), job, *source))
            self.db.commit()
            return
        self.db.execute('''INSERT OR IGNORE INTO files
            (job,source_chat,source_message,chat,message,file_id,kind,caption,media_key,has_thumbnail)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (job, *source, message.chat_id, message.message_id, file_id, kind,
             (getattr(message, 'caption', None) or '') if caption is None else caption,
             source_media_key, int(has_thumbnail)))
        self.db.commit()
