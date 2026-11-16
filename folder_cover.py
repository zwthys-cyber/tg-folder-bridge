"""Small display cover from the first image; originals are never modified."""
import asyncio
import io
import warnings
from pathlib import Path


def jpeg_cover(data):
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener
    register_heif_opener()
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 40_000_000:
                raise ValueError('Cover exceeds pixel budget')
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.convert('RGB').save(output, 'JPEG', quality=82, optimize=True)
    result = output.getvalue()
    if len(result) > 2*1024**2:
        raise ValueError('Cover exceeds byte budget')
    return result


async def image_cover(bot, items):
    for item in items:
        suffix = Path(item.get('source_name') or '').suffix.lower()
        is_image = (item.get('source_kind') == 'photo' or item['kind'] == 'photo'
                    or suffix in {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp'})
        if not is_image:
            continue
        if item.get('preview_file_id'):
            return item['preview_file_id']
        if item['kind'] == 'photo':
            return item['file_id']
        # Saved images can be documents. A document file_id cannot be used
        # with sendPhoto, so upload a bounded JPEG display copy once.
        if item.get('file_size') is None or item['file_size'] > 20*1024**2:
            raise ValueError('Cover source exceeds download budget')
        file = await bot.get_file(item['file_id'], read_timeout=60)
        if file.file_size is None or file.file_size > 20*1024**2:
            raise ValueError('Cover source exceeds download budget')
        buffer = io.BytesIO()
        await file.download_to_memory(buffer, read_timeout=60)
        if buffer.tell() > 20*1024**2:
            raise ValueError('Cover source exceeds download budget')
        return await asyncio.to_thread(jpeg_cover, buffer.getvalue())
    return None
