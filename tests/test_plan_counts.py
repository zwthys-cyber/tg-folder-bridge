import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from core import Store


class PlanCounts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'index.sqlite3')
        self.store.create('job',7,'link');self.store.update('job','partial',2)
        for n in (1,2):
            self.store.add_file('job',(101,n),NS(chat_id=7,message_id=n),str(n),'document',
                                source_media_key=f'document:{n}')

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def plan(self,ids):
        self.store.save_source_plan('job',[NS(chat_id=101,id=i+10,document=NS(id=n),photo=None)
                                          for i,n in enumerate(ids)])

    def test_replayed_source_ids_and_historical_alias_do_not_inflate_count(self):
        self.store.merge_alias('job',(101,3),(101,1),'document:1')
        self.plan([1,2])
        self.assertEqual(self.store.processed_count('job'),2)
        self.assertEqual(len(self.store.files('job')),2)

    def test_duplicate_entries_in_current_plan_each_count_as_processed(self):
        self.plan([1,1])
        self.assertEqual(self.store.processed_count('job'),2)

    def test_extra_old_files_do_not_hide_a_missing_current_media(self):
        self.plan([1,3])
        self.assertEqual(self.store.processed_count('job'),1)

    def test_plan_media_identifiers_survive_restart(self):
        self.plan([1,2]);self.store.db.close()
        self.store=Store(Path(self.tmp.name)/'index.sqlite3')
        self.assertEqual(self.store.processed_count('job'),2)
