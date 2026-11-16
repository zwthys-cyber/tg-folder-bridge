import unittest
from adaptive_speed import AdaptiveSpeed


class Adaptive(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.tuner = AdaptiveSpeed(32, clock=lambda: self.now)
        self.bytes = self.parts = 0
        self.tuner.observe(0, 10**12, 0)

    def sample(self, rate=100):
        self.now += 8
        self.bytes += rate * 8
        self.parts += 64
        self.tuner.observe(self.bytes, 10**12, self.parts)

    def test_only_keep_a_probe_when_both_windows_show_gain(self):
        self.sample(); self.sample()
        self.assertEqual(self.tuner.current, 12)
        self.sample(140); self.sample(140)
        self.assertEqual(self.tuner.current, 16)
        # One burst cannot hide a subsequent stall.
        self.sample(300); self.sample(100)
        self.assertEqual(self.tuner.current, 12)
        self.assertIn('回退', self.tuner.note)
        self.sample(140); self.sample(140)
        self.assertEqual(self.tuner.current, 12)

    def test_new_file_discards_unverified_trial_but_keeps_verified_limit(self):
        self.sample(); self.sample()
        self.sample(150); self.sample(150)
        self.assertEqual(self.tuner.current, 16)
        self.tuner.begin()
        self.assertEqual(self.tuner.current, 12)
        self.assertIsNone(self.tuner.trial)

    def test_live_lower_ceiling_cannot_restore_old_trial(self):
        self.sample(); self.sample()
        self.tuner.configure(2)
        self.assertEqual(self.tuner.current, 2)
        self.assertIsNone(self.tuner.trial)

    def test_file_tail_does_not_trigger_an_experiment(self):
        self.tuner.observe(90, 100, 1000)
        self.now = 100
        self.tuner.observe(100, 100, 2000)
        self.assertEqual(self.tuner.current, 8)

    def test_independent_upload_and_download_and_hard_ceiling(self):
        other = AdaptiveSpeed(32)
        self.tuner.configure(10)
        self.tuner.observe(0, 10**12, 0)
        self.sample(); self.sample()
        self.assertEqual(self.tuner.current, 10)
        self.assertEqual(other.current, 8)
        self.sample(200); self.sample(200)
        self.assertEqual(self.tuner.current, 10)

    def test_mid_file_reconfigure_does_not_count_earlier_bytes(self):
        self.tuner.configure(32)
        self.tuner.observe(10**9, 10**12, 10000)
        self.now += 8
        self.tuner.observe(10**9 + 800, 10**12, 10064)
        self.assertEqual(self.tuner.samples, [100])

    def test_automatic_probes_never_reach_observed_429_range(self):
        rate = 100
        for _ in range(12):
            self.sample(rate); self.sample(rate)
            rate *= 2
            self.assertLessEqual(self.tuner.current, 16)
        self.assertEqual(self.tuner.current, 16)
        self.tuner.configure(32)
        self.assertLessEqual(self.tuner.ceiling, 16)
