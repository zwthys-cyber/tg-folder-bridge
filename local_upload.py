"""Official local Bot API upload: URI for media, bytes only for small thumbnail."""
import os
import asyncio
import grp
import re
import tempfile
import time
from pathlib import Path
from telegram import InputFile
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter
from bot_api_config import LOCAL

MAX_LOCAL_BYTES = 2_000_000_000


class DeliveryUnknownError(RuntimeError):
    pass


class LocalBotRateLimitError(RuntimeError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__('Local Bot API rate limited')


class LocalBotUploadRejected(BadRequest):
    def __init__(self, method, reason):
        self.method = method
        self.reason = reason
        self.code = 400
        super().__init__(f'{method}: {reason}')


def rejection_reason(error):
    # Only persist fixed labels. Telegram descriptions can contain paths or URLs.
    description = str(error).casefold()
    for reason, patterns in (
        ('thumbnail', ('thumbnail', 'thumb_invalid')),
        ('video_format', ('video_file_invalid', 'video_content_type_invalid',
                          'video_mime_type_invalid', 'invalid video', 'video is not',
                          'video_content_invalid', 'video_invalid')),
        ('media_dimensions', ('video dimensions', 'photo_invalid_dimensions',
                              'media_invalid_dimensions')),
        ('caption', ('caption_too_long', 'caption is too long', 'caption entities')),
        ('chat_access', ('chat not found', 'chat write forbidden', 'chat_write_forbidden',
                         'not enough rights', 'have no rights', 'user is deactivated')),
        ('file_size', ('file is too big', 'file_too_big', 'file_size_invalid')),
        ('file_parts', ('file_part', 'file parts')),
        ('local_file', ('realpath failed', 'failed to open', 'failed to stat',
                        'file not found', 'no such file', 'permission denied')),
        ('file_reference', ('wrong file identifier', 'invalid file http url',
                            'failed to get http url', 'file_reference')),
        ('media_invalid', ('media_invalid', 'media invalid', 'wrong type of the web page content')),
        ('message_rejected', ('message failed to be sent', 'failed to send message')),
    ):
        if any(pattern in description for pattern in patterns):
            return reason
    return 'unclassified'


def eligible(size):
    return 0 < size <= MAX_LOCAL_BYTES


def safe_filename(name):
    name = re.sub(r'[\\/\x00-\x1f\x7f]', '_', name or '')
    name = name.encode('utf-8')[:200].decode('utf-8', errors='ignore').strip('. ')
    return name or 'media.bin'


async def send_local(bot, store, job, source, chat, path, marker, filename,
                     photo=False, thumbnail=None, stats=None, save=None, caption=None, video=None):
    path = Path(path)
    size = path.stat().st_size
    if not eligible(size):
        raise ValueError('File exceeds local Bot API routing limit')
    if not bot.local_mode or not str(bot.base_url).startswith(LOCAL+'/bot'):
        raise ValueError('Local uploads require the local Bot API endpoint for all bot calls')
    stats = stats if stats is not None else {}
    stats.update(transport='local_bot_api', bytes=0, total=size, seconds=0,
                 workers=0, connections=0, retries=0)
    intent = store.begin_local_send(job, source, marker)
    started = time.monotonic()
    submitted = False
    sending = False
    method = 'sendPhoto' if photo else 'sendVideo' if video else 'sendDocument'
    try:
        handoff = os.getenv('LOCAL_UPLOAD_DIR')
        group = os.getenv('LOCAL_UPLOAD_GROUP')
        root = Path(handoff) if handoff else path.parent
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Local upload directory must be a real directory')
        if handoff and root.stat().st_mode & 0o007:
            raise ValueError('Local upload directory must not be accessible to other users')
        with tempfile.TemporaryDirectory(prefix='upload-', dir=root) as directory:
            if group:
                gid = grp.getgrnam(group).gr_gid
                os.chown(directory, -1, gid)
                os.chmod(directory, 0o750)
            named = Path(directory)/safe_filename(filename)
            os.link(path, named)  # Preserve filename without copying another large file.
            if group:
                os.chown(named, -1, gid)
                os.chmod(named, 0o640)
            kwargs = dict(chat_id=chat, caption=(marker if caption is None else caption)[:1024], parse_mode=None,
                          read_timeout=3600, write_timeout=60, connect_timeout=10, pool_timeout=30)
            if photo:
                submitted = sending = True
                reply = await bot.send_photo(photo=named.as_uri(), **kwargs)
                kind, media = 'photo', reply.photo[-1] if reply.photo else None
            else:
                if thumbnail:
                    data = Path(thumbnail).read_bytes()
                    if len(data) >= 200_000:
                        raise ValueError('Thumbnail too large')
                    kwargs['thumbnail'] = InputFile(data, filename='thumbnail.jpg')
                submitted = sending = True
                if video:
                    reply = await bot.send_video(video=named.as_uri(), **video, **kwargs)
                    kind, media = 'video', reply.video
                    if media is None and getattr(reply, 'document', None):
                        kind, media = 'document', reply.document
                else:
                    reply = await bot.send_document(document=named.as_uri(),
                        disable_content_type_detection=True, **kwargs)
                    kind, media = 'document', reply.document
            sending = False
            if (reply.chat_id != chat or not media or not media.file_id
                    or (not photo and media.file_size != size)):
                raise DeliveryUnknownError('Local Bot API receipt could not be validated')
            # File index + confirmed intent commit together. On DB failure the
            # durable pending intent prevents an automatic duplicate send.
            store.confirm_local_send(intent, lambda: save(reply, kind, media))
            stats.update(bytes=size, seconds=time.monotonic()-started)
            stats['average_mib'] = size/max(stats['seconds'], .001)/1024**2
            return reply
    except (RetryAfter, BadRequest, Forbidden, InvalidToken) as exc:
        if sending:
            store.reject_local_send(intent)
            if isinstance(exc, RetryAfter):
                delay = exc.retry_after
                raise LocalBotRateLimitError(int(delay.total_seconds() if hasattr(delay, 'total_seconds') else delay)) from None
            if isinstance(exc, BadRequest):
                raise LocalBotUploadRejected(method, rejection_reason(exc)) from None
            raise
        if not submitted:
            store.reject_local_send(intent)
            raise
        raise DeliveryUnknownError('Check chat before authorizing a retry') from None
    except BaseException as exc:
        # Network timeout/cancellation after submission has an unknown outcome.
        # Never silently switch channels and send the same file again.
        if not submitted:
            store.reject_local_send(intent)
            raise
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise DeliveryUnknownError('Check chat before authorizing a retry') from None
    finally:
        stats['seconds'] = time.monotonic()-started
