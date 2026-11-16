import json
import tempfile
import unittest
from pathlib import Path
from core import Store
from transfer_metrics import TransferMeter


class Metrics(unittest.TestCase):
    def test_rates_are_acknowledged_bytes_and_percentiles_are_bounded(self):
        now = [0]
        stats = {}
        meter = TransferMeter(stats, 100*1024**2, 2, 'dedicated', clock=lambda: now[0])
        for n in range(1, 1001):
            now[0] = n/10
            meter.update(n*1024**2, .2)
        self.assertAlmostEqual(stats['average_mib'], 10)
        self.assertAlmostEqual(stats['recent_mib'], 10)
        self.assertEqual(stats['connections'], 2)
        self.assertEqual(stats['p95_ms'], 200)
        self.assertLessEqual(len(meter.latencies), 512)
        self.assertLessEqual(len(meter.recent), 128)

    def test_records_survive_restart_are_bounded_and_exclude_unknown_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'index.sqlite3'
            store = Store(path)
            for n in range(505):
                store.record_transfer('job', '上传', dict(bytes=n, token='secret', filename='private'))
            rows = store.transfer_history(600)
            self.assertEqual(len(rows), 500)
            self.assertNotIn('secret', json.dumps(rows))
            store.db.close()
            store = Store(path)
            self.assertEqual(store.transfer_history(1)[0]['payload']['bytes'], 504)
            store.db.close()
