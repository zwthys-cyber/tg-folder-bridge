import tempfile
import unittest
from pathlib import Path
from PIL import Image
from pillow_heif import register_heif_opener
from media_spool import MediaSpool
from previews import make_jpeg_preview


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
