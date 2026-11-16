import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from telethon.errors import FloodWaitError, InvalidBufferError
from transport_recovery import Cooldown, error_detail, rate_wait


def transport_error(code):
    return InvalidBufferError((-code).to_bytes(4, 'little', signed=True))


class Recovery(unittest.TestCase):
    def test_transport_429_uses_exponential_cooldown_but_404_is_not_retried(self):
        self.assertEqual(rate_wait(transport_error(429), 0), 120)
        self.assertEqual(rate_wait(transport_error(429), 1), 240)
        self.assertIsNone(rate_wait(transport_error(404), 0))

    def test_native_flood_wait_is_not_shortened(self):
        self.assertEqual(rate_wait(FloodWaitError(None, capture=600), 0), 600)
        self.assertEqual(rate_wait(FloodWaitError(None, capture=17), 0), 17)

    def test_cooldown_survives_restart_and_is_not_reduced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rate-limit.json'
            with patch('transport_recovery.time.time', return_value=100):
                guard = Cooldown(path)
                guard.defer(120, 'InvalidBufferError (code=429)')
            with patch('transport_recovery.time.time', return_value=150):
                recovered = Cooldown(path)
                self.assertEqual(recovered.remaining(), 70)
                recovered.defer(5, 'ConnectionError')
                self.assertEqual(recovered.remaining(), 70)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_error_details_do_not_disclose_raw_transport_payload(self):
        text = error_detail(InvalidBufferError(b'PRIVATE_PAYLOAD'), '下载')
        self.assertIn('InvalidBufferError', text)
        self.assertNotIn('PRIVATE_PAYLOAD', text)
        self.assertIn('code=429', error_detail(transport_error(429)))
