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
        self.db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','running')")
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

    def add_file(self, job, source, message, file_id, kind, caption=None):
        self.db.execute('''INSERT OR IGNORE INTO files
            (job,source_chat,source_message,chat,message,file_id,kind,caption)
            VALUES(?,?,?,?,?,?,?,?)''',
            (job, *source, message.chat_id, message.message_id, file_id, kind,
             (getattr(message, 'caption', None) or '') if caption is None else caption))
        self.db.commit()
