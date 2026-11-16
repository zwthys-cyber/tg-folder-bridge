"""Rate-limit classification and persisted cooldown; never logs raw payloads."""
import json
import os
import tempfile
import time
from pathlib import Path


def rate_wait(error, attempt):
    name = type(error).__name__
    if name == 'InvalidBufferError' and getattr(error, 'code', None) == 429:
        return min(120 * 2**attempt, 900)
    if name in {'FloodWaitError', 'FloodPremiumWaitError'}:
        return max(1, int(getattr(error, 'seconds', 0)))
    return None


def error_detail(error, phase=''):
    text = type(error).__name__
    code = getattr(error, 'code', None)
    if type(code) is int:
        text += f' (code={code})'
    seconds = getattr(error, 'seconds', None)
    if type(seconds) is int:
        text += f'；服务端等待 {seconds} 秒'
    if phase:
        text += f'；阶段：{phase}'
    return text


class Cooldown:
    def __init__(self, path):
        self.path = Path(path)
        self.until = 0
        self.reason = ''
        if self.path.exists():
            value = json.loads(self.path.read_text())
            self.until, self.reason = float(value['until']), str(value['reason'])

    def remaining(self):
        return max(0, self.until-time.time())

    def defer(self, seconds, reason):
        self.until = max(self.until, time.time()+seconds)
        self.reason = reason
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as output:
                temporary = Path(output.name)
                os.fchmod(output.fileno(), 0o600)
                json.dump(dict(until=self.until, reason=self.reason), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
