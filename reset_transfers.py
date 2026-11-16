"""Clear transfer indexes only. Never connects to Telegram or opens resources.db."""
import argparse
import sqlite3
import subprocess
import uuid
from pathlib import Path
from media_spool import MediaSpool


def reset(root, apply=False):
    root = Path(root).resolve()
    database = root / 'index.sqlite3'
    mode = 'rw' if apply else 'ro'
    db = sqlite3.connect(database.as_uri() + '?mode=' + mode, uri=True)
    spool = None
    try:
        jobs = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        files = db.execute('SELECT count(*) FROM files').fetchone()[0]
        result = dict(jobs=jobs, files=files, applied=False, backup=None)
        if not apply:
            return result
        spool = MediaSpool(root / 'spool', 4 * 1024**3, 10 * 1024**3)
        backup_dir = root / 'reset-backups'
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup_path = backup_dir / ('index-' + uuid.uuid4().hex + '.sqlite3')
        backup_path.touch(mode=0o600, exist_ok=False)
        with sqlite3.connect(backup_path) as backup:
            db.backup(backup)
        with db:
            for table in ('view_receipts', 'view_sessions', 'favorites', 'source_entries', 'file_aliases', 'local_send_intents', 'transfer_metrics'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    db.execute('DELETE FROM '+table)
            db.execute('DELETE FROM files')
            db.execute('DELETE FROM jobs')
        result.update(applied=True, backup=str(backup_path))
        return result
    finally:
        if spool:
            spool.close()
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=str(Path(__file__).resolve().parent / 'data'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        active = subprocess.run(['systemctl', 'is-active', '--quiet', 'sougabot'], check=False)
        if active.returncode == 0:
            parser.error('请先执行 systemctl stop sougabot，再清空转存索引。')
    result = reset(args.data_dir, apply=args.apply)
    print(f"转存任务：{result['jobs']}；原文件索引：{result['files']}")
    print('已清空转存索引。' if result['applied'] else '仅预览；添加 --apply 才执行清理。')
    if result['backup']:
        print('旧索引备份：' + result['backup'])
    print('Telegram 聊天旧媒体、登录会话和原 resources.db 均保留。')
