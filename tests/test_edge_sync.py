import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core import Store
from edge_sync import snapshot, fingerprint, EdgeClient


class EdgeExportTests(unittest.TestCase):
    def test_read_only_export_preserves_saved_media_and_stopped_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.sqlite3'
            store = Store(path)
            with store.db:
                for job, state in [('a'*16, 'complete'), ('b'*16, 'cancelled'), ('c'*16, 'running')]:
                    store.db.execute('INSERT INTO jobs(id,owner,link,state,title) VALUES(?,1,?,?,?)',
                                     (job, 'https://t.me/epan?start=s_test', state, '测试1051'))
                    store.db.execute('INSERT INTO files(job,source_chat,source_message,file_id,kind) VALUES(?,2,3,?,?)',
                                     (job, 'same-bot-original-id', 'video'))
                    store.db.execute('INSERT INTO source_entries VALUES(?,2,3,0,?,1024,?)',
                                     (job, 'IMG_0001.MOV', 'video'))
                store.db.execute('UPDATE jobs SET cover_file_id=? WHERE id=?', ('cached-photo-id', 'a'*16))
                store.db.execute('INSERT INTO favorites VALUES(1,?)', ('a'*16,))
                store.db.execute('INSERT INTO subscribers VALUES(1,1)')
            store.db.close()
            before = path.read_bytes()
            result = snapshot(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([x['id'] for x in result['folders']], ['a'*16])
            self.assertEqual(result['folders'][0]['items'][0]['file_id'], 'same-bot-original-id')
            self.assertEqual(result['folders'][0]['items'][0]['name'], 'IMG_0001.MOV')
            self.assertEqual(result['folders'][0]['cover'], 'cached-photo-id')
            self.assertEqual(result['subscribers'], [{'owner': 1, 'enabled': 1}])
            db = sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?", ('b'*16,)).fetchone()[0], 'cancelled')
            self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 3)
            db.close()
            self.assertNotIn('session', json.dumps(result))

    def test_preference_changes_do_not_overwrite_edge_authority(self):
        first = dict(folders=[{'id': 'x'}], favorites=[], subscribers=[])
        second = dict(first, favorites=[{'owner': 2, 'job': 'x'}])
        self.assertEqual(fingerprint(first), fingerprint(second))

    def test_private_endpoint_must_use_https(self):
        with self.assertRaises(ValueError):
            EdgeClient('http://example.com', 'secret')

    def test_importer_rejects_hybrid_and_local_polling_configuration(self):
        from edge_importer import validate
        with patch.dict(os.environ, {'EDGE_MODE': '1', 'BOT_API_BASE': 'http://127.0.0.1:8081', 'UPLOAD_BACKEND': 'hybrid'}):
            with self.assertRaises(ValueError):
                validate()
        with patch.dict(os.environ, {'EDGE_MODE': '1', 'BOT_API_BASE': 'https://api.telegram.org', 'UPLOAD_BACKEND': 'relay'}):
            validate()


if __name__ == '__main__':
    unittest.main()
