"""Small JPEG display copy for HEIC originals; original bytes remain unchanged."""
import io
import warnings
from pathlib import Path
from media_spool import LimitedWriter


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
