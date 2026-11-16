"""Keep polling, metadata and file delivery on one validated Bot API endpoint."""
import os

CLOUD = 'https://api.telegram.org'
LOCAL = 'http://127.0.0.1:8081'


def endpoint(value=None):
    value = (value if value is not None else os.getenv('BOT_API_BASE', CLOUD)).rstrip('/')
    if value not in {CLOUD, LOCAL}:
        raise ValueError('BOT_API_BASE must be the official cloud or local loopback endpoint')
    return value


def configure(builder):
    base = endpoint()
    if base == LOCAL:
        return builder.base_url(base+'/bot').base_file_url(base+'/file/bot').local_mode(True)
    return builder
