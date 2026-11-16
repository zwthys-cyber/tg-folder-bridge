import tempfile
import unittest
from pathlib import Path
from transfer_settings import load, save, validate


class Settings(unittest.TestCase):
    def test_persist_both_limits_privately_and_restore_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'performance.json'
            self.assertEqual(load(path, 4, 3), (4, 3))
            save(path, 32, 32)
            self.assertEqual(load(path, 4, 3), (32, 32))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_reject_invalid_limits(self):
        for value in (0, 33, -1, True, 3.5, '32'):
            with self.assertRaises(ValueError):
                validate(value, 32)
