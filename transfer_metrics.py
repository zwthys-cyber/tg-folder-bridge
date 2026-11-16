"""Bounded per-file telemetry, containing no paths, captions, or credentials."""
import time
from collections import deque


class TransferMeter:
    def __init__(self, stats, total, connections=1, transport='control', clock=time.monotonic):
        self.stats, self.total, self.clock = stats, total, clock
        self.started = clock()
        self.current = 0
        self.latencies = deque(maxlen=512)
        self.recent = deque(maxlen=128)
        stats.update(bytes=0, total=total, seconds=0.0, connections=connections,
                     transport=transport, recent_mib=0.0)

    def update(self, current, latency=None):
        now = self.clock()
        self.current = current
        if latency is not None:
            self.latencies.append(latency)
        self.recent.append((now, current))
        while len(self.recent) > 2 and self.recent[1][0] < now-10:
            self.recent.popleft()
        seconds = max(0, now-self.started)
        self.stats.update(bytes=current, seconds=seconds,
                          average_mib=current/max(seconds, .001)/1024**2)
        if len(self.recent) > 1:
            at, previous = self.recent[0]
            self.stats['recent_seconds'] = now-at
            self.stats['recent_mib'] = (current-previous)/max(now-at, .001)/1024**2
        if self.latencies:
            values = sorted(self.latencies)
            self.stats.update(p50_ms=1000*values[(len(values)-1)//2],
                              p95_ms=1000*values[int((len(values)-1)*.95)])

    def finish(self):
        self.update(self.current)
