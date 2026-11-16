"""Measure acknowledged bytes before retaining a higher concurrency limit."""
import time


class AdaptiveSpeed:
    window = 8.0
    minimum_parts = 32

    def __init__(self, ceiling, clock=time.monotonic):
        self.clock = clock
        self.current = min(8, ceiling)
        self.ceiling = ceiling
        self.hold_until = 0
        self.begin()

    def begin(self):
        # An unfinished experiment must not become the next file's baseline.
        if getattr(self, 'trial', None):
            self.current = self.trial[0]
        self.trial = None
        self.baseline = None
        self.samples = []
        self.at = None
        self.bytes = self.parts = 0
        self.note = '采样中'

    def configure(self, ceiling):
        if self.trial:
            self.current = self.trial[0]
        self.trial = None
        self.ceiling = ceiling
        self.current = min(self.current, ceiling)
        self.begin()

    def observe(self, current, total, parts):
        now = self.clock()
        if self.at is None:
            self.at, self.bytes, self.parts = now, current, parts
            return
        # Ignore file tails: fewer outstanding requests would bias the trial.
        if total and current >= total * .85:
            return
        elapsed = now - self.at
        if elapsed < self.window or parts - self.parts < self.minimum_parts:
            return
        rate = (current - self.bytes) / elapsed
        self.at, self.bytes, self.parts = now, current, parts
        if now < self.hold_until:
            self.note = '保持已验证并发'
            return
        self.samples.append(rate)
        if len(self.samples) < 2:
            return
        measured = sum(self.samples) / len(self.samples)
        slowest = min(self.samples)
        self.samples.clear()
        if self.trial:
            previous, baseline = self.trial
            # Both windows must improve, rather than a single completion burst.
            if measured >= baseline * 1.10 and slowest >= baseline * 1.05:
                self.baseline = measured
                self.note = '加速有效'
            else:
                self.current = previous
                self.trial = None
                self.baseline = None
                self.hold_until = now + 60
                self.note = '收益不足，已回退'
                return
            self.trial = None
        else:
            self.baseline = measured
        if self.current < self.ceiling:
            self.trial = (self.current, self.baseline)
            self.current = min(self.ceiling, self.current + 4)
            self.note = '试探更高并发'
        else:
            self.note = '已达设置上限'
