import tempfile
import unittest
from pathlib import Path
from PIL import Image
from pillow_heif import register_heif_opener
from media_spool import MediaSpool
from previews import make_jpeg_preview
from previews import document_thumbnail


class Previews(unittest.TestCase):
    def test_real_heic_gets_jpeg_preview_and_original_is_unchanged(self):
        register_heif_opener()
        with tempfile.TemporaryDirectory() as tmp:
            spool = MediaSpool(Path(tmp) / 'spool', 1024**3, 1)
            try:
                source = Path(tmp) / 'photo.heic'
                Image.new('RGB', (1800, 900), 'blue').save(source, format='HEIF')
                original = source.read_bytes()
                preview = make_jpeg_preview(source, spool)
                with Image.open(preview) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.size, (1600, 800))
                self.assertEqual(source.read_bytes(), original)
                self.assertLessEqual(preview.stat().st_size, 2 * 1024**2)
            finally:
                spool.close()

    def test_non_heic_does_not_create_a_preview(self):
        self.assertIsNone(make_jpeg_preview(Path('video.mp4'), None))


class Thumbnails(unittest.IsolatedAsyncioTestCase):
    async def test_document_image_thumbnail_is_small_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = MediaSpool(Path(tmp) / 'spool', 1024**3, 1)
            try:
                source = Path(tmp) / 'image.jpg'
                Image.new('RGB', (1800, 900), 'red').save(source)
                thumb = await document_thumbnail(source, spool)
                with Image.open(thumb) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.size, (320, 160))
                self.assertLess(thumb.stat().st_size, 200 * 1024)
            finally:
                spool.close()

    async def test_real_video_gets_a_document_thumbnail(self):
        import asyncio
        from imageio_ffmpeg import get_ffmpeg_exe
        with tempfile.TemporaryDirectory() as tmp:
            spool = MediaSpool(Path(tmp) / 'spool', 1024**3, 1)
            try:
                source = Path(tmp) / 'video.mp4'
                process = await asyncio.create_subprocess_exec(
                    get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=red:s=128x72:d=0.2', '-c:v', 'mpeg4', '-y', str(source),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                self.assertEqual(await process.wait(), 0)
                thumb = await document_thumbnail(source, spool)
                self.assertIsNotNone(thumb)
                with Image.open(thumb) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertLessEqual(max(image.size), 320)
            finally:
                spool.close()
