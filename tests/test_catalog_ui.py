import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from catalog_ui import catalog_page, send_file_page


class Catalog(unittest.IsolatedAsyncioTestCase):
    async def test_list_has_names_counts_number_buttons_and_pagination(self):
        rows = [dict(id=f'job{i}', title=f'文件夹{i}', file_count=i+1) for i in range(12)]
        text, keyboard = catalog_page(rows)
        self.assertIn('共 12 个文件夹', text)
        self.assertIn('文件夹0（1 个文件）', text)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'bridge_open:job0:1')
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, 'bridge_catalog:2')
        text, keyboard = catalog_page(rows, 2)
        self.assertIn('11. 📁 文件夹10', text)
        self.assertEqual(keyboard.inline_keyboard[0][0].text, '11')

    async def test_files_use_native_document_album_and_next_group_button(self):
        bot = NS(send_message=AsyncMock(), send_media_group=AsyncMock())
        items = [dict(kind='document', file_id=f'file{i}', caption='') for i in range(12)]
        send_one = AsyncMock()
        await send_file_page(bot, 7, dict(id='job', title='示例'), items, 1, send_one)
        group = bot.send_media_group.call_args.kwargs['media']
        self.assertEqual(len(group), 10)
        self.assertEqual([x.media for x in group], [f'file{i}' for i in range(10)])
        self.assertEqual(group[0].type, 'document')
        send_one.assert_not_awaited()
        keyboard = bot.send_message.call_args.kwargs['reply_markup']
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'bridge_open:job:2')

    async def test_single_remaining_file_does_not_use_invalid_one_item_album(self):
        bot = NS(send_message=AsyncMock(), send_media_group=AsyncMock())
        send_one = AsyncMock()
        item = dict(kind='document', file_id='file')
        await send_file_page(bot, 7, dict(id='job', title='示例'), [item], 1, send_one)
        send_one.assert_awaited_once_with(bot, 7, item, use_preview=False)
        bot.send_media_group.assert_not_awaited()
