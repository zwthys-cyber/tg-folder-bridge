import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from catalog_ui import catalog_page, send_file_page, folder_details, send_folder_card


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
        sequence = Mock()
        sequence.attach_mock(bot.send_message, 'text')
        sequence.attach_mock(bot.send_media_group, 'album')
        items = [dict(kind='document', file_id=f'file{i}', caption='') for i in range(12)]
        send_one = AsyncMock()
        await send_file_page(bot, 7, dict(id='job', title='示例'), items, 1, send_one)
        group = bot.send_media_group.call_args.kwargs['media']
        self.assertEqual(len(group), 10)
        self.assertEqual([x.media for x in group], [f'file{i}' for i in range(10)])
        self.assertEqual(group[0].type, 'document')
        send_one.assert_not_awaited()
        keyboard = bot.send_message.call_args.kwargs['reply_markup']
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'bridge_view:job:2')
        self.assertEqual([call[0] for call in sequence.mock_calls], ['text', 'album', 'text'])
        self.assertIn('📖 内容统计：', bot.send_message.call_args_list[0].args[1])
        self.assertIn('第 1/2 组', bot.send_message.call_args_list[-1].args[1])

    async def test_details_count_original_media_types_and_real_sizes(self):
        items = [dict(kind='document', source_kind='photo', file_size=1024**2),
                 dict(kind='document', source_kind='video', file_size=2*1024**2),
                 dict(kind='document', source_kind='document', file_size=262144)]
        text = folder_details(dict(id='job', title='示例', source_created_at='2026-11-17 00:35',
                                   source_updated_at='2026-11-17 00:36'), items)
        for expected in ('消息数：4 条', '图片数：1 张', '视频数：1 个', '文档数：1 个',
                         '总大小：3.25 MB', '创建时间：2026-11-17 00:35',
                         '最后更新：2026-11-17 00:36', '⏳ 开始发送内容...'):
            self.assertIn(expected, text)
        self.assertIn('总大小：未知', folder_details(dict(id='job'), [dict(kind='document')]))

    async def test_cover_and_details_are_one_message_with_explicit_content_button(self):
        bot = NS(send_photo=AsyncMock(), send_message=AsyncMock(), send_media_group=AsyncMock())
        await send_folder_card(bot, 7, dict(id='job',title='示例'), [dict(kind='photo',file_size=10)], 'photo-id')
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        bot.send_media_group.assert_not_awaited()
        args = bot.send_photo.call_args.kwargs
        self.assertEqual(args['photo'], 'photo-id')
        self.assertIn('📖 内容统计：', args['caption'])
        self.assertNotIn('开始发送内容', args['caption'])
        buttons = args['reply_markup'].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, 'bridge_view:job:1')
        self.assertEqual(buttons[1][0].callback_data, 'bridge_catalog:1')

    async def test_single_remaining_file_does_not_use_invalid_one_item_album(self):
        bot = NS(send_message=AsyncMock(), send_media_group=AsyncMock())
        send_one = AsyncMock()
        item = dict(kind='document', file_id='file')
        await send_file_page(bot, 7, dict(id='job', title='示例'), [item], 1, send_one)
        send_one.assert_awaited_once_with(bot, 7, item, use_preview=False)
        bot.send_media_group.assert_not_awaited()

    async def test_playable_videos_stay_in_one_album_after_document_pages(self):
        bot = NS(send_message=AsyncMock(), send_media_group=AsyncMock())
        send_one = AsyncMock()
        items = [dict(kind='document', file_id=f'doc{i}') for i in range(24)]
        items += [dict(kind='video', file_id=f'video{i}') for i in range(7)]
        await send_file_page(bot, 7, dict(id='job', title='示例'), items, 4, send_one)
        album = bot.send_media_group.call_args.kwargs['media']
        self.assertEqual([x.media for x in album], [f'video{i}' for i in range(7)])
        self.assertTrue(all(x.type=='video' for x in album))
        send_one.assert_not_awaited()
        self.assertIn('第 4/4 组', bot.send_message.call_args_list[-1].args[1])

    async def test_eleven_videos_split_at_ten_and_keep_last_video_playable(self):
        bot = NS(send_message=AsyncMock(), send_media_group=AsyncMock())
        send_one = AsyncMock()
        items = [dict(kind='video', file_id=f'video{i}') for i in range(11)]
        await send_file_page(bot, 7, dict(id='job', title='示例'), items, 1, send_one)
        self.assertEqual(len(bot.send_media_group.call_args.kwargs['media']),10)
        await send_file_page(bot, 7, dict(id='job', title='示例'), items, 2, send_one)
        send_one.assert_awaited_once_with(bot,7,items[-1])
