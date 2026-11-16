import tempfile
import unittest
from pathlib import Path
from configure_transfer import configure


class Config(unittest.TestCase):
    def test_update_preserves_credentials_removes_duplicate_keys_and_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('API_HASH=TEST_SECRET\nOWNER_IDS=7\n'
                            'TRANSFER_MODE=forward\nexport TRANSFER_MODE=forward\n'
                            'JOB_TIMEOUT_SECONDS=3600\n')
            configure(path)
            text = path.read_text()
            self.assertIn('API_HASH=TEST_SECRET\n', text)
            self.assertIn('OWNER_IDS=7\n', text)
            self.assertEqual(text.count('TRANSFER_MODE='), 1)
            self.assertIn('TRANSFER_MODE=download_upload\n', text)
            self.assertIn('JOB_TIMEOUT_SECONDS=86400\n', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            configure(path)
            self.assertEqual(path.read_text(), text)

    def test_missing_config_is_not_silently_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            with self.assertRaises(FileNotFoundError):
                configure(path)
            self.assertFalse(path.exists())
