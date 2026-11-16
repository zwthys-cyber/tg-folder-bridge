import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from core import Store
from reset_transfers import reset


class Reset(unittest.TestCase):
    def test_only_transfer_indexes_and_temporary_media_are_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / 'index.sqlite3')
            store.create('job', 7, 'link')
            store.add_file('job', (1, 2), NS(chat_id=7, message_id=9), 'file-id', 'document')
            store.db.close()
            resources = root / 'resources.db'
            resources.write_bytes(b'ORIGINAL SEARCH DATABASE')
            session = root / 'relay.session'
            session.write_bytes(b'LOGIN SESSION')
            stale = root / 'spool' / 'transfer-stale'
            stale.mkdir(parents=True)
            (stale / 'media.mov').write_bytes(b'old temp')
            plan = reset(root)
            self.assertFalse(plan['applied'])
            self.assertEqual((plan['jobs'], plan['files']), (1, 1))
            self.assertTrue(stale.exists())
            result = reset(root, apply=True)
            self.assertTrue(result['applied'])
            with sqlite3.connect(root / 'index.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 0)
            with sqlite3.connect(result['backup']) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
            self.assertFalse(stale.exists())
            self.assertEqual(resources.read_bytes(), b'ORIGINAL SEARCH DATABASE')
            self.assertEqual(session.read_bytes(), b'LOGIN SESSION')
