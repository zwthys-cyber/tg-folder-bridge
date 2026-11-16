import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from core import Store
from catalog_ui import send_file_page, catalog_page


class Browsing(unittest.IsolatedAsyncioTestCase):
    async def test_details_once_count_receipts_and_keep_state_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db';store=Store(path);store.create('job',7,'link')
            items=[dict(kind='document',file_id=str(i),source_chat=1,source_message=i) for i in range(12)]
            progress=store.start_view(7,'job');sid=progress.id
            bot=NS(send_message=AsyncMock(),send_media_group=AsyncMock())
            single=AsyncMock()
            await send_file_page(bot,7,dict(id='job'),items,1,single,progress=progress)
            self.assertEqual(progress.message_count,11)
            self.assertEqual(bot.send_message.await_count,2)
            store.db.close();store=Store(path);progress=store.view(sid,7)
            self.assertIsNone(store.view(sid,8))
            bot.send_message.reset_mock()
            await send_file_page(bot,7,dict(id='job'),items,2,single,progress=progress)
            bot.send_message.assert_awaited_once()
            self.assertEqual(bot.send_message.call_args.args[1],'✅ 文件夹内容发送完成\n\n共发送 13 条消息')
            bot.send_message.reset_mock()
            await send_file_page(bot,7,dict(id='job'),items,1,single,progress=progress)
            self.assertEqual(bot.send_message.await_count,1)
            self.assertEqual(progress.message_count,23)  # Revisited media really were resent.
            self.assertEqual(progress.received_count(items),12)
            store.db.close()

    async def test_jumping_to_last_page_does_not_claim_whole_folder_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db');store.create('job',7,'link')
            progress=store.start_view(7,'job')
            items=[dict(kind='document',file_id=str(i),source_chat=1,source_message=i) for i in range(12)]
            bot=NS(send_message=AsyncMock(),send_media_group=AsyncMock())
            await send_file_page(bot,7,dict(id='job'),items,2,AsyncMock(),progress=progress)
            text=bot.send_message.call_args.args[1]
            self.assertIn('当前组发送完成',text)
            self.assertNotIn('文件夹内容发送完成',text)
            self.assertEqual(progress.received_count(items),2)
            store.db.close()

    async def test_favorites_are_private_idempotent_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'db';store=Store(path);store.create('job',7,'link')
            store.add_file('job',(1,1),NS(chat_id=7,message_id=1),'file','document')
            store.update('job','complete',1)
            store.favorite(8,'job');store.favorite(8,'job')
            self.assertEqual(len(store.favorite_catalog(8)),1)
            self.assertEqual(store.favorite_catalog(9),[])
            store.db.close();store=Store(path)
            self.assertTrue(store.is_favorite(8,'job'))
            text,markup=catalog_page(store.favorite_catalog(8),page_callback_prefix='bridge_favorites',favorites=True)
            self.assertIn('我的收藏',text)
            self.assertEqual(markup.inline_keyboard[0][0].callback_data,'bridge_open:job:1')
            store.favorite(8,'job',False)
            self.assertEqual(store.favorite_catalog(8),[])
            store.db.close()
