"""Pure parsing and persistent metadata. No media bytes are written here."""
import re
import sqlite3
from urllib.parse import parse_qs, urlparse


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


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner INTEGER, link TEXT,
                state TEXT, expected INTEGER, detail TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS files (
                job TEXT, source_chat INTEGER, source_message INTEGER,
                chat INTEGER, message INTEGER, file_id TEXT, kind TEXT,
                PRIMARY KEY(job, source_chat, source_message)
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
        job_columns = {x['name'] for x in self.db.execute('PRAGMA table_info(jobs)')}
        if 'title' not in job_columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN title TEXT DEFAULT ''")
        self.db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running','cooldown')")
        self.db.commit()

    def create(self, job, owner, link):
        self.db.execute('INSERT INTO jobs(id,owner,link,state) VALUES(?,?,?,?)',
                        (job, owner, link, 'queued'))
        self.db.commit()

    def update(self, job, state, expected=None, detail=''):
        self.db.execute('UPDATE jobs SET state=?,expected=?,detail=? WHERE id=?',
                        (state, expected, detail, job))
        self.db.commit()

    def get(self, job):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        return dict(row) if row else None

    def files(self, job):
        return [dict(x) for x in self.db.execute(
            'SELECT * FROM files WHERE job=? ORDER BY rowid', (job,))]

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
            JOIN files ON files.job=jobs.id WHERE jobs.state='complete'
            GROUP BY jobs.id ORDER BY jobs.rowid DESC
        ''')]

    def search_catalog(self, query, uid, owned=False):
        return [dict(x) for x in self.db.execute('''
            SELECT jobs.*, count(files.file_id) AS file_count FROM jobs
            JOIN files ON files.job=jobs.id
            WHERE instr(lower(jobs.title), lower(?)) > 0
              AND (jobs.state='complete' OR (? AND jobs.owner=?))
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
