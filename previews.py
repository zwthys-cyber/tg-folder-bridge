"""Small JPEG display copy for HEIC originals; original bytes remain unchanged."""
import io
import asyncio
import shutil
import warnings
from pathlib import Path
from media_spool import LimitedWriter
from media_spool import InsufficientDiskError


class PreviewUnavailableError(RuntimeError):
    pass


def make_jpeg_preview(path, spool):
    path = Path(path)
    if path.suffix.lower() not in {'.heic', '.heif'}:
        return None
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener
    register_heif_opener()
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as source:
            if source.width * source.height > 40_000_000:
                raise PreviewUnavailableError('Image too large to preview')
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1600, 1600))
            image = image.convert('RGB')
            data = io.BytesIO()
            image.save(data, 'JPEG', quality=82, optimize=True)
    encoded = data.getvalue()
    if len(encoded) > 2 * 1024**2:
        raise PreviewUnavailableError('Preview too large')
    target = path.parent / 'preview.jpg'
    target.touch(mode=0o600, exist_ok=False)
    with LimitedWriter(target.open('wb', buffering=0), spool.root,
                       2 * 1024**2, spool.reserve_bytes) as output:
        output.write(encoded)
    return target


async def document_thumbnail(path, spool, preview=None):
    """Telegram document thumbnail: JPEG, <=320 px and <200 KiB."""
    from PIL import Image, ImageOps
    path = Path(path)
    target = path.parent / 'thumbnail.jpg'
    if path.suffix.lower() in {'.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi'}:
        if shutil.disk_usage(spool.root).free < spool.reserve_bytes + 200 * 1024:
            raise InsufficientDiskError('Not enough space for thumbnail')
        from imageio_ffmpeg import get_ffmpeg_exe
        executable = shutil.which('ffmpeg') or get_ffmpeg_exe()
        process = await asyncio.create_subprocess_exec(
            executable, '-nostdin', '-v', 'error', '-i', str(path),
            '-frames:v', '1', '-vf', 'scale=320:320:force_original_aspect_ratio=decrease',
            '-an', '-q:v', '6', '-fs', '190000', '-y', str(target),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode or not target.is_file():
            return None
        if target.stat().st_size >= 200 * 1024:
            target.unlink()
            return None
        with Image.open(target) as image:
            image.verify()
        target.chmod(0o600)
        return target
    source = preview or path
    if not preview and path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp'}:
        return None
    with Image.open(source) as source_image:
        if source_image.width * source_image.height > 40_000_000:
            return None
        image = ImageOps.exif_transpose(source_image)
        image.thumbnail((320, 320))
        data = io.BytesIO()
        image.convert('RGB').save(data, 'JPEG', quality=75)
    target.touch(mode=0o600, exist_ok=False)
    with LimitedWriter(target.open('wb', buffering=0), spool.root,
                       200 * 1024 - 1, spool.reserve_bytes) as output:
        output.write(data.getvalue())
    return target
