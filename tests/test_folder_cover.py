import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from PIL import Image
from folder_cover import image_cover


class Cover(unittest.IsolatedAsyncioTestCase):
    async def test_first_photo_is_used_in_source_order_without_downloading(self):
        bot = NS(get_file=AsyncMock())
        items = [dict(kind='video',file_id='video'),dict(kind='photo',file_id='first'),dict(kind='photo',file_id='second')]
        self.assertEqual(await image_cover(bot,items),'first')
        bot.get_file.assert_not_awaited()

    async def test_document_image_becomes_jpeg_without_changing_original(self):
        stream=io.BytesIO();Image.new('RGB',(32,48),'blue').save(stream,'PNG');original=stream.getvalue()
        async def download(buffer,**kwargs):buffer.write(original)
        bot=NS(get_file=AsyncMock(return_value=NS(file_size=len(original),download_to_memory=AsyncMock(side_effect=download))))
        result=await image_cover(bot,[dict(kind='document',source_kind='photo',file_id='document',file_size=len(original))])
        with Image.open(io.BytesIO(result)) as im:self.assertEqual(im.format,'JPEG')
        self.assertEqual(stream.getvalue(),original)

    async def test_no_image_and_oversized_document_do_not_download_video(self):
        bot=NS(get_file=AsyncMock())
        self.assertIsNone(await image_cover(bot,[dict(kind='video',file_id='video')]))
        with self.assertRaises(ValueError):
            await image_cover(bot,[dict(kind='document',source_kind='photo',file_id='big',file_size=21*1024**2)])
        bot.get_file.assert_not_awaited()
